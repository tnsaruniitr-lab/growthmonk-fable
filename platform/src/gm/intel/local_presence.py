"""Local-presence check family J-05..J-07 (Phase D3, WP-F) — deterministic, $0.

Grades the client's Google local-pack presence from serp_snapshots the weekly
track_serps loop already buys: pure DB reads, zero new provider spend, zero
LLM. gm.audit.pipeline merges local_presence_overrides into its pre-decided
overrides (the comparative-N/A mechanism), so these check ids NEVER reach the
classifier — statuses are byte-identical across runs of the same data.

Extraction honesty: only snapshots stored after serp._normalize_items began
retaining local-pack entries (D3) carry the feature's "entries" list. A
local_pack feature without usable entries counts as "pack present, entries
unobserved" — the raw provider response is unstored (D0 note), so we never
guess. No pack observed at all is an equally honest empty state
("no local-pack sighting"), never a fake fail.

DIAGNOSTIC ONLY (docs/01-product.md §6 do-not-build list): J-07 measures the
review signal; the platform never generates, drafts, solicits, templates, or
automates reviews. The registry fix_template texts point the operator at the
client's own legitimate in-person review-request process, and no fix-closer
path consumes J-05..J-07 (both fix classes are operator-executed, like levers).

Queue surfacing: detect_local_presence upserts kind='local_presence' rows via
detectors._upsert_item on fail/warn, inheriting the open-refresh /
dismissed-snooze / actioned discipline. Kind allowed by migration 011.
"""

from __future__ import annotations

import logging
import statistics

from gm.intel.detectors import _upsert_item
from gm.intel.engines.base import normalize_host

log = logging.getLogger(__name__)

# The family, in registry order. Append-only forever (C-13 gap rule).
LOCAL_PRESENCE_CHECK_IDS: tuple[str, ...] = ("J-05", "J-06", "J-07")

QUEUE_KIND = "local_presence"

# Weekly tracking cadence + slack: the newest snapshot per query inside this
# window is at most one tick stale.
DEFAULT_WINDOW_DAYS = 35

# J-05 completeness: the core listing fields of a local-pack entry.
CORE_FIELDS: tuple[str, ...] = ("title", "rating", "votes", "phone", "url")

# J-07 fail thresholds (contract table).
RATING_FAIL_DELTA = 0.5  # fail when rating < pack median - 0.5
VOTES_FAIL_FACTOR = 0.5  # fail when votes < half the pack median


# --- pure helpers -------------------------------------------------------------------------


def _host_matches(host: str, target: str) -> bool:
    """Subdomain-aware host match (rank_tracker's discipline)."""
    return bool(host) and bool(target) and (host == target or host.endswith("." + target))


def _entry_host(entry: dict) -> str:
    """Normalized host of a local-pack entry — its domain field, else its url."""
    return normalize_host(str(entry.get("domain") or "") or str(entry.get("url") or ""))


def _fold_name(value: str) -> str:
    """Name folding: collapse whitespace + casefold — the 'minor variant' filter."""
    return " ".join(str(value).split()).casefold()


def _fold_phone(value: str) -> str:
    """Phone folding: drop all whitespace (('+971 4 1' vs '+97141') is minor)."""
    return "".join(str(value).split()).casefold()


def _number(value: object) -> float | None:
    """The value as a float when it is a real number, else None (bools excluded)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _entry_is_you(entry: dict, brand_terms_folded: set[str], target_host: str) -> bool:
    """you-match: entry title in brand_terms (case-folded) OR entry domain/url
    host matches domain_norm (subdomain-aware)."""
    title = _fold_name(str(entry.get("title") or ""))
    if title and title in brand_terms_folded:
        return True
    return _host_matches(_entry_host(entry), target_host)


def _deterministic(status: str, note: str) -> dict:
    return {"status": status, "note": note, "source": "deterministic"}


def _plural(n: int) -> str:
    return f"{n} tracked {'query' if n == 1 else 'queries'}"


# --- collection (DB reads only — snapshots already bought weekly) -------------------------


def _load_site(conn, site_id) -> dict:
    row = conn.execute(
        "select org_id, domain_norm, brand_terms from sites where id = %s", (site_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"site not found: {site_id}")
    return row


def collect_local_pack(conn, site_id, *, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Local-pack sightings for a site's active tracked queries — pure DB read.

    Takes the newest in-window serp_snapshots row per active tracked query and
    extracts its local_pack feature (when present):

      * feature carries a usable "entries" list -> one SIGHTING
        {"query_norm", "fetched_at", "entries", "you": entry|None}, where
        "you" is the first entry matching the site (title in brand_terms
        case-folded, or domain/url host matching domain_norm subdomain-aware);
      * feature without entries (snapshot predates D3 entry retention) ->
        counted in packs_unobserved — "pack present, entries unobserved",
        never guessed;
      * no local_pack feature (or no in-window snapshot) -> contributes nothing.

    Returns {"sightings": [... newest first], "packs_seen", "packs_unobserved",
    "you_seen", "note": str|None} — note is the honest empty-state line when
    there is no entry-bearing sighting at all.
    """
    site = _load_site(conn, site_id)
    target_host = normalize_host(str(site["domain_norm"] or ""))
    brand_folded = {
        _fold_name(t) for t in (site["brand_terms"] or []) if str(t or "").strip()
    }
    rows = conn.execute(
        """
        select distinct on (tq.query_norm)
               tq.query_norm, ss.fetched_at, ss.features
          from tracked_queries tq
          join serp_snapshots ss
            on ss.site_id = tq.site_id and ss.query_norm = tq.query_norm
         where tq.site_id = %s and tq.active
           and ss.fetched_at > now() - make_interval(days => %s)
         order by tq.query_norm, ss.fetched_at desc
        """,
        (site_id, window_days),
    ).fetchall()

    sightings: list[dict] = []
    packs_unobserved = 0
    for row in rows:
        features = row["features"] if isinstance(row["features"], list) else []
        feature = next(
            (f for f in features if isinstance(f, dict) and f.get("type") == "local_pack"),
            None,
        )
        if feature is None:
            continue
        raw = feature.get("entries")
        entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
        if not entries:
            packs_unobserved += 1  # legacy snapshot: pack present, entries unobserved
            continue
        you = next((e for e in entries if _entry_is_you(e, brand_folded, target_host)), None)
        sightings.append(
            {
                "query_norm": row["query_norm"],
                "fetched_at": row["fetched_at"],
                "entries": entries,
                "you": you,
            }
        )

    sightings.sort(key=lambda s: s["fetched_at"], reverse=True)
    note = None
    if not sightings:
        if packs_unobserved:
            note = (
                f"local pack present for {_plural(packs_unobserved)} but entries "
                "unobserved (snapshots predate entry retention)"
            )
        else:
            note = f"no local-pack sighting for tracked queries in the last {window_days} days"
    return {
        "sightings": sightings,
        "packs_seen": len(sightings),
        "packs_unobserved": packs_unobserved,
        "you_seen": sum(1 for s in sightings if s["you"] is not None),
        "note": note,
    }


# --- evaluation (PURE — no DB, no network) -------------------------------------------------


def _eval_gbp_presence(pack: dict) -> dict:
    """J-05 — GBP presence & completeness."""
    sightings = pack.get("sightings") or []
    packs_seen = int(pack.get("packs_seen") or 0)
    you_seen = int(pack.get("you_seen") or 0)
    if packs_seen == 0:
        return _deterministic(
            "inconclusive",
            str(pack.get("note") or "no local-pack sighting for tracked queries"),
        )
    if you_seen == 0:
        return _deterministic(
            "fail",
            f"local packs observed for {_plural(packs_seen)}; you appear in none",
        )
    newest = next(s for s in sightings if s.get("you"))
    entry = newest["you"]
    missing = [f for f in CORE_FIELDS if entry.get(f) in (None, "")]
    seen = f"in pack for {you_seen} of {_plural(packs_seen)}"
    if missing:
        return _deterministic(
            "warn", f"{seen}; newest entry missing {', '.join(missing)}"
        )
    return _deterministic(
        "pass", f"{seen}; newest entry carries title, rating, votes, phone, url"
    )


def _eval_nap_consistency(pack: dict, site: dict) -> dict:
    """J-06 — NAP consistency across sightings."""
    attributable = [s for s in (pack.get("sightings") or []) if s.get("you")]
    n = len(attributable)
    if n < 2:
        return _deterministic(
            "inconclusive",
            f"{n} attributable local-pack sighting{'s' if n != 1 else ''};"
            " consistency needs at least 2",
        )
    target_host = normalize_host(str(site.get("domain_norm") or ""))
    titles_raw: set[str] = set()
    phones_raw: set[str] = set()
    hosts: set[str] = set()
    for s in attributable:
        entry = s["you"]
        title = str(entry.get("title") or "").strip()
        if title:
            titles_raw.add(title)
        phone = str(entry.get("phone") or "").strip()
        if phone:
            phones_raw.add(phone)
        host = _entry_host(entry)
        if host:
            hosts.add(host)

    titles_folded = {_fold_name(t) for t in titles_raw}
    phones_folded = {_fold_phone(p) for p in phones_raw}
    foreign = sorted(h for h in hosts if not _host_matches(h, target_host))

    if len(titles_folded) > 1 or len(phones_folded) > 1 or foreign:
        problems: list[str] = []
        if len(titles_folded) > 1:
            problems.append(f"{len(titles_folded)} conflicting names")
        if len(phones_folded) > 1:
            problems.append(f"{len(phones_folded)} conflicting phones")
        if foreign:
            problems.append(f"foreign domain {', '.join(foreign)}")
        return _deterministic("fail", f"across {n} sightings: {'; '.join(problems)}")
    if len(titles_raw) > 1 or len(phones_raw) > 1:
        return _deterministic(
            "warn",
            f"minor name/phone variants (case/whitespace only) across {n} sightings",
        )
    identity = next(iter(titles_raw), None) or "(name unobserved)"
    return _deterministic(
        "pass",
        f"one identity ({identity}) across {n} sightings;"
        f" domain matches {target_host or 'domain_norm'}",
    )


def _eval_review_signal(pack: dict) -> dict:
    """J-07 — review-signal vs local pack (newest attributable sighting)."""
    newest = next((s for s in (pack.get("sightings") or []) if s.get("you")), None)
    if newest is None:
        return _deterministic(
            "inconclusive", "you were not observed in any local pack; review signal unmeasured"
        )
    you = newest["you"]
    rating = _number(you.get("rating"))
    votes = _number(you.get("votes"))
    if rating is None or votes is None:
        return _deterministic(
            "inconclusive", "your rating/votes unobserved in the newest local-pack entry"
        )
    others = [e for e in newest["entries"] if e is not you]
    comp_ratings = sorted(r for r in (_number(e.get("rating")) for e in others) if r is not None)
    comp_votes = sorted(v for v in (_number(e.get("votes")) for e in others) if v is not None)
    if not comp_ratings or not comp_votes:
        return _deterministic(
            "inconclusive",
            f"competitor ratings unobserved in the newest local pack"
            f" ({len(others)} other entries)",
        )
    median_rating = float(statistics.median(comp_ratings))
    median_votes = float(statistics.median(comp_votes))
    numbers = (
        f"rating {rating:g} vs pack median {median_rating:g};"
        f" votes {votes:g} vs median {median_votes:g}"
        f" (query {newest['query_norm']!r})"
    )
    if rating >= median_rating and votes >= median_votes:
        return _deterministic("pass", f"at or above pack medians — {numbers}")
    if rating < median_rating - RATING_FAIL_DELTA or votes < median_votes * VOTES_FAIL_FACTOR:
        return _deterministic("fail", f"well below pack medians — {numbers}")
    return _deterministic("warn", f"below a pack median — {numbers}")


def evaluate_local_presence(pack: dict, site: dict) -> dict[str, dict]:
    """PURE: {check_id: {"status", "note", "source": "deterministic"}} for
    J-05..J-07 per the phase-d3 contract status table. Notes carry the numbers
    ("in pack for 2 of 5 tracked queries") so findings read like evidence."""
    return {
        "J-05": _eval_gbp_presence(pack),
        "J-06": _eval_nap_consistency(pack, site),
        "J-07": _eval_review_signal(pack),
    }


# --- pipeline hook + queue detector --------------------------------------------------------


def local_presence_overrides(conn, site_id, registry) -> dict[str, dict]:
    """Pipeline hook: pre-decided statuses for the local-presence family.

    Restricted to the ids the pinned registry actually carries (mini
    registries in tests, older pinned versions in prod) — {} when the family
    is absent, so no DB work happens for registries that predate v1.4.0.
    """
    ids = [cid for cid in LOCAL_PRESENCE_CHECK_IDS if cid in registry.checks]
    if not ids:
        return {}
    site = _load_site(conn, site_id)
    evaluated = evaluate_local_presence(collect_local_pack(conn, site_id), site)
    return {cid: evaluated[cid] for cid in ids}


def detect_local_presence(conn, site_id) -> int:
    """Queue surfacing: one kind='local_presence' upsert per fail/warn check.

    target={"check_id"}; at_stake={"issue", "queries_with_pack",
    "basis": "serp_local_pack"} — via detectors._upsert_item, inheriting the
    open-refresh / dismissed-snooze / actioned discipline. Returns the number
    of items touched. pass/inconclusive statuses never enqueue (honest empty
    queue beats invented work).
    """
    site = _load_site(conn, site_id)
    pack = collect_local_pack(conn, site_id)
    evaluated = evaluate_local_presence(pack, site)
    touched = 0
    for cid in LOCAL_PRESENCE_CHECK_IDS:
        info = evaluated[cid]
        if info["status"] not in ("fail", "warn"):
            continue
        _upsert_item(
            conn,
            org_id=site["org_id"],
            site_id=site_id,
            kind=QUEUE_KIND,
            target={"check_id": cid},
            at_stake={
                "issue": info["note"],
                "queries_with_pack": pack["packs_seen"],
                "basis": "serp_local_pack",
            },
        )
        touched += 1
    return touched
