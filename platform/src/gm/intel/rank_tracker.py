"""Rank + AI-Overview tracking (Phase D0, agent A).

Weekly loop: for each active tracked query, reuse-or-buy a SERP snapshot via
serp.get_snapshot with max_age_days=6 — one weekly schedule tick therefore buys
at most one snapshot per query — then derive the client's rank (subdomain-aware
against sites.domain_norm) and AIO citation status, and upsert one rank_history
row per (site, query, checked_on=today). Same-day re-runs are idempotent.

AIO data comes from the snapshot's stored `features` list: serp normalization
retains the ai_overview parse there ({"type": "ai_overview", "cited_domains":
[...]}), so tracking never needs the (unstored) raw provider response.

Honest absence: a query the site does not rank for within the tracked depth is
recorded with rank NULL — never 0.
"""

from __future__ import annotations

import datetime as dt
import logging

from gm.infra import jobs
from gm.intel import serp
from gm.intel.engines.base import normalize_host

log = logging.getLogger(__name__)

TOP_N = 10  # top_domains fingerprint depth (ranks 1..10)


# --- tracked queries --------------------------------------------------------------------


def add_tracked_query(conn, org_id, site_id, query, target_page=None) -> str:
    """Register (or re-activate) a tracked query for a site; returns the row id.

    The query is normalized via serp.query_norm so it shares the snapshot cache
    key. Re-adding an existing query re-activates it; a None target_page never
    clobbers a previously set one.
    """
    row = conn.execute(
        """
        insert into tracked_queries (org_id, site_id, query_norm, target_page)
        values (%s, %s, %s, %s)
        on conflict (site_id, query_norm) do update
           set active = true,
               target_page = coalesce(excluded.target_page, tracked_queries.target_page)
        returning id
        """,
        (org_id, site_id, serp.query_norm(query), target_page),
    ).fetchone()
    return str(row["id"])


# --- pure helpers (no DB) ---------------------------------------------------------------


def _host_matches(host: str, target: str) -> bool:
    """Subdomain-aware host match: exact or any-level subdomain of target."""
    return bool(host) and bool(target) and (host == target or host.endswith("." + target))


def _entry_host(entry: dict) -> str:
    """Normalized host of a snapshot results entry (domain field, else its url)."""
    return normalize_host(str(entry.get("domain") or "") or str(entry.get("url") or ""))


def find_rank(results: list, domain_norm: str) -> tuple[int | None, str | None]:
    """(rank, ranked_url) of the site's best-ranked organic entry, subdomain-aware.

    Absent from the tracked depth -> (None, None): rank is NULL, never 0.
    """
    target = normalize_host(domain_norm)
    best: tuple[int, str] | None = None
    for entry in results or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("rank"), int):
            continue
        if not _host_matches(_entry_host(entry), target):
            continue
        if best is None or entry["rank"] < best[0]:
            best = (entry["rank"], str(entry.get("url") or ""))
    return best if best is not None else (None, None)


def top_domains(results: list, n: int = TOP_N) -> list[str]:
    """Normalized hosts at ranks 1..n in rank order — the SERP fingerprint."""
    ranked = sorted(
        (
            e
            for e in results or []
            if isinstance(e, dict) and isinstance(e.get("rank"), int) and e["rank"] <= n
        ),
        key=lambda e: e["rank"],
    )
    return [host for host in (_entry_host(e) for e in ranked) if host]


def aio_from_features(features: list, domain_norm: str) -> dict:
    """AIO fields from a snapshot's stored feature list (no raw response needed).

    Returns {"present", "cited", "cited_domains"}; cited is subdomain-aware
    against the site's domain_norm.
    """
    target = normalize_host(domain_norm)
    for feature in features or []:
        if isinstance(feature, dict) and feature.get("type") == "ai_overview":
            domains = [d for d in (feature.get("cited_domains") or []) if isinstance(d, str)]
            cited = any(_host_matches(normalize_host(d), target) for d in domains)
            return {"present": True, "cited": cited, "cited_domains": domains}
    return {"present": False, "cited": False, "cited_domains": []}


# --- tracking ----------------------------------------------------------------------------


def track_site(conn, *, org_id, site_id, serp_client=None, max_age_days: int = 6) -> dict:
    """Track every active query for a site: snapshot (reuse-before-buy) -> rank +
    AIO fields -> rank_history upsert for checked_on=today (same-day idempotent).

    Returns counts + spend: {"queries", "tracked", "fresh", "cached", "errors",
    "cost_cents"} — cost_cents covers only snapshots purchased by this call.
    """
    site = conn.execute("select domain_norm from sites where id = %s", (site_id,)).fetchone()
    if site is None:
        raise RuntimeError(f"site not found: {site_id}")
    domain_norm = site["domain_norm"]

    queries = conn.execute(
        "select query_norm from tracked_queries where site_id = %s and active"
        " order by query_norm",
        (site_id,),
    ).fetchall()

    today = dt.date.today()
    counts = {"queries": len(queries), "tracked": 0, "fresh": 0, "cached": 0, "errors": 0}
    cost_cents = 0.0
    for row in queries:
        q = row["query_norm"]
        try:
            snap = serp.get_snapshot(
                conn, site_id, q, max_age_days=max_age_days, client=serp_client
            )
        except serp.SerpError as exc:
            log.warning("track_site: snapshot failed site=%s query=%r: %s", site_id, q, exc)
            counts["errors"] += 1
            continue
        if snap["fresh"]:
            counts["fresh"] += 1
            bought = conn.execute(
                "select cost_cents from serp_snapshots where id = %s", (snap["id"],)
            ).fetchone()
            cost_cents += float(bought["cost_cents"]) if bought else 0.0
        else:
            counts["cached"] += 1

        results = snap["results"] or []
        rank, ranked_url = find_rank(results, domain_norm)
        aio = aio_from_features(snap["features"] or [], domain_norm)
        conn.execute(
            """
            insert into rank_history
              (org_id, site_id, query_norm, checked_on, rank, ranked_url,
               aio_present, aio_cited, aio_cited_domains, top_domains, snapshot_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (site_id, query_norm, checked_on) do update
               set rank = excluded.rank,
                   ranked_url = excluded.ranked_url,
                   aio_present = excluded.aio_present,
                   aio_cited = excluded.aio_cited,
                   aio_cited_domains = excluded.aio_cited_domains,
                   top_domains = excluded.top_domains,
                   snapshot_id = excluded.snapshot_id
            """,
            (
                org_id,
                site_id,
                q,
                today,
                rank,
                ranked_url,
                aio["present"],
                aio["cited"],
                aio["cited_domains"],
                top_domains(results),
                snap["id"],
            ),
        )
        counts["tracked"] += 1
    return {**counts, "cost_cents": round(cost_cents, 4)}


def handle_track_serps(ctx: jobs.JobContext) -> None:
    """Job 'track_serps' (weekly via schedules; payload {}).

    Site-scoped when job.site_id / payload.site_id is set; otherwise tracks every
    site in scope that has active tracked queries.
    """
    conn = ctx.conn
    site_id = ctx.job.site_id or (ctx.job.payload or {}).get("site_id")
    if site_id:
        rows = conn.execute(
            "select distinct org_id, site_id from tracked_queries"
            " where active and site_id = %s",
            (site_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "select distinct org_id, site_id from tracked_queries where active"
        ).fetchall()
    for row in rows:
        result = track_site(conn, org_id=row["org_id"], site_id=row["site_id"])
        log.info("track_serps site=%s %s", row["site_id"], result)


# --- movement assembly (for receipts) ----------------------------------------------------


def rank_movement(conn, site_id, *, since: dt.date, until: dt.date) -> list[dict]:
    """Per-query first/last rank_history rows with checked_on in [since, until]
    (inclusive): rank endpoints, AIO-cited transition, and the competitor domains
    that entered/left the top 10 between the two checks. Pure assembly for
    receipts — no provider calls, no writes. Sorted by query; [] when nothing
    was tracked in the window.
    """
    rows = conn.execute(
        """
        select query_norm, checked_on, rank, ranked_url,
               aio_present, aio_cited, top_domains
          from rank_history
         where site_id = %s and checked_on between %s and %s
         order by query_norm, checked_on
        """,
        (site_id, since, until),
    ).fetchall()
    by_query: dict[str, list[dict]] = {}
    for r in rows:
        by_query.setdefault(r["query_norm"], []).append(r)

    out: list[dict] = []
    for query in sorted(by_query):
        first, last = by_query[query][0], by_query[query][-1]
        first_top = list(dict.fromkeys(first["top_domains"] or []))
        last_top = list(dict.fromkeys(last["top_domains"] or []))
        out.append(
            {
                "query": query,
                "first_date": first["checked_on"],
                "last_date": last["checked_on"],
                "first_rank": first["rank"],
                "last_rank": last["rank"],
                "ranked_url": last["ranked_url"],
                "first_aio_cited": bool(first["aio_cited"]),
                "last_aio_cited": bool(last["aio_cited"]),
                "aio_present": bool(last["aio_present"]),
                "entered_top10": [d for d in last_top if d not in first_top],
                "left_top10": [d for d in first_top if d not in last_top],
            }
        )
    return out
