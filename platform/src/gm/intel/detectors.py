"""Opportunity detectors -> the operator queue (Phase C wave 1).

Four detectors scan Search Console data for a site and upsert queue_items rows.

Data-source selection (recorded in at_stake.basis):

- FINAL mode when gsc_ingest_log holds >= FINAL_MODE_MIN_DAYS (28) final days
  for the site: 28d aggregates come from gsc_daily / gsc_page_daily, windows
  anchored on the latest final day. All four detectors run.
- PROVISIONAL mode otherwise: aggregates come from the phase-1 whole-window
  pull (gsc_window_agg, window_days=28). Only striking_distance and
  ctr_outlier run; decay and cannibalization need day-level history, so
  compute_queue reports them as skipped with reason 'insufficient history'
  (an honest gap, closed as the backfill lands).

Upsert discipline (unique site_id + kind + target_hash): on conflict refresh
at_stake + last_seen ONLY when the row is 'open', or when it is 'dismissed'
with an elapsed snooze_until — which reopens it (status back to 'open',
snooze cleared). 'actioned' and 'done' rows are never touched. Targets that
vanish from the data leave their rows behind (operator history): detectors
never delete.

target_hash = sha256(canonical JSON of the kind-specific identity)[:16]:
{page, query} for striking_distance / ctr_outlier, {query} for
cannibalization, {page} for decay.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

import psycopg
from psycopg.types.json import Jsonb

from gm.infra import jobs

log = logging.getLogger(__name__)

# --- thresholds -----------------------------------------------------------------------

FINAL_MODE_MIN_DAYS = 28       # final days required before day-level detectors run
MIN_IMPRESSIONS = 100          # 28d impressions floor (striking, ctr_outlier, cannibalization)
STRIKING_MIN_POSITION = 5.0    # inclusive
STRIKING_MAX_POSITION = 20.0   # inclusive
CTR_OUTLIER_MAX_POSITION = 5.0
CTR_OUTLIER_FACTOR = 0.5       # flag when ctr < 0.5 * expected_ctr(position)
DECAY_DROP_THRESHOLD = 0.25    # >= 25% click drop flags
DECAY_MIN_BASE_CLICKS = 30     # noise floor: baseline window needs this many clicks
CANNIBALIZATION_MIN_SHARE = 0.20  # inclusive share of query impressions per page

# Tolerance for float-boundary comparisons (e.g. a planted ctr of exactly half the
# expected value must NOT flag, even though 0.5*float64(0.09) != 45/1000 bit-for-bit).
_EPS = 1e-12

# Expected organic CTR by (rounded) SERP position. Rough blend of published organic
# CTR curves — Advanced Web Ranking (2023), Backlinko (2023), Sistrix (2020) desktop
# aggregates — deliberately taken at the LOW end of the published ranges so that
# est_clicks_gain under-promises rather than over-promises.
EXPECTED_CTR_BY_POSITION: dict[int, float] = {
    1: 0.25,
    2: 0.13,
    3: 0.09,
    4: 0.06,
    5: 0.045,
    6: 0.035,
    7: 0.028,
    8: 0.022,
    9: 0.018,
    10: 0.015,
    11: 0.012,
    12: 0.011,
    13: 0.010,
    14: 0.009,
    15: 0.008,
    16: 0.0075,
    17: 0.007,
    18: 0.0065,
    19: 0.006,
    20: 0.0055,
}
EXPECTED_CTR_FLOOR = 0.005  # positions beyond 20


def expected_ctr(position: float) -> float:
    """Expected organic CTR at a (possibly fractional) average position."""
    idx = int(round(position))
    if idx < 1:
        idx = 1
    return EXPECTED_CTR_BY_POSITION.get(idx, EXPECTED_CTR_FLOOR)


# --- identity hashing -----------------------------------------------------------------


def canonical_target(target: dict) -> str:
    """Canonical JSON: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def target_hash(target: dict) -> str:
    """sha256 of the canonical JSON of the kind-specific identity, first 16 hex chars."""
    return hashlib.sha256(canonical_target(target).encode("utf-8")).hexdigest()[:16]


# --- queue upsert ---------------------------------------------------------------------

_UPSERT_SQL = """
insert into queue_items (org_id, site_id, kind, page_id, target, target_hash, at_stake)
values (%(org_id)s, %(site_id)s, %(kind)s, %(page_id)s, %(target)s, %(target_hash)s, %(at_stake)s)
on conflict (site_id, kind, target_hash) do update
   set at_stake = excluded.at_stake,
       last_seen = now(),
       status = 'open',
       snooze_until = null
 where queue_items.status = 'open'
    or (queue_items.status = 'dismissed'
        and queue_items.snooze_until is not null
        and queue_items.snooze_until < now())
"""


def _upsert_item(
    conn: psycopg.Connection,
    *,
    org_id,
    site_id,
    kind: str,
    target: dict,
    at_stake: dict,
    page: str | None = None,
) -> None:
    page_id = None
    if page is not None:
        row = conn.execute(
            "select id from pages where site_id = %s and url_norm = %s", (site_id, page)
        ).fetchone()
        page_id = row["id"] if row else None
    conn.execute(
        _UPSERT_SQL,
        {
            "org_id": org_id,
            "site_id": site_id,
            "kind": kind,
            "page_id": page_id,
            "target": Jsonb(target),
            "target_hash": target_hash(target),
            "at_stake": Jsonb(at_stake),
        },
    )


# --- data loading ---------------------------------------------------------------------


def _select_basis(conn: psycopg.Connection, site_id: str) -> tuple[str, dt.date | None]:
    """('final', latest_final_date) with >= 28 final days ingested, else ('provisional', None)."""
    row = conn.execute(
        "select count(distinct date) as n, max(date) as latest"
        "  from gsc_ingest_log"
        " where site_id = %s and search_type = 'web' and final",
        (site_id,),
    ).fetchone()
    if row["n"] >= FINAL_MODE_MIN_DAYS:
        return "final", row["latest"]
    return "provisional", None


def load_rows_28d(
    conn: psycopg.Connection,
    site_id: str,
    *,
    basis: str,
    latest_final: dt.date | None = None,
) -> list[dict]:
    """28d (page, query) aggregates. CTR is recomputed from click/impression sums so
    detector math is float64-exact rather than trusting the stored real column."""
    if basis == "final":
        if latest_final is None:
            raise ValueError("final basis requires latest_final")
        return conn.execute(
            """
            select page, query,
                   sum(clicks)::int as clicks,
                   sum(impressions)::int as impressions,
                   case when sum(impressions) > 0
                        then sum(clicks)::float8 / sum(impressions) else 0.0 end as ctr,
                   case when sum(impressions) > 0
                        then sum(position::float8 * impressions) / sum(impressions)
                        else avg(position)::float8 end as position
              from gsc_daily
             where site_id = %s and search_type = 'web' and date between %s and %s
             group by page, query
            """,
            (site_id, latest_final - dt.timedelta(days=27), latest_final),
        ).fetchall()
    return conn.execute(
        """
        select page, query, clicks, impressions,
               case when impressions > 0
                    then clicks::float8 / impressions else 0.0 end as ctr,
               position::float8 as position
          from gsc_window_agg
         where site_id = %s and window_days = 28
        """,
        (site_id,),
    ).fetchall()


def _final_days_in(conn: psycopg.Connection, site_id, start: dt.date, end: dt.date) -> int:
    row = conn.execute(
        "select count(distinct date) as n from gsc_ingest_log"
        " where site_id = %s and search_type = 'web' and final and date between %s and %s",
        (site_id, start, end),
    ).fetchone()
    return row["n"]


# --- detectors ------------------------------------------------------------------------


def striking_distance(
    conn: psycopg.Connection, *, org_id, site_id, rows: list[dict], basis: str
) -> int:
    """Pages/queries at avg position 5-20 (inclusive) with >= 100 impressions over 28d.

    est_clicks_gain = impressions * (ctr_at_position_3 - ctr_now), clamped at 0:
    the clicks at stake if the query moved into the top 3.
    """
    n = 0
    for r in rows:
        if not (STRIKING_MIN_POSITION <= r["position"] <= STRIKING_MAX_POSITION):
            continue
        if r["impressions"] < MIN_IMPRESSIONS:
            continue
        gain = max(0.0, r["impressions"] * (expected_ctr(3.0) - r["ctr"]))
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind="striking_distance",
            page=r["page"],
            target={"page": r["page"], "query": r["query"]},
            at_stake={
                "est_clicks_gain": round(gain, 2),
                "basis": basis,
                "impressions": r["impressions"],
                "clicks": r["clicks"],
                "ctr": round(r["ctr"], 6),
                "position": round(r["position"], 2),
            },
        )
        n += 1
    return n


def ctr_outlier(
    conn: psycopg.Connection, *, org_id, site_id, rows: list[dict], basis: str
) -> int:
    """Top-5 positions earning less than half the expected CTR (>= 100 impressions floor).

    est_clicks_gain = impressions * (expected_ctr(position) - ctr_now): the clicks at
    stake if the snippet performed at par for its position.
    """
    n = 0
    for r in rows:
        if r["position"] > CTR_OUTLIER_MAX_POSITION:
            continue
        if r["impressions"] < MIN_IMPRESSIONS:
            continue
        exp = expected_ctr(r["position"])
        if not (r["ctr"] < CTR_OUTLIER_FACTOR * exp - _EPS):
            continue
        gain = max(0.0, r["impressions"] * (exp - r["ctr"]))
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind="ctr_outlier",
            page=r["page"],
            target={"page": r["page"], "query": r["query"]},
            at_stake={
                "est_clicks_gain": round(gain, 2),
                "basis": basis,
                "impressions": r["impressions"],
                "clicks": r["clicks"],
                "ctr": round(r["ctr"], 6),
                "expected_ctr": exp,
                "position": round(r["position"], 2),
            },
        )
        n += 1
    return n


def decay(
    conn: psycopg.Connection, *, org_id, site_id, latest_final: dt.date
) -> int:
    """FINAL data only: per-page 28d clicks vs prior-28d and vs same-28d-last-year.

    A drop >= 25% against either fully-ingested baseline flags the page. Baselines
    below DECAY_MIN_BASE_CLICKS are skipped (percentage swings on tiny bases are
    noise). The YoY window is shifted 364 days (52 weeks) to keep weekday alignment.
    Windows without complete final-day coverage in gsc_ingest_log are not compared,
    so partial ingestion never fakes a drop. est_clicks_gain = the largest flagged
    (baseline - current) click loss per 28d.
    """
    cur_end = latest_final
    cur_start = cur_end - dt.timedelta(days=27)
    prior_end = cur_start - dt.timedelta(days=1)
    prior_start = prior_end - dt.timedelta(days=27)
    yoy_start = cur_start - dt.timedelta(days=364)
    yoy_end = cur_end - dt.timedelta(days=364)

    if _final_days_in(conn, site_id, cur_start, cur_end) < FINAL_MODE_MIN_DAYS:
        return 0  # current window not fully ingested yet: comparisons would be fake drops
    prior_ok = _final_days_in(conn, site_id, prior_start, prior_end) >= FINAL_MODE_MIN_DAYS
    yoy_ok = _final_days_in(conn, site_id, yoy_start, yoy_end) >= FINAL_MODE_MIN_DAYS
    if not (prior_ok or yoy_ok):
        return 0

    rows = conn.execute(
        """
        select page,
               coalesce(sum(clicks) filter (where date between %(c0)s and %(c1)s), 0)::int as cur,
               coalesce(sum(clicks) filter (where date between %(p0)s and %(p1)s), 0)::int
                   as prior,
               coalesce(sum(clicks) filter (where date between %(y0)s and %(y1)s), 0)::int as yoy
          from gsc_page_daily
         where site_id = %(site_id)s
           and (date between %(c0)s and %(c1)s
             or date between %(p0)s and %(p1)s
             or date between %(y0)s and %(y1)s)
         group by page
        """,
        {
            "site_id": site_id,
            "c0": cur_start, "c1": cur_end,
            "p0": prior_start, "p1": prior_end,
            "y0": yoy_start, "y1": yoy_end,
        },
    ).fetchall()

    n = 0
    for r in rows:
        cur = r["cur"]
        losses: list[int] = []
        drop_pct = yoy_drop_pct = None
        if prior_ok and r["prior"] >= DECAY_MIN_BASE_CLICKS:
            drop_pct = (r["prior"] - cur) / r["prior"]
            if drop_pct >= DECAY_DROP_THRESHOLD:
                losses.append(r["prior"] - cur)
        if yoy_ok and r["yoy"] >= DECAY_MIN_BASE_CLICKS:
            yoy_drop_pct = (r["yoy"] - cur) / r["yoy"]
            if yoy_drop_pct >= DECAY_DROP_THRESHOLD:
                losses.append(r["yoy"] - cur)
        if not losses:
            continue
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind="decay",
            page=r["page"],
            target={"page": r["page"]},
            at_stake={
                "est_clicks_gain": float(max(losses)),
                "basis": "final",
                "clicks_28d": cur,
                "prior_clicks": r["prior"] if prior_ok else None,
                "drop_pct": round(drop_pct, 4) if drop_pct is not None else None,
                "yoy_clicks": r["yoy"] if yoy_ok else None,
                "yoy_drop_pct": round(yoy_drop_pct, 4) if yoy_drop_pct is not None else None,
            },
        )
        n += 1
    return n


def cannibalization(
    conn: psycopg.Connection, *, org_id, site_id, rows: list[dict]
) -> int:
    """FINAL data only: queries where 2+ pages each take >= 20% of the query's 28d
    impressions (>= 100 impressions total). est_clicks_gain = query impressions *
    expected_ctr(best qualifying position) - current query clicks, clamped at 0:
    a rough consolidated-page upside, conservative by construction of the CTR table.
    """
    by_query: dict[str, list[dict]] = {}
    for r in rows:
        by_query.setdefault(r["query"], []).append(r)

    n = 0
    for query, page_rows in by_query.items():
        total_impr = sum(r["impressions"] for r in page_rows)
        if total_impr < MIN_IMPRESSIONS:
            continue
        qualifying = [
            r for r in page_rows
            if r["impressions"] / total_impr >= CANNIBALIZATION_MIN_SHARE
        ]
        if len(qualifying) < 2:
            continue
        total_clicks = sum(r["clicks"] for r in page_rows)
        best_pos = min(r["position"] for r in qualifying)
        gain = max(0.0, total_impr * expected_ctr(best_pos) - total_clicks)
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind="cannibalization",
            target={"query": query},
            at_stake={
                "est_clicks_gain": round(gain, 2),
                "basis": "final",
                "impressions_total": total_impr,
                "clicks_total": total_clicks,
                "pages": [
                    {
                        "page": r["page"],
                        "impressions": r["impressions"],
                        "clicks": r["clicks"],
                        "position": round(r["position"], 2),
                        "share": round(r["impressions"] / total_impr, 4),
                    }
                    for r in sorted(qualifying, key=lambda r: -r["impressions"])
                ],
            },
        )
        n += 1
    return n


# --- orchestration --------------------------------------------------------------------


def _detect_local_presence_fn():
    """Lazy accessor for gm.intel.local_presence.detect_local_presence (Phase D3, WP-F).

    Resolved at call time (the D0 _rank_movement_fn pattern in receipts.py):
    tests monkeypatch it, and a partially deployed wave never breaks the queue
    computation — an absent module is reported as an honest skip, never a fake
    zero count.
    """
    try:
        from gm.intel.local_presence import detect_local_presence
    except ImportError:
        return None
    return detect_local_presence


def compute_queue(conn: psycopg.Connection, site_id: str) -> dict:
    """Run every applicable detector for the site; returns
    {"basis": ..., "counts": {kind: n, ...}, "skipped": {kind: reason, ...}}."""
    site = conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
    if site is None:
        raise RuntimeError(f"site not found: {site_id}")
    org_id = site["org_id"]

    basis, latest_final = _select_basis(conn, site_id)
    rows = load_rows_28d(conn, site_id, basis=basis, latest_final=latest_final)

    counts: dict[str, int] = {
        "striking_distance": striking_distance(
            conn, org_id=org_id, site_id=site_id, rows=rows, basis=basis
        ),
        "ctr_outlier": ctr_outlier(conn, org_id=org_id, site_id=site_id, rows=rows, basis=basis),
    }
    skipped: dict[str, str] = {}
    if basis == "final":
        counts["decay"] = decay(conn, org_id=org_id, site_id=site_id, latest_final=latest_final)
        counts["cannibalization"] = cannibalization(
            conn, org_id=org_id, site_id=site_id, rows=rows
        )
    else:
        # Honest gap: these need day-level history that provisional mode doesn't have.
        skipped["decay"] = "insufficient history"
        skipped["cannibalization"] = "insufficient history"

    # Phase D3 (WP-F): local-presence detector over SERP local-pack sightings —
    # not GSC data, so it runs on either basis. Lazy import per the D0
    # _rank_movement_fn pattern: an absent module is an honest skip.
    detect_local = _detect_local_presence_fn()
    if detect_local is not None:
        counts["local_presence"] = detect_local(conn, site_id)
    else:
        skipped["local_presence"] = "module unavailable"
    return {"basis": basis, "counts": counts, "skipped": skipped}


def handle_compute_queue(ctx: jobs.JobContext) -> None:
    """Job 'compute_queue': payload/job.site_id -> compute_queue."""
    site_id = ctx.job.site_id or (ctx.job.payload or {}).get("site_id")
    if not site_id:
        raise RuntimeError("compute_queue job requires site_id")
    result = compute_queue(ctx.conn, str(site_id))
    log.info(
        "compute_queue site=%s basis=%s counts=%s skipped=%s",
        site_id, result["basis"], result["counts"], result["skipped"],
    )
