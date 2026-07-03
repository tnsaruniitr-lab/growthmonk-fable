"""Two-phase GSC ingest (Phase C wave 1).

Phase 1 (minutes after connect): two whole-window aggregate pulls into gsc_window_agg so
detectors can run in provisional mode immediately. Phase 2 (hours/days): per-day paginated
slices into the month-partitioned gsc_daily table plus the gsc_page_daily rollup, driven by
backfill jobs that throttle against the shared gsc_rows quota ledger.

Slice replacement everywhere: DELETE the (site, date[, search_type]) slice, re-INSERT, and
recompute the rollup slice — re-pulls are idempotent by construction, no upsert index needed.

The GSC HTTP client is duck-typed (GscSearchAnalytics protocol): gm.connections.gsc.GscClient
is imported lazily inside _load_client only, so this module never touches google-auth at
import time and tests inject fake clients.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterator
from typing import Any, Protocol

import psycopg
from psycopg import sql

from gm import db
from gm.infra import costs, jobs

# Data older than this many days is final in GSC; newer rows are still subject to revision.
FINAL_LAG_DAYS = 3
# GSC searchanalytics page size (client paginates in steps of this).
PAGE_ROW_LIMIT = 25_000
# Self-imposed cap on rows ingested per site per day; tracked in the gsc_rows quota ledger.
DAILY_ROW_CAP = 50_000
# Backfill batches stop early once the ledger goes past this, leaving headroom under the cap.
THROTTLE_ROWS = 45_000
# Days per gsc_backfill job payload.
BACKFILL_BATCH_DAYS = 30
# GSC retains 16 months of search analytics data.
BACKFILL_MONTHS = 16
QUOTA_PORT = "gsc_rows"
DIMENSIONS = ["page", "query"]


class GscSearchAnalytics(Protocol):
    """Duck-typed slice of gm.connections.gsc.GscClient used by the ingest."""

    def query(
        self,
        *,
        start_date: dt.date,
        end_date: dt.date,
        dimensions: list[str],
        row_limit: int = PAGE_ROW_LIMIT,
        start_row: int = 0,
        search_type: str = "web",
        data_state: str = "final",
    ) -> list[dict]: ...

    def query_all(self, **kw: Any) -> Iterator[list[dict]]: ...


# --- pure date helpers ------------------------------------------------------------------


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def partition_name(month_start: dt.date) -> str:
    """Deterministic partition name for the month containing month_start."""
    return f"gsc_daily_y{month_start.year:04d}m{month_start.month:02d}"


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    """[first of month, first of next month) — the partition's FROM/TO bounds."""
    start = day.replace(day=1)
    if start.month == 12:
        return start, dt.date(start.year + 1, 1, 1)
    return start, dt.date(start.year, start.month + 1, 1)


def is_final(day: dt.date, *, today: dt.date | None = None) -> bool:
    """GSC data for a day is final once the day is more than FINAL_LAG_DAYS old."""
    today = today or _utc_today()
    return day < today - dt.timedelta(days=FINAL_LAG_DAYS)


def months_ago(day: dt.date, months: int) -> dt.date:
    """Calendar-month subtraction with day-of-month clamping (Mar 31 - 1mo -> Feb 28/29)."""
    total = day.year * 12 + day.month - 1 - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def window_range(days: int, *, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Inclusive [start, end] of a whole-window pull: `days` days ending today-FINAL_LAG."""
    today = today or _utc_today()
    end = today - dt.timedelta(days=FINAL_LAG_DAYS)
    return end - dt.timedelta(days=days - 1), end


def next_backfill_run(today: dt.date | None = None) -> dt.datetime:
    """Throttled batches resume tomorrow 06:00 UTC."""
    today = today or _utc_today()
    return dt.datetime.combine(today + dt.timedelta(days=1), dt.time(6, 0), tzinfo=dt.UTC)


def _norm_row(row: dict) -> tuple[str, str, int, int, float, float]:
    """API row {"keys": [page, query], clicks, impressions, ctr, position} -> flat tuple."""
    keys = row.get("keys") or ["", ""]
    return (
        keys[0],
        keys[1],
        int(row.get("clicks", 0)),
        int(row.get("impressions", 0)),
        float(row.get("ctr", 0.0)),
        float(row.get("position", 0.0)),
    )


# --- partitions ---------------------------------------------------------------------------


def ensure_partition(conn: psycopg.Connection, month_start: dt.date) -> None:
    """Idempotently create the monthly gsc_daily partition covering month_start."""
    start, end = month_bounds(month_start)
    conn.execute(
        sql.SQL(
            "create table if not exists {} partition of gsc_daily"
            " for values from ({}) to ({})"
        ).format(sql.Identifier(partition_name(start)), sql.Literal(start), sql.Literal(end))
    )


# --- phase 1: whole-window aggregates -----------------------------------------------------


def initial_pull(
    conn: psycopg.Connection,
    site_id: str,
    gsc: GscSearchAnalytics,
    *,
    today: dt.date | None = None,
) -> dict:
    """Two whole-window pulls (28d/90d ending today-3), first page only (top 25k by clicks).

    Slice-replaces gsc_window_agg per (site_id, window_days) so re-connects are idempotent.
    """
    out: dict[str, int] = {}
    for days in (28, 90):
        start, end = window_range(days, today=today)
        rows = gsc.query(
            start_date=start, end_date=end, dimensions=DIMENSIONS, row_limit=PAGE_ROW_LIMIT
        )
        parsed = [_norm_row(r) for r in rows]
        conn.execute(
            "delete from gsc_window_agg where site_id = %s and window_days = %s",
            (site_id, days),
        )
        with conn.cursor() as cur, cur.copy(
            "copy gsc_window_agg"
            " (site_id, window_days, page, query, clicks, impressions, ctr, position)"
            " from stdin"
        ) as copy:
            for page, query, clicks, impressions, ctr, position in parsed:
                copy.write_row((site_id, days, page, query, clicks, impressions, ctr, position))
        out[f"rows_{days}"] = len(parsed)
    return out


# --- phase 2: per-day slices ----------------------------------------------------------------


def pull_day(
    conn: psycopg.Connection,
    site_id: str,
    gsc: GscSearchAnalytics,
    day: dt.date,
    search_type: str = "web",
) -> int:
    """Pull one fully-paginated day slice; replace gsc_daily + gsc_page_daily slices.

    Bookkeeping: upserts gsc_ingest_log (final = day < today-3) and bumps the gsc_rows
    quota ledger — callers enforce the 50k/day cap against that ledger.
    """
    ensure_partition(conn, day)
    final = is_final(day)
    parsed: list[tuple[str, str, int, int, float, float]] = []
    for page_rows in gsc.query_all(
        start_date=day,
        end_date=day,
        dimensions=DIMENSIONS,
        search_type=search_type,
        data_state="final" if final else "all",
    ):
        parsed.extend(_norm_row(r) for r in page_rows)

    conn.execute(
        "delete from gsc_daily where site_id = %s and date = %s and search_type = %s",
        (site_id, day, search_type),
    )
    with conn.cursor() as cur, cur.copy(
        "copy gsc_daily"
        " (site_id, date, search_type, page, query, clicks, impressions, ctr, position)"
        " from stdin"
    ) as copy:
        for page, query, clicks, impressions, ctr, position in parsed:
            copy.write_row(
                (site_id, day, search_type, page, query, clicks, impressions, ctr, position)
            )

    # Rollup slice: recompute from what is now in gsc_daily for (site, day) — the freshly
    # inserted rows (plus any other search_type slices). Impression-weighted mean position,
    # falling back to the plain mean when a page has zero impressions.
    conn.execute(
        "delete from gsc_page_daily where site_id = %s and date = %s", (site_id, day)
    )
    conn.execute(
        """
        insert into gsc_page_daily (site_id, date, page, clicks, impressions, position)
        select site_id, date, page, sum(clicks), sum(impressions),
               coalesce(sum(position * impressions) / nullif(sum(impressions), 0),
                        avg(position))::real
          from gsc_daily
         where site_id = %s and date = %s
         group by site_id, date, page
        """,
        (site_id, day),
    )

    conn.execute(
        """
        insert into gsc_ingest_log (site_id, date, search_type, rows, final, pulled_at)
        values (%s, %s, %s, %s, %s, now())
        on conflict (site_id, date, search_type) do update
           set rows = excluded.rows, final = excluded.final, pulled_at = now()
        """,
        (site_id, day, search_type, len(parsed), final),
    )
    costs.bump_quota(conn, QUOTA_PORT, str(site_id), len(parsed))
    return len(parsed)


def backfill_plan(
    conn: psycopg.Connection,
    site_id: str,
    *,
    months: int = BACKFILL_MONTHS,
    today: dt.date | None = None,
) -> list[dt.date]:
    """Newest-first days needing a pull over the GSC retention window.

    A day needs a pull when it has no gsc_ingest_log row, or when it was pulled non-final
    and is now old enough (<= today-4) that a re-pull can finalize it. Days newer than
    today-4 that are already pulled are left to the daily trailing-window job, so the
    backfill chain terminates.
    """
    today = today or _utc_today()
    newest = today - dt.timedelta(days=2)
    oldest = months_ago(today, months)
    finalizable = today - dt.timedelta(days=FINAL_LAG_DAYS + 1)
    logged = {
        r["date"]: r["final"]
        for r in conn.execute(
            "select date, final from gsc_ingest_log"
            " where site_id = %s and search_type = 'web'",
            (site_id,),
        ).fetchall()
    }
    plan: list[dt.date] = []
    day = newest
    while day >= oldest:
        if day not in logged or (not logged[day] and day <= finalizable):
            plan.append(day)
        day -= dt.timedelta(days=1)
    return plan


# --- credential loading (lazy imports: gm.connections is owned elsewhere) --------------------


def _load_client(conn: psycopg.Connection, site_id: Any) -> tuple[Any, GscSearchAnalytics]:
    """Stored gsc connection -> (connection_id, GscClient). Tests monkeypatch this."""
    from gm.connections.gsc import GscClient
    from gm.connections.vault import load_connection

    row = load_connection(conn, site_id, "gsc")
    return row["id"], GscClient(row["credentials"], row["meta"]["property"])


def _is_auth_error(exc: BaseException) -> bool:
    try:
        from gm.connections.gsc import GscAuthError
    except ImportError:  # pragma: no cover - only when the client module is absent
        return False
    return isinstance(exc, GscAuthError)


def _mark_ok(conn: psycopg.Connection, connection_id: Any) -> None:
    """In-band: commits (or rolls back) together with the job's work transaction."""
    from gm.connections.vault import mark_connection

    mark_connection(conn, connection_id, ok=True)


def _mark_broken(org_id: Any, connection_id: Any, error: str) -> None:
    """Out-of-band connection so the status survives the job transaction's rollback."""
    from gm.connections.vault import mark_connection

    with db.connect() as conn:
        db.set_org(conn, org_id)
        mark_connection(conn, connection_id, ok=False, error=error)
        conn.commit()


# --- job handlers -----------------------------------------------------------------------------


def _enqueue_backfill(
    conn: psycopg.Connection, job: Any, *, throttled: bool
) -> int | None:
    """Enqueue the next gsc_backfill batch if the plan is non-empty."""
    plan = backfill_plan(conn, job.site_id)
    if not plan:
        return None
    batch = plan[:BACKFILL_BATCH_DAYS]
    run_after = next_backfill_run() if throttled else None
    key_day = run_after.date() if run_after else _utc_today()
    return jobs.enqueue(
        conn,
        type="gsc_backfill",
        org_id=job.org_id,
        site_id=job.site_id,
        payload={"days": [d.isoformat() for d in batch]},
        run_after=run_after,
        idempotency_key=f"gsc_backfill:{job.site_id}:{batch[0].isoformat()}:{key_day.isoformat()}",
    )


def handle_gsc_initial(ctx: jobs.JobContext) -> None:
    """Job 'gsc_initial': phase-1 pull, provisional detectors, then the first backfill batch."""
    conn = ctx.conn
    connection_id, gsc = _load_client(conn, ctx.job.site_id)
    try:
        initial_pull(conn, ctx.job.site_id, gsc)
    except Exception as exc:
        if _is_auth_error(exc):
            _mark_broken(ctx.job.org_id, connection_id, str(exc))
        raise
    _mark_ok(conn, connection_id)
    jobs.enqueue(conn, type="compute_queue", org_id=ctx.job.org_id, site_id=ctx.job.site_id)
    _enqueue_backfill(conn, ctx.job, throttled=False)


def handle_gsc_backfill(ctx: jobs.JobContext) -> None:
    """Job 'gsc_backfill' {days: [...]}: pull each day, heartbeating between days.

    Stops the batch early once the site's gsc_rows ledger exceeds THROTTLE_ROWS today;
    the next batch then waits until tomorrow 06:00 UTC. Re-enqueues while the plan is
    non-empty (unpulled days from an interrupted batch fall back into the plan).
    """
    conn = ctx.conn
    days = [dt.date.fromisoformat(d) for d in ctx.job.payload.get("days", [])]
    connection_id, gsc = _load_client(conn, ctx.job.site_id)
    throttled = False
    try:
        for day in days:
            ctx.heartbeat()
            if costs.quota_used(conn, QUOTA_PORT, str(ctx.job.site_id)) > THROTTLE_ROWS:
                throttled = True
                break
            pull_day(conn, ctx.job.site_id, gsc, day)
    except Exception as exc:
        if _is_auth_error(exc):
            _mark_broken(ctx.job.org_id, connection_id, str(exc))
        raise
    _mark_ok(conn, connection_id)
    _enqueue_backfill(conn, ctx.job, throttled=throttled)


def handle_gsc_daily(ctx: jobs.JobContext) -> None:
    """Scheduled job 'gsc_daily': re-pull the trailing window [today-4 .. today-2]."""
    conn = ctx.conn
    connection_id, gsc = _load_client(conn, ctx.job.site_id)
    today = _utc_today()
    try:
        for offset in (4, 3, 2):
            ctx.heartbeat()
            pull_day(conn, ctx.job.site_id, gsc, today - dt.timedelta(days=offset))
    except Exception as exc:
        if _is_auth_error(exc):
            _mark_broken(ctx.job.org_id, connection_id, str(exc))
        raise
    _mark_ok(conn, connection_id)
    jobs.enqueue(
        conn,
        type="compute_queue",
        org_id=ctx.job.org_id,
        site_id=ctx.job.site_id,
        idempotency_key=f"compute_queue:{ctx.job.site_id}:{today.isoformat()}",
    )
