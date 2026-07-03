"""Tests for the two-phase GSC ingest.

Pure date/partition math runs everywhere; DB-backed tests (slice replacement, rollups,
handlers) skip cleanly when DATABASE_URL is unset. ZERO network: a fake in-memory client
stands in for GscClient, and the credential loader is monkeypatched in handler tests.
"""

import datetime as dt
import os
import uuid
from types import SimpleNamespace

import pytest

from gm.intel import gsc_ingest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


# --- fakes -----------------------------------------------------------------------------


def gsc_row(page: str, query: str, clicks: int, impressions: int, position: float) -> dict:
    return {
        "keys": [page, query],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0.0,
        "position": position,
    }


class FakeGsc:
    """Duck-typed GSC client: canned rows per day and per whole-window length."""

    def __init__(self, rows_by_day=None, window_rows=None, page_size=2):
        self.rows_by_day = rows_by_day or {}
        self.window_rows = window_rows or {}
        self.page_size = page_size  # tiny page size so pagination is actually exercised
        self.calls: list[dict] = []

    def query(self, *, start_date, end_date, dimensions, row_limit=25000, start_row=0,
              search_type="web", data_state="final"):
        self.calls.append({"start": start_date, "end": end_date, "start_row": start_row,
                           "data_state": data_state})
        if start_date == end_date:
            rows = self.rows_by_day.get(start_date, [])
        else:
            rows = self.window_rows.get((end_date - start_date).days + 1, [])
        return rows[start_row:start_row + row_limit]

    def query_all(self, **kw):
        start_row = 0
        while True:
            page = self.query(**{**kw, "start_row": start_row, "row_limit": self.page_size})
            if not page:
                return
            yield page
            start_row += self.page_size


class FakeCtx:
    """Just enough of jobs.JobContext for the handlers."""

    def __init__(self, conn, site_id, payload=None, org_id=None):
        self.conn = conn
        self.job = SimpleNamespace(id=1, site_id=site_id, org_id=org_id, payload=payload or {})
        self.beats = 0

    def heartbeat(self):
        self.beats += 1


# --- pure date / partition math (no DB) -------------------------------------------------


def test_partition_name():
    assert gsc_ingest.partition_name(dt.date(2026, 7, 1)) == "gsc_daily_y2026m07"
    assert gsc_ingest.partition_name(dt.date(2025, 11, 30)) == "gsc_daily_y2025m11"


def test_month_bounds_mid_month_and_december():
    assert gsc_ingest.month_bounds(dt.date(2026, 7, 15)) == (
        dt.date(2026, 7, 1), dt.date(2026, 8, 1)
    )
    assert gsc_ingest.month_bounds(dt.date(2026, 12, 31)) == (
        dt.date(2026, 12, 1), dt.date(2027, 1, 1)
    )


def test_is_final_boundary():
    today = dt.date(2026, 7, 3)
    assert gsc_ingest.is_final(today - dt.timedelta(days=4), today=today) is True
    assert gsc_ingest.is_final(today - dt.timedelta(days=3), today=today) is False
    assert gsc_ingest.is_final(today, today=today) is False


def test_months_ago_clamps_day_of_month():
    assert gsc_ingest.months_ago(dt.date(2026, 3, 31), 1) == dt.date(2026, 2, 28)
    assert gsc_ingest.months_ago(dt.date(2024, 3, 31), 1) == dt.date(2024, 2, 29)  # leap
    assert gsc_ingest.months_ago(dt.date(2026, 7, 3), 16) == dt.date(2025, 3, 3)
    assert gsc_ingest.months_ago(dt.date(2026, 1, 15), 1) == dt.date(2025, 12, 15)


def test_window_range_ends_at_final_lag():
    start, end = gsc_ingest.window_range(28, today=dt.date(2026, 7, 3))
    assert end == dt.date(2026, 6, 30)
    assert (end - start).days + 1 == 28


def test_next_backfill_run_is_tomorrow_0600_utc():
    run = gsc_ingest.next_backfill_run(dt.date(2026, 7, 3))
    assert run == dt.datetime(2026, 7, 4, 6, 0, tzinfo=dt.UTC)


# --- DB-backed tests ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate gsc_daily, gsc_window_agg, gsc_page_daily, gsc_ingest_log")
        c.execute("truncate quota_ledgers")
        c.execute("truncate jobs restart identity")
        yield c


@pytest.fixture()
def site_id():
    return uuid.uuid4()


def _daily_rows(conn, site_id):
    return conn.execute(
        "select date, search_type, page, query, clicks, impressions, ctr, position"
        " from gsc_daily where site_id = %s order by date, page, query",
        (site_id,),
    ).fetchall()


def _log_row(conn, site_id, day):
    return conn.execute(
        "select * from gsc_ingest_log where site_id = %s and date = %s and search_type = 'web'",
        (site_id, day),
    ).fetchone()


def _prefill_log(conn, site_id, start, end, *, final=True):
    conn.execute(
        "insert into gsc_ingest_log (site_id, date, search_type, rows, final)"
        " select %s, d::date, 'web', 1, %s from generate_series(%s::date, %s::date,"
        " interval '1 day') d",
        (site_id, final, start, end),
    )


@requires_db
class TestPartitions:
    def test_ensure_partition_idempotent_and_named(self, conn):
        month = dt.date(2031, 1, 1)
        gsc_ingest.ensure_partition(conn, month)
        gsc_ingest.ensure_partition(conn, dt.date(2031, 1, 20))  # any day of the month
        n = conn.execute(
            "select count(*) as n from pg_class where relname = 'gsc_daily_y2031m01'"
        ).fetchone()["n"]
        assert n == 1
        # the partition accepts rows for its month
        conn.execute(
            "insert into gsc_daily (site_id, date, search_type, page, query)"
            " values (%s, '2031-01-15', 'web', '/p', 'q')",
            (uuid.uuid4(),),
        )


@requires_db
class TestPullDay:
    def test_slice_replacement_idempotent(self, conn, site_id):
        day = gsc_ingest._utc_today() - dt.timedelta(days=10)
        rows = [
            gsc_row("/a", "q1", 3, 100, 5.0),
            gsc_row("/a", "q2", 1, 300, 9.0),
            gsc_row("/b", "q1", 0, 50, 12.0),
        ]
        fake = FakeGsc(rows_by_day={day: rows})  # page_size=2 -> 2 pages, proves pagination
        n1 = gsc_ingest.pull_day(conn, site_id, fake, day)
        first = _daily_rows(conn, site_id)
        n2 = gsc_ingest.pull_day(conn, site_id, fake, day)
        second = _daily_rows(conn, site_id)

        assert n1 == n2 == 3
        assert first == second  # two pulls of the same day leave identical rows
        rollup1 = conn.execute(
            "select * from gsc_page_daily where site_id = %s order by page", (site_id,)
        ).fetchall()
        gsc_ingest.pull_day(conn, site_id, fake, day)
        rollup2 = conn.execute(
            "select * from gsc_page_daily where site_id = %s order by page", (site_id,)
        ).fetchall()
        assert rollup1 == rollup2

    def test_slice_replacement_drops_stale_rows(self, conn, site_id):
        day = gsc_ingest._utc_today() - dt.timedelta(days=10)
        fake = FakeGsc(rows_by_day={day: [gsc_row("/old", "q", 1, 10, 3.0)]})
        gsc_ingest.pull_day(conn, site_id, fake, day)
        fake.rows_by_day[day] = [gsc_row("/new", "q", 2, 20, 4.0)]
        gsc_ingest.pull_day(conn, site_id, fake, day)

        rows = _daily_rows(conn, site_id)
        assert [r["page"] for r in rows] == ["/new"]
        pages = conn.execute(
            "select page from gsc_page_daily where site_id = %s", (site_id,)
        ).fetchall()
        assert [r["page"] for r in pages] == ["/new"]
        assert _log_row(conn, site_id, day)["rows"] == 1

    def test_rollup_impression_weighted_position(self, conn, site_id):
        day = gsc_ingest._utc_today() - dt.timedelta(days=10)
        fake = FakeGsc(rows_by_day={day: [
            gsc_row("/a", "q1", 3, 100, 5.0),
            gsc_row("/a", "q2", 1, 300, 9.0),
            gsc_row("/b", "q1", 0, 0, 7.0),  # zero impressions -> plain-mean fallback
        ]})
        gsc_ingest.pull_day(conn, site_id, fake, day)
        rollup = {r["page"]: r for r in conn.execute(
            "select * from gsc_page_daily where site_id = %s", (site_id,)
        ).fetchall()}
        a = rollup["/a"]
        assert a["clicks"] == 4
        assert a["impressions"] == 400
        assert a["position"] == pytest.approx((100 * 5.0 + 300 * 9.0) / 400)  # 8.0
        assert rollup["/b"]["position"] == pytest.approx(7.0)

    def test_final_flag_boundary(self, conn, site_id):
        today = gsc_ingest._utc_today()
        final_day = today - dt.timedelta(days=4)
        fresh_day = today - dt.timedelta(days=3)
        fake = FakeGsc(rows_by_day={
            final_day: [gsc_row("/a", "q", 1, 10, 2.0)],
            fresh_day: [gsc_row("/a", "q", 1, 10, 2.0)],
        })
        gsc_ingest.pull_day(conn, site_id, fake, final_day)
        gsc_ingest.pull_day(conn, site_id, fake, fresh_day)
        assert _log_row(conn, site_id, final_day)["final"] is True
        assert _log_row(conn, site_id, fresh_day)["final"] is False

    def test_quota_bookkeeping(self, conn, site_id):
        from gm.infra import costs

        day = gsc_ingest._utc_today() - dt.timedelta(days=10)
        fake = FakeGsc(rows_by_day={day: [gsc_row(f"/p{i}", "q", 0, 1, 1.0) for i in range(5)]})
        gsc_ingest.pull_day(conn, site_id, fake, day)
        assert costs.quota_used(conn, "gsc_rows", str(site_id)) == 5
        gsc_ingest.pull_day(conn, site_id, fake, day)  # re-pull still counts API rows
        assert costs.quota_used(conn, "gsc_rows", str(site_id)) == 10


@requires_db
class TestInitialPull:
    def test_window_slices_replaced(self, conn, site_id):
        fake = FakeGsc(window_rows={
            28: [gsc_row("/a", "q1", 5, 100, 4.0), gsc_row("/b", "q2", 2, 50, 8.0)],
            90: [gsc_row("/a", "q1", 20, 900, 4.5)],
        })
        out = gsc_ingest.initial_pull(conn, str(site_id), fake)
        assert out == {"rows_28": 2, "rows_90": 1}

        # re-pull with new data replaces, never accumulates
        fake.window_rows[28] = [gsc_row("/c", "q3", 1, 10, 2.0)]
        out = gsc_ingest.initial_pull(conn, str(site_id), fake)
        assert out == {"rows_28": 1, "rows_90": 1}
        rows = conn.execute(
            "select window_days, page from gsc_window_agg where site_id = %s"
            " order by window_days, page",
            (site_id,),
        ).fetchall()
        assert [(r["window_days"], r["page"]) for r in rows] == [(28, "/c"), (90, "/a")]


@requires_db
class TestBackfillPlan:
    def test_empty_log_covers_retention_newest_first(self, conn, site_id):
        today = dt.date(2026, 7, 3)
        plan = gsc_ingest.backfill_plan(conn, str(site_id), today=today)
        assert plan[0] == dt.date(2026, 7, 1)  # today-2
        assert plan[-1] == dt.date(2025, 3, 3)  # 16 months back
        assert len(plan) == (plan[0] - plan[-1]).days + 1
        assert plan == sorted(plan, reverse=True)

    def test_gaps_and_unfinal_days(self, conn, site_id):
        today = dt.date(2026, 7, 3)
        oldest = gsc_ingest.months_ago(today, 16)
        _prefill_log(conn, site_id, oldest, today - dt.timedelta(days=2), final=True)
        # two missing days (a gap) + one stale non-final old day + one non-final recent day
        gap = [dt.date(2026, 6, 11), dt.date(2026, 6, 10)]
        conn.execute(
            "delete from gsc_ingest_log where site_id = %s and date = any(%s)", (site_id, gap)
        )
        conn.execute(
            "update gsc_ingest_log set final = false where site_id = %s and date = any(%s)",
            (site_id, [dt.date(2026, 5, 1), dt.date(2026, 7, 1)]),
        )
        plan = gsc_ingest.backfill_plan(conn, str(site_id), today=today)
        # 2026-07-01 (today-2, non-final) is the daily job's business, not the backfill's
        assert plan == [dt.date(2026, 6, 11), dt.date(2026, 6, 10), dt.date(2026, 5, 1)]


@requires_db
class TestHandlers:
    @pytest.fixture()
    def marks(self, monkeypatch):
        calls = {"ok": [], "broken": []}
        monkeypatch.setattr(gsc_ingest, "_mark_ok", lambda conn, cid: calls["ok"].append(cid))
        monkeypatch.setattr(
            gsc_ingest, "_mark_broken", lambda org, cid, err: calls["broken"].append((cid, err))
        )
        return calls

    def _patch_client(self, monkeypatch, fake):
        monkeypatch.setattr(gsc_ingest, "_load_client", lambda conn, site_id: ("conn-1", fake))

    def _backfill_jobs(self, conn):
        return conn.execute(
            "select * from jobs where type = 'gsc_backfill' order by id"
        ).fetchall()

    def test_gsc_initial_enqueues_compute_and_first_batch(
        self, conn, site_id, monkeypatch, marks
    ):
        fake = FakeGsc(window_rows={28: [gsc_row("/a", "q", 1, 10, 3.0)], 90: []})
        self._patch_client(monkeypatch, fake)
        gsc_ingest.handle_gsc_initial(FakeCtx(conn, site_id))

        agg = conn.execute(
            "select count(*) as n from gsc_window_agg where site_id = %s", (site_id,)
        ).fetchone()["n"]
        assert agg == 1
        assert marks["ok"] == ["conn-1"]

        compute = conn.execute("select * from jobs where type = 'compute_queue'").fetchall()
        assert len(compute) == 1

        (batch,) = self._backfill_jobs(conn)
        days = [dt.date.fromisoformat(d) for d in batch["payload"]["days"]]
        assert len(days) == gsc_ingest.BACKFILL_BATCH_DAYS
        assert days[0] == gsc_ingest._utc_today() - dt.timedelta(days=2)  # newest first
        assert days == sorted(days, reverse=True)

    def test_backfill_pulls_days_and_stops_when_done(self, conn, site_id, monkeypatch, marks):
        today = gsc_ingest._utc_today()
        d1, d2 = today - dt.timedelta(days=10), today - dt.timedelta(days=11)
        _prefill_log(conn, site_id, gsc_ingest.months_ago(today, 16),
                     today - dt.timedelta(days=2))
        conn.execute(
            "delete from gsc_ingest_log where site_id = %s and date = any(%s)",
            (site_id, [d1, d2]),
        )
        fake = FakeGsc(rows_by_day={
            d1: [gsc_row("/a", "q", 1, 10, 3.0)],
            d2: [gsc_row("/b", "q", 2, 20, 6.0)],
        })
        self._patch_client(monkeypatch, fake)
        ctx = FakeCtx(conn, site_id, payload={"days": [d1.isoformat(), d2.isoformat()]})
        gsc_ingest.handle_gsc_backfill(ctx)

        assert ctx.beats == 2  # heartbeat between days
        assert _log_row(conn, site_id, d1)["final"] is True
        assert _log_row(conn, site_id, d2)["final"] is True
        assert self._backfill_jobs(conn) == []  # plan empty: chain ends
        assert marks["ok"] == ["conn-1"]

    def test_backfill_throttles_at_ledger_threshold(self, conn, site_id, monkeypatch, marks):
        from gm.infra import costs

        today = gsc_ingest._utc_today()
        d1, d2 = today - dt.timedelta(days=10), today - dt.timedelta(days=11)
        _prefill_log(conn, site_id, gsc_ingest.months_ago(today, 16),
                     today - dt.timedelta(days=2))
        conn.execute(
            "delete from gsc_ingest_log where site_id = %s and date = any(%s)",
            (site_id, [d1, d2]),
        )
        # after d1's 10 rows the ledger crosses 45k -> d2 must not be pulled
        costs.bump_quota(conn, "gsc_rows", str(site_id), gsc_ingest.THROTTLE_ROWS - 5)
        fake = FakeGsc(rows_by_day={
            d1: [gsc_row(f"/p{i}", "q", 0, 1, 1.0) for i in range(10)],
            d2: [gsc_row("/b", "q", 2, 20, 6.0)],
        })
        self._patch_client(monkeypatch, fake)
        ctx = FakeCtx(conn, site_id, payload={"days": [d1.isoformat(), d2.isoformat()]})
        gsc_ingest.handle_gsc_backfill(ctx)

        assert _log_row(conn, site_id, d1) is not None
        assert _log_row(conn, site_id, d2) is None  # stopped early

        (nxt,) = self._backfill_jobs(conn)
        assert [dt.date.fromisoformat(d) for d in nxt["payload"]["days"]] == [d2]
        run_after = nxt["run_after"].astimezone(dt.UTC)
        assert run_after == gsc_ingest.next_backfill_run()  # tomorrow 06:00 UTC

    def test_backfill_throttled_before_first_day(self, conn, site_id, monkeypatch, marks):
        from gm.infra import costs

        today = gsc_ingest._utc_today()
        d1 = today - dt.timedelta(days=10)
        _prefill_log(conn, site_id, gsc_ingest.months_ago(today, 16),
                     today - dt.timedelta(days=2))
        conn.execute(
            "delete from gsc_ingest_log where site_id = %s and date = %s", (site_id, d1)
        )
        costs.bump_quota(conn, "gsc_rows", str(site_id), gsc_ingest.THROTTLE_ROWS + 1)
        fake = FakeGsc(rows_by_day={d1: [gsc_row("/a", "q", 1, 10, 3.0)]})
        self._patch_client(monkeypatch, fake)
        gsc_ingest.handle_gsc_backfill(
            FakeCtx(conn, site_id, payload={"days": [d1.isoformat()]})
        )

        assert _daily_rows(conn, site_id) == []  # nothing pulled at all
        (nxt,) = self._backfill_jobs(conn)
        assert nxt["payload"]["days"] == [d1.isoformat()]
        assert nxt["run_after"].astimezone(dt.UTC) == gsc_ingest.next_backfill_run()

    def test_gsc_daily_repulls_trailing_window(self, conn, site_id, monkeypatch, marks):
        today = gsc_ingest._utc_today()
        window = [today - dt.timedelta(days=o) for o in (4, 3, 2)]
        fake = FakeGsc(rows_by_day={d: [gsc_row("/a", "q", 1, 10, 3.0)] for d in window})
        self._patch_client(monkeypatch, fake)
        ctx = FakeCtx(conn, site_id)
        gsc_ingest.handle_gsc_daily(ctx)

        assert ctx.beats == 3
        assert _log_row(conn, site_id, window[0])["final"] is True  # today-4
        assert _log_row(conn, site_id, window[1])["final"] is False  # today-3
        assert _log_row(conn, site_id, window[2])["final"] is False  # today-2
        compute = conn.execute("select * from jobs where type = 'compute_queue'").fetchall()
        assert len(compute) == 1
        assert compute[0]["idempotency_key"] == f"compute_queue:{site_id}:{today.isoformat()}"
        assert marks["ok"] == ["conn-1"]
