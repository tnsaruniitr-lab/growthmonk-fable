"""Tests for the opportunity detectors + operator-queue upsert discipline.

Pure tests (hashing, expected-CTR table) always run. DB tests plant fixture rows
in the gsc_* tables and skip cleanly when DATABASE_URL is unset. No network.
"""

import datetime as dt
import hashlib
import os
import uuid

import pytest

from gm import db
from gm.infra import jobs
from gm.intel import detectors

needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

# Latest plausible "final" GSC day: comfortably older than today-3.
ANCHOR = dt.date.today() - dt.timedelta(days=4)


# --- pure: target_hash + expected_ctr -------------------------------------------------


def test_target_hash_is_sha256_of_canonical_json_first_16():
    target = {"query": "kw", "page": "https://e.x/p"}
    canonical = '{"page":"https://e.x/p","query":"kw"}'
    expected = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    assert detectors.target_hash(target) == expected
    assert len(detectors.target_hash(target)) == 16
    # key order must not matter (canonical JSON sorts keys)
    assert detectors.target_hash({"page": "https://e.x/p", "query": "kw"}) == expected
    # different identities hash differently
    assert detectors.target_hash({"page": "https://e.x/p"}) != expected


def test_expected_ctr_table_and_lookup():
    assert detectors.expected_ctr(3.0) == detectors.EXPECTED_CTR_BY_POSITION[3]
    # monotone-ish and conservative
    assert detectors.expected_ctr(1) > detectors.expected_ctr(5) > detectors.expected_ctr(20)
    # fractional positions round; out-of-range clamps
    assert detectors.expected_ctr(2.4) == detectors.EXPECTED_CTR_BY_POSITION[2]
    assert detectors.expected_ctr(0.6) == detectors.EXPECTED_CTR_BY_POSITION[1]
    assert detectors.expected_ctr(35.0) == detectors.EXPECTED_CTR_FLOOR


# --- DB fixtures ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    with db.connect(autocommit=True) as c:
        c.execute("truncate queue_items, gsc_window_agg, gsc_page_daily, gsc_ingest_log cascade")
        c.execute("truncate gsc_daily")
        yield c


@pytest.fixture()
def site(conn):
    org_id = conn.execute("insert into orgs (name) values ('t') returning id").fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org_id, f"det-{uuid.uuid4().hex[:10]}.example"),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _ensure_partitions(conn, start: dt.date, end: dt.date) -> None:
    month = start.replace(day=1)
    while month <= end:
        nxt = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        conn.execute(
            f"create table if not exists gsc_daily_y{month.year}m{month.month:02d} "
            f"partition of gsc_daily for values from ('{month}') to ('{nxt}')"
        )
        month = nxt


def _plant_final_days(conn, site_id, start: dt.date, end: dt.date) -> None:
    conn.execute(
        "insert into gsc_ingest_log (site_id, date, rows, final)"
        " select %s, d::date, 1, true"
        "   from generate_series(%s::date, %s::date, interval '1 day') d"
        " on conflict (site_id, date, search_type) do update set final = true",
        (site_id, start, end),
    )


def _daily(conn, site_id, day, page, query, clicks, impressions, position):
    _ensure_partitions(conn, day, day)
    conn.execute(
        "insert into gsc_daily (site_id, date, page, query, clicks, impressions, ctr, position)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (site_id, day, page, query, clicks, impressions,
         clicks / impressions if impressions else 0, position),
    )


def _window(conn, site_id, page, query, clicks, impressions, position, window_days=28):
    conn.execute(
        "insert into gsc_window_agg"
        " (site_id, window_days, page, query, clicks, impressions, ctr, position)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (site_id, window_days, page, query, clicks, impressions,
         clicks / impressions if impressions else 0, position),
    )


def _page_day(conn, site_id, day, page, clicks):
    conn.execute(
        "insert into gsc_page_daily (site_id, date, page, clicks, impressions)"
        " values (%s, %s, %s, %s, %s)",
        (site_id, day, page, clicks, clicks * 10),
    )


def _items(conn, site_id, kind=None):
    q = "select * from queue_items where site_id = %s"
    params = [site_id]
    if kind:
        q += " and kind = %s"
        params.append(kind)
    return conn.execute(q + " order by kind, target_hash", params).fetchall()


def _targets(rows):
    return {detectors.canonical_target(r["target"]) for r in rows}


# --- basis selection: provisional vs final --------------------------------------------


@needs_db
def test_basis_boundary_27_vs_28_final_days(conn, site):
    s = site["site_id"]
    _window(conn, s, "https://e.x/p", "kw", 20, 1000, 8.0)
    # 27 final days -> provisional
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=26), ANCHOR)
    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "provisional"
    assert result["skipped"] == {
        "decay": "insufficient history",
        "cannibalization": "insufficient history",
    }
    assert "decay" not in result["counts"]
    assert result["counts"]["striking_distance"] == 1

    # the 28th final day flips to final mode
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=27), ANCHOR)
    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "final"
    assert result["skipped"] == {}
    assert set(result["counts"]) == {
        "striking_distance", "ctr_outlier", "decay", "cannibalization",
        "local_presence",  # D3: runs on either basis (SERP data, not GSC)
    }


@needs_db
def test_final_mode_reads_gsc_daily_not_window_agg(conn, site):
    s = site["site_id"]
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=27), ANCHOR)
    # qualifying row only present in gsc_daily
    _daily(conn, s, ANCHOR, "https://e.x/real", "real-kw", 20, 1000, 8.0)
    # qualifying row only present in gsc_window_agg: must be IGNORED in final mode
    _window(conn, s, "https://e.x/stale", "stale-kw", 20, 1000, 8.0)

    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "final"
    items = _items(conn, s, "striking_distance")
    assert _targets(items) == {'{"page":"https://e.x/real","query":"real-kw"}'}
    assert items[0]["at_stake"]["basis"] == "final"


@needs_db
def test_provisional_mode_reads_window_agg(conn, site):
    s = site["site_id"]  # no ingest log at all
    _window(conn, s, "https://e.x/p", "kw", 20, 1000, 8.0)
    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "provisional"
    items = _items(conn, s, "striking_distance")
    assert len(items) == 1
    assert items[0]["at_stake"]["basis"] == "provisional"


# --- striking distance ----------------------------------------------------------------


@needs_db
def test_striking_distance_thresholds_and_gain(conn, site):
    s = site["site_id"]
    _window(conn, s, "https://e.x/a", "kw-4.99", 20, 1000, 4.99)   # below 5: out
    _window(conn, s, "https://e.x/b", "kw-5", 20, 1000, 5.0)       # boundary: in
    _window(conn, s, "https://e.x/c", "kw-20", 20, 1000, 20.0)     # boundary: in
    _window(conn, s, "https://e.x/d", "kw-20.5", 20, 1000, 20.5)   # above 20: out
    _window(conn, s, "https://e.x/e", "kw-99imp", 2, 99, 8.0)      # impressions 99: out
    _window(conn, s, "https://e.x/f", "kw-100imp", 2, 100, 8.0)    # boundary: in

    result = detectors.compute_queue(conn, s)
    assert result["counts"]["striking_distance"] == 3
    items = _items(conn, s, "striking_distance")
    assert {r["target"]["query"] for r in items} == {"kw-5", "kw-20", "kw-100imp"}

    # gain math: impressions * (ctr@3 - ctr_now) = 1000 * (0.09 - 0.02) = 70
    by_query = {r["target"]["query"]: r for r in items}
    assert by_query["kw-5"]["at_stake"]["est_clicks_gain"] == pytest.approx(70.0)
    # target_hash persisted matches the canonical identity hash
    assert by_query["kw-5"]["target_hash"] == detectors.target_hash(
        {"page": "https://e.x/b", "query": "kw-5"}
    )


# --- ctr outlier ----------------------------------------------------------------------


@needs_db
def test_ctr_outlier_half_expected_boundary_and_gain(conn, site):
    s = site["site_id"]
    # position 3 expects 0.09; half = 0.045
    _window(conn, s, "https://e.x/a", "kw-at-half", 45, 1000, 3.0)     # == half: NOT flagged
    _window(conn, s, "https://e.x/b", "kw-below-half", 44, 1000, 3.0)  # < half: flagged
    _window(conn, s, "https://e.x/c", "kw-pos6", 1, 1000, 6.0)         # position > 5: out
    _window(conn, s, "https://e.x/d", "kw-thin", 1, 99, 3.0)           # impressions floor: out

    result = detectors.compute_queue(conn, s)
    assert result["counts"]["ctr_outlier"] == 1
    items = _items(conn, s, "ctr_outlier")
    assert items[0]["target"] == {"page": "https://e.x/b", "query": "kw-below-half"}
    # gain = impressions * (expected - ctr) = 1000 * (0.09 - 0.044) = 46
    assert items[0]["at_stake"]["est_clicks_gain"] == pytest.approx(46.0, abs=0.01)
    assert items[0]["at_stake"]["expected_ctr"] == pytest.approx(0.09)


# --- decay ----------------------------------------------------------------------------


@needs_db
def test_decay_25pct_boundary_yoy_and_noise_floor(conn, site):
    s = site["site_id"]
    cur, prior = ANCHOR, ANCHOR - dt.timedelta(days=40)
    yoy = ANCHOR - dt.timedelta(days=370)
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=55), ANCHOR)          # cur + prior
    _plant_final_days(
        conn, s, ANCHOR - dt.timedelta(days=391), ANCHOR - dt.timedelta(days=364)
    )  # yoy window

    _page_day(conn, s, prior, "https://e.x/drop25", 400)   # (400-300)/400 = 25%: flagged
    _page_day(conn, s, cur, "https://e.x/drop25", 300)
    _page_day(conn, s, prior, "https://e.x/drop24", 400)   # 24.75%: not flagged
    _page_day(conn, s, cur, "https://e.x/drop24", 301)
    _page_day(conn, s, prior, "https://e.x/yoydrop", 310)  # prior drop 3%: no
    _page_day(conn, s, yoy, "https://e.x/yoydrop", 400)    # yoy drop 25%: flagged
    _page_day(conn, s, cur, "https://e.x/yoydrop", 300)
    _page_day(conn, s, prior, "https://e.x/tiny", 20)      # 50% drop but base < 30: noise
    _page_day(conn, s, cur, "https://e.x/tiny", 10)

    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "final"
    assert result["counts"]["decay"] == 2
    items = _items(conn, s, "decay")
    by_page = {r["target"]["page"]: r for r in items}
    assert set(by_page) == {"https://e.x/drop25", "https://e.x/yoydrop"}
    assert by_page["https://e.x/drop25"]["at_stake"]["est_clicks_gain"] == pytest.approx(100.0)
    assert by_page["https://e.x/drop25"]["at_stake"]["drop_pct"] == pytest.approx(0.25)
    assert by_page["https://e.x/yoydrop"]["at_stake"]["yoy_drop_pct"] == pytest.approx(0.25)
    assert by_page["https://e.x/yoydrop"]["target_hash"] == detectors.target_hash(
        {"page": "https://e.x/yoydrop"}
    )


@needs_db
def test_decay_skips_uncovered_prior_window(conn, site):
    s = site["site_id"]
    # only the current 28 days are final: no baseline coverage -> no fake drops
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=27), ANCHOR)
    _page_day(conn, s, ANCHOR - dt.timedelta(days=40), "https://e.x/p", 400)
    _page_day(conn, s, ANCHOR, "https://e.x/p", 10)
    result = detectors.compute_queue(conn, s)
    assert result["basis"] == "final"
    assert result["counts"]["decay"] == 0


# --- cannibalization ------------------------------------------------------------------


@needs_db
def test_cannibalization_20pct_share_boundary(conn, site):
    s = site["site_id"]
    _plant_final_days(conn, s, ANCHOR - dt.timedelta(days=27), ANCHOR)
    # q-split: shares 0.8 / 0.2 -> both qualify (boundary inclusive) -> flagged
    _daily(conn, s, ANCHOR, "https://e.x/p1", "q-split", 40, 800, 2.0)
    _daily(conn, s, ANCHOR, "https://e.x/p2", "q-split", 2, 200, 9.0)
    # q-mono: shares 0.81 / 0.19 -> one qualifying page -> not flagged
    _daily(conn, s, ANCHOR, "https://e.x/p1", "q-mono", 40, 810, 2.0)
    _daily(conn, s, ANCHOR, "https://e.x/p2", "q-mono", 2, 190, 9.0)
    # q-thin: total impressions 90 < 100 -> not flagged
    _daily(conn, s, ANCHOR, "https://e.x/p1", "q-thin", 2, 50, 2.0)
    _daily(conn, s, ANCHOR, "https://e.x/p2", "q-thin", 2, 40, 9.0)

    result = detectors.compute_queue(conn, s)
    assert result["counts"]["cannibalization"] == 1
    items = _items(conn, s, "cannibalization")
    item = items[0]
    assert item["target"] == {"query": "q-split"}
    assert item["target_hash"] == detectors.target_hash({"query": "q-split"})
    stake = item["at_stake"]
    assert stake["impressions_total"] == 1000
    assert [p["page"] for p in stake["pages"]] == ["https://e.x/p1", "https://e.x/p2"]
    assert stake["pages"][1]["share"] == pytest.approx(0.2)
    # gain = 1000 * expected_ctr(2) - 42 clicks = 130 - 42 = 88
    assert stake["est_clicks_gain"] == pytest.approx(88.0)


# --- upsert discipline ----------------------------------------------------------------


def _detect_once(conn, site, impressions=1000, clicks=20):
    rows = [{
        "page": "https://e.x/p", "query": "kw", "clicks": clicks,
        "impressions": impressions, "ctr": clicks / impressions, "position": 8.0,
    }]
    return detectors.striking_distance(
        conn, org_id=site["org_id"], site_id=site["site_id"], rows=rows, basis="provisional"
    )


@needs_db
def test_upsert_refreshes_open_rows(conn, site):
    assert _detect_once(conn, site, impressions=1000) == 1
    first = _items(conn, site["site_id"])[0]
    assert first["status"] == "open"

    assert _detect_once(conn, site, impressions=2000) == 1
    rows = _items(conn, site["site_id"])
    assert len(rows) == 1  # same identity -> same row
    second = rows[0]
    assert second["id"] == first["id"]
    assert second["at_stake"]["impressions"] == 2000       # at_stake refreshed
    assert second["last_seen"] >= first["last_seen"]
    assert second["first_seen"] == first["first_seen"]     # history preserved


@needs_db
def test_upsert_never_touches_actioned_or_done(conn, site):
    _detect_once(conn, site, impressions=1000)
    for status in ("actioned", "done"):
        conn.execute(
            "update queue_items set status = %s where site_id = %s", (status, site["site_id"])
        )
        before = _items(conn, site["site_id"])[0]
        _detect_once(conn, site, impressions=3000)
        after = _items(conn, site["site_id"])[0]
        assert after["status"] == status
        assert after["at_stake"] == before["at_stake"]
        assert after["last_seen"] == before["last_seen"]


@needs_db
def test_upsert_dismissed_snooze_semantics(conn, site):
    _detect_once(conn, site, impressions=1000)
    sid = site["site_id"]

    # dismissed with a future snooze: untouched
    conn.execute(
        "update queue_items set status='dismissed', snooze_until = now() + interval '1 hour'"
        " where site_id = %s", (sid,),
    )
    _detect_once(conn, site, impressions=2000)
    row = _items(conn, sid)[0]
    assert row["status"] == "dismissed"
    assert row["at_stake"]["impressions"] == 1000

    # dismissed with NULL snooze: dismissed forever, untouched
    conn.execute(
        "update queue_items set snooze_until = null where site_id = %s", (sid,)
    )
    _detect_once(conn, site, impressions=2000)
    row = _items(conn, sid)[0]
    assert row["status"] == "dismissed"
    assert row["at_stake"]["impressions"] == 1000

    # dismissed with an elapsed snooze: reopens with fresh at_stake
    conn.execute(
        "update queue_items set snooze_until = now() - interval '1 second'"
        " where site_id = %s", (sid,),
    )
    _detect_once(conn, site, impressions=2000)
    row = _items(conn, sid)[0]
    assert row["status"] == "open"
    assert row["snooze_until"] is None
    assert row["at_stake"]["impressions"] == 2000


@needs_db
def test_vanished_targets_leave_history_rows(conn, site):
    _detect_once(conn, site)  # target kw
    rows = [{
        "page": "https://e.x/other", "query": "other-kw", "clicks": 20,
        "impressions": 1000, "ctr": 0.02, "position": 8.0,
    }]
    detectors.striking_distance(
        conn, org_id=site["org_id"], site_id=site["site_id"], rows=rows, basis="provisional"
    )
    items = _items(conn, site["site_id"])
    assert len(items) == 2  # old target's row is preserved, not deleted


# --- job handler ----------------------------------------------------------------------


@needs_db
def test_handle_compute_queue(conn, site):
    _window(conn, site["site_id"], "https://e.x/p", "kw", 20, 1000, 8.0)
    now = dt.datetime.now(dt.UTC)
    job = jobs.JobRow(
        id=1, type="compute_queue", org_id=site["org_id"], site_id=site["site_id"],
        payload={}, status="running", priority=5, run_after=now, attempts=1,
        max_attempts=3, idempotency_key=None, concurrency_key=None, locked_by="w",
        locked_until=None, last_error=None, created_at=now, finished_at=None,
    )
    detectors.handle_compute_queue(jobs.JobContext(job, conn, "w", 60))
    assert len(_items(conn, site["site_id"], "striking_distance")) == 1


@needs_db
def test_handle_compute_queue_requires_site(conn, site):
    now = dt.datetime.now(dt.UTC)
    job = jobs.JobRow(
        id=1, type="compute_queue", org_id=site["org_id"], site_id=None,
        payload={}, status="running", priority=5, run_after=now, attempts=1,
        max_attempts=3, idempotency_key=None, concurrency_key=None, locked_by="w",
        locked_until=None, last_error=None, created_at=now, finished_at=None,
    )
    with pytest.raises(RuntimeError, match="site_id"):
        detectors.handle_compute_queue(jobs.JobContext(job, conn, "w", 60))
