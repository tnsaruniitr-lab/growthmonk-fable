"""Ads daily ingest — trailing-window slice replacement (Phase D3, WP-G).

gsc_ingest's shape: the platforms restate conversions for days after the fact,
so every pull re-pulls a trailing window and replaces the whole (site, date,
channel) slice range — DELETE the window per channel + INSERT, idempotent by
construction, no upsert index needed (migration 011's discipline).

BLOCKED-ON-CLIENT: no live ad account exists; readers are injected in tests
(fixture shapes only) and resolved via gm.connections.ads.readers_for_site in
production once a client links an account. Non-retryable auth errors mark the
connection broken (status='broken' + last_error) and are reported honestly in
the result — retrying an expired token cannot help; retryable errors propagate
so the leased job retries. Every pull records a cost_event (provider=channel,
purpose='ads_daily_pull', cost 0 — audit trail; the report APIs are free).

Worker registration, CLI verbs, and the default schedule are WP-E's (cli.py).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg

from gm.connections.ads import AdsError, AdsReader, readers_for_site
from gm.infra import costs, jobs

log = logging.getLogger(__name__)

# Trailing re-pull window: platforms restate conversions for about a week.
DEFAULT_WINDOW_DAYS = 7


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def window_range(days: int, *, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """Inclusive [since, until] — `days` whole days ending yesterday (today is partial)."""
    today = today or _utc_today()
    until = today - dt.timedelta(days=1)
    return until - dt.timedelta(days=days - 1), until


def _replace_slice(
    conn: psycopg.Connection,
    *,
    org_id: Any,
    site_id: Any,
    channel: str,
    since: dt.date,
    until: dt.date,
    rows: list[dict],
) -> int:
    """Replace the channel's whole [since, until] slice range with `rows`.

    Deleting the full window (not just dates present in the response) is the
    point: a day the platform now reports as empty must not keep stale rows.
    Rows outside the window are ignored — the slice being replaced defines
    what this pull is allowed to assert.
    """
    conn.execute(
        "delete from ads_daily where site_id = %s and channel = %s"
        " and date between %s and %s",
        (site_id, channel, since, until),
    )
    inserted = 0
    for row in rows:
        date_raw = row.get("date")
        try:
            date = dt.date.fromisoformat(str(date_raw))
        except ValueError:
            log.warning("ads_ingest: skipping row with bad date %r", date_raw)
            continue
        if not (since <= date <= until):
            continue
        conn.execute(
            "insert into ads_daily (org_id, site_id, date, channel, campaign_id,"
            " campaign_name, spend, currency, clicks, platform_conversions)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id, site_id, date, channel,
                str(row.get("campaign_id") or ""),
                row.get("campaign_name"),
                round(float(row.get("spend") or 0.0), 2),
                row.get("currency") or "AED",
                row.get("clicks"),
                row.get("platform_conversions"),
            ),
        )
        inserted += 1
    return inserted


def pull_ads_daily(
    conn: psycopg.Connection,
    *,
    org_id: Any,
    site_id: Any,
    readers: list[AdsReader] | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    today: dt.date | None = None,
) -> dict:
    """Trailing-window re-pull for every connected ads channel.

    Returns {"since","until","channels":[{"channel","rows"}],"broken":[...],
    "note"} — with the honest {"note": "no ads connections"} short-circuit
    (zero work, zero rows written) when the site has no ads connections.
    """
    if readers is None:
        readers = readers_for_site(conn, site_id)
    if not readers:
        return {"note": "no ads connections"}
    since, until = window_range(days, today=today)
    channels: list[dict] = []
    broken: list[dict] = []
    for reader in readers:
        try:
            rows = reader.daily_rows(since=since, until=until)
        except AdsError as exc:
            if exc.retryable:
                raise  # leased job retries; slice replacement keeps re-runs idempotent
            # Auth (401/403) and other non-retryable failures: mark the
            # connection broken in-band and report honestly — a retry loop
            # cannot fix an expired token.
            connection_id = getattr(reader, "connection_id", None)
            if connection_id is not None:
                conn.execute(
                    "update connections set status = 'broken', last_error = %s"
                    " where id = %s",
                    (str(exc), connection_id),
                )
            broken.append({"channel": reader.channel, "error": str(exc)})
            continue
        inserted = _replace_slice(
            conn, org_id=org_id, site_id=site_id, channel=reader.channel,
            since=since, until=until, rows=rows,
        )
        costs.record_cost(
            conn,
            provider=reader.channel,
            purpose="ads_daily_pull",
            cost_cents=0,
            org_id=org_id,
            units={"rows": inserted, "since": since.isoformat(), "until": until.isoformat()},
        )
        channels.append({"channel": reader.channel, "rows": inserted})
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "channels": channels,
        "broken": broken,
        "note": None,
    }


def handle_pull_ads_daily(ctx: jobs.JobContext) -> None:
    """Job 'pull_ads_daily': site-scoped trailing-window pull (COMMON wiring).

    site_id from job.site_id or payload.site_id; org_id resolved from sites
    when absent (handle_keyword_gap's pattern). payload.days widens the
    trailing window (`gm ads pull --days N` enqueues it); default 7.
    """
    payload = ctx.job.payload or {}
    site_id = ctx.job.site_id or payload.get("site_id")
    if not site_id:
        raise RuntimeError("pull_ads_daily job requires site_id")
    org_id = ctx.job.org_id
    if org_id is None:
        row = ctx.conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"site not found: {site_id}")
        org_id = row["org_id"]
    days = int(payload.get("days", DEFAULT_WINDOW_DAYS))
    result = pull_ads_daily(ctx.conn, org_id=org_id, site_id=site_id, days=days)
    log.info(
        "pull_ads_daily site=%s channels=%s broken=%s note=%s",
        site_id, result.get("channels"), result.get("broken"), result.get("note"),
    )
