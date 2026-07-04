"""SERP-feature share + competitive position (Phase D2, WP-C).

Pure assembly over rows already bought (serp_snapshots, rank_history, audits,
tracked_queries, competitor_profiles) — ZERO new spend, no provider calls, no
LLM. Every number is deterministic Python/SQL.

feature_share buckets in-window snapshots of the active tracked-query panel
into Monday-start weeks (latest snapshot per query per week) and attributes
each tracked SERP feature (ai_overview, featured_snippet, people_also_ask) to
its owner(s), subdomain-aware against the site's domain_norm and the
configured sites.competitor_domains:

  * "you"          — an owner host matches the site,
  * competitors    — one count per matching configured competitor host,
  * "other"        — some owner matched neither you nor any competitor,
  * "unattributed" — the feature is present but carries no owner data
                     (snapshots written before D2 owner retention, or a
                     feature the provider returned without sources).

Multi-source features (AI Overview citations, PAA answer sources) count each
party per query, so buckets may overlap; the single-owner featured snippet
increments exactly one of you/competitor/other per query.

competitive_position is THE section data contract consumed by the receipt
renderer, the CLI, and the admin API: a "you" summary plus one row per
configured competitor (rank counts from the last-in-window rank_history row
per query, AIO citations, competitor_reference audit medians, latest monthly
profile) plus the feature_share table. Empty-state law: a competitor with no
observations gets has_data=False and callers render "no data yet" — zeros are
FORBIDDEN there. Rank counts are None (never 0) when the window holds no
rank_history rows at all; once rows exist, a 0 is a true measurement (we
looked at every tracked SERP and the party was absent).

gm.intel.competitors (WP-A) is built concurrently: latest_profile is reached
through the lazy _latest_profile_fn accessor and its absence is tolerated
(profile stays None) per the D0 _rank_movement_fn pattern in receipts.py.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from gm.intel.engines.base import normalize_host

FEATURE_TYPES = ("ai_overview", "featured_snippet", "people_also_ask")

# Audits that never enter the client's own stats (mirrors the page-audit
# filter in gm.delivery.receipts.assemble_site_receipt).
_EXCLUDED_GATE_STATES = ("competitor_reference", "group_rollup", "draft")


def _latest_profile_fn():
    """Lazy accessor for gm.intel.competitors.latest_profile (WP-A, concurrent).

    Resolved at call time (D0 _rank_movement_fn pattern): tests monkeypatch it,
    and a partially deployed wave never breaks position assembly — an absent
    module simply means every profile is None ("no data yet").
    """
    try:
        from gm.intel.competitors import latest_profile
    except ImportError:
        return None
    return latest_profile


# --- pure helpers -------------------------------------------------------------------------


def _host_matches(host: str, target: str) -> bool:
    """Subdomain-aware host match (local copy of rank_tracker's private helper)."""
    return bool(host) and bool(target) and (host == target or host.endswith("." + target))


def week_start(day: dt.date) -> dt.date:
    """Monday of the week containing `day` (Mon-start buckets)."""
    return day - dt.timedelta(days=day.weekday())


def _configured_competitors(site_row: dict) -> list[str]:
    """sites.competitor_domains normalized, deduped, configured order preserved."""
    out: list[str] = []
    for domain in site_row["competitor_domains"] or []:
        host = normalize_host(str(domain or "").strip())
        if host and host not in out:
            out.append(host)
    return out


def _feature_owners(ftype: str, feature: dict) -> list[str] | None:
    """Normalized owner hosts of one stored snapshot feature entry.

    None means the snapshot predates owner retention (the owner fields are
    absent entirely) — callers count it "unattributed", never guess. An empty
    list means the fields were retained but carried no attributable host.
    """
    if ftype == "featured_snippet":
        if "domain" not in feature and "url" not in feature:
            return None
        owner = str(feature.get("domain") or "") or str(feature.get("url") or "")
        host = normalize_host(owner) if owner else ""
        return [host] if host else []
    key = "cited_domains" if ftype == "ai_overview" else "source_domains"
    if key not in feature:
        return None
    values = feature.get(key)
    out: list[str] = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, str) and value.strip():
            host = normalize_host(value.strip())
            if host and host not in out:
                out.append(host)
    return out


def _empty_bucket() -> dict[str, Any]:
    return {"present": 0, "you": 0, "competitors": {}, "other": 0, "unattributed": 0}


def _overall_score(scores: Any) -> float | None:
    value = scores.get("overall_score") if isinstance(scores, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _median(values: list[float]) -> tuple[float | None, int]:
    """(median rounded to 1 decimal, n) — (None, 0) when there is nothing to say."""
    if not values:
        return None, 0
    return round(float(statistics.median(values)), 1), len(values)


# --- feature share -------------------------------------------------------------------------


def feature_share(conn, site_id, *, since: dt.date, until: dt.date) -> dict:
    """Weekly SERP-feature ownership over the active tracked-query panel.

    Returns {"weeks": [{"week_start": iso date (Monday), "features": {ftype:
    {"present", "you", "competitors": {host: n}, "other", "unattributed"}}}],
    "queries": panel size, "note": str|None}. Weeks ascend; only weeks with at
    least one in-window snapshot appear. The competitors map carries only
    hosts with a non-zero count. Both window bounds are inclusive dates
    (snapshots bucketed by their UTC fetch date).
    """
    site = conn.execute(
        "select domain_norm, competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()
    if site is None:
        raise ValueError(f"site {site_id} not found")
    you = normalize_host(site["domain_norm"] or "")
    competitors = _configured_competitors(site)

    panel = [
        r["query_norm"]
        for r in conn.execute(
            "select query_norm from tracked_queries where site_id = %s and active"
            " order by query_norm",
            (site_id,),
        ).fetchall()
    ]
    if not panel:
        return {"weeks": [], "queries": 0, "note": "no tracked queries yet"}

    rows = conn.execute(
        "select query_norm, features, fetched_at from serp_snapshots"
        " where site_id = %s and query_norm = any(%s)"
        " and (fetched_at at time zone 'UTC')::date between %s and %s"
        " order by fetched_at",
        (site_id, panel, since, until),
    ).fetchall()
    # Latest snapshot per (week, query): rows ascend, so the last write wins.
    latest: dict[tuple[dt.date, str], list] = {}
    for row in rows:
        day = row["fetched_at"].astimezone(dt.UTC).date()
        features = row["features"] if isinstance(row["features"], list) else []
        latest[(week_start(day), row["query_norm"])] = features

    weeks: dict[dt.date, dict[str, dict]] = {}
    present_total = 0
    retained_seen = False
    for (wk, _query), features in latest.items():
        buckets = weeks.setdefault(wk, {ftype: _empty_bucket() for ftype in FEATURE_TYPES})
        for ftype in FEATURE_TYPES:
            feature = next(
                (f for f in features if isinstance(f, dict) and f.get("type") == ftype), None
            )
            if feature is None:
                continue
            bucket = buckets[ftype]
            bucket["present"] += 1
            present_total += 1
            owners = _feature_owners(ftype, feature)
            if owners is None:  # pre-retention snapshot: never guess
                bucket["unattributed"] += 1
                continue
            retained_seen = True
            if not owners:  # retained but sourceless: no owner to credit
                bucket["unattributed"] += 1
                continue
            if any(_host_matches(o, you) for o in owners):
                bucket["you"] += 1
            for comp in competitors:
                if any(_host_matches(o, comp) for o in owners):
                    bucket["competitors"][comp] = bucket["competitors"].get(comp, 0) + 1
            others = (
                o
                for o in owners
                if not _host_matches(o, you)
                and not any(_host_matches(o, comp) for comp in competitors)
            )
            if any(others):
                bucket["other"] += 1

    note = None
    if not latest:
        note = "no SERP snapshots in the window yet"
    elif present_total and not retained_seen:
        note = "snapshots predate feature-owner retention — owners unattributed"
    return {
        "weeks": [
            {"week_start": wk.isoformat(), "features": weeks[wk]} for wk in sorted(weeks)
        ],
        "queries": len(panel),
        "note": note,
    }


# --- competitive position --------------------------------------------------------------------


def _fingerprint_position(top_domains: list, target: str) -> int | None:
    """1-based position of the first fingerprint host matching target, else None."""
    for idx, host in enumerate(top_domains or [], start=1):
        if isinstance(host, str) and _host_matches(normalize_host(host), target):
            return idx
    return None


def competitive_position(conn, site_id, *, since: dt.date, until: dt.date) -> dict:
    """THE competitive-position section payload (receipt/CLI/API contract).

    {"window": {"since","until"}, "you": {...}, "competitors": [...],
    "feature_share": feature_share(...), "note": str|None}. Both window bounds
    are inclusive dates. Rank counts derive from the last-in-window
    rank_history row per query (the site's own rank column for "you"; the
    top_domains fingerprint / aio_cited_domains for competitors) and are None
    when the window holds no rank rows. Audit medians: "you" mirrors the
    receipt's page-audit filter; competitors use done in-window audits with
    gate_state='competitor_reference' whose url host matches (subdomain-aware).
    has_data=False (no rank observations, no audits, no profile) means callers
    render "no data yet" — zeros are forbidden there.
    """
    site = conn.execute(
        "select domain_norm, competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()
    if site is None:
        raise ValueError(f"site {site_id} not found")
    you_host = normalize_host(site["domain_norm"] or "")
    competitors_cfg = _configured_competitors(site)
    # timestamptz-vs-date comparisons depend on the session timezone; pin the
    # audit window to explicit UTC instants so results match CI/prod everywhere.
    window_start_ts = dt.datetime.combine(since, dt.time.min, tzinfo=dt.UTC)
    window_end_ts = dt.datetime.combine(
        until + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
    )

    tracked = int(
        conn.execute(
            "select count(*) as n from tracked_queries where site_id = %s and active",
            (site_id,),
        ).fetchone()["n"]
    )

    last_rows = conn.execute(
        """
        select distinct on (query_norm)
               query_norm, rank, aio_cited, aio_cited_domains, top_domains
          from rank_history
         where site_id = %s and checked_on between %s and %s
         order by query_norm, checked_on desc
        """,
        (site_id, since, until),
    ).fetchall()
    have_ranks = bool(last_rows)

    if have_ranks:
        you_top3 = sum(1 for r in last_rows if isinstance(r["rank"], int) and r["rank"] <= 3)
        you_top10 = sum(1 for r in last_rows if isinstance(r["rank"], int) and r["rank"] <= 10)
        you_aio = sum(1 for r in last_rows if r["aio_cited"])
    else:  # no observations: None, never a fake 0
        you_top3 = you_top10 = you_aio = None

    you_audits = conn.execute(
        """
        select scores from audits
         where site_id = %s and status = 'done' and draft_id is null
           and coalesce(gate_state, 'ok') != all(%s)
           and coalesce(finished_at, created_at) >= %s
           and coalesce(finished_at, created_at) < %s
        """,
        (site_id, list(_EXCLUDED_GATE_STATES), window_start_ts, window_end_ts),
    ).fetchall()
    you_median, you_n = _median(
        [s for s in (_overall_score(r["scores"]) for r in you_audits) if s is not None]
    )

    # Competitor-reference audits carry the CLIENT's site_id and the competitor
    # URL (gm.audit.compare) — fetched once, attributed per competitor by host.
    ref_audits = conn.execute(
        """
        select url, scores from audits
         where site_id = %s and status = 'done' and gate_state = 'competitor_reference'
           and coalesce(finished_at, created_at) >= %s
           and coalesce(finished_at, created_at) < %s
        """,
        (site_id, window_start_ts, window_end_ts),
    ).fetchall()

    profile_fn = _latest_profile_fn()
    competitors: list[dict] = []
    for comp in competitors_cfg:
        if have_ranks:
            top3 = top10 = aio = 0
            for row in last_rows:
                pos = _fingerprint_position(row["top_domains"], comp)
                if pos is not None and pos <= 3:
                    top3 += 1
                if pos is not None and pos <= 10:
                    top10 += 1
                cited = (
                    d for d in row["aio_cited_domains"] or [] if isinstance(d, str)
                )
                if any(_host_matches(normalize_host(d), comp) for d in cited):
                    aio += 1
        else:
            top3 = top10 = aio = None
        scores = [
            s
            for s in (
                _overall_score(r["scores"])
                for r in ref_audits
                if _host_matches(normalize_host(str(r["url"] or "")), comp)
            )
            if s is not None
        ]
        audit_median, audit_n = _median(scores)
        profile = profile_fn(conn, site_id, comp) if profile_fn is not None else None
        competitors.append(
            {
                "domain": comp,
                "rank_top3": top3,
                "rank_top10": top10,
                "aio_citations": aio,
                "audit_median": audit_median,
                "audit_n": audit_n,
                "profile": profile,
                "has_data": have_ranks or audit_n > 0 or profile is not None,
            }
        )

    notes = []
    if not tracked:
        notes.append("no tracked queries yet")
    if not competitors_cfg:
        notes.append("no competitors configured")
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "you": {
            "domain": you_host,
            "tracked_queries": tracked,
            "rank_top3": you_top3,
            "rank_top10": you_top10,
            "aio_citations": you_aio,
            "audit_median": you_median,
            "audit_n": you_n,
        },
        "competitors": competitors,
        "feature_share": feature_share(conn, site_id, since=since, until=until),
        "note": "; ".join(notes) or None,
    }
