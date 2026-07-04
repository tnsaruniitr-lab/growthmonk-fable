"""Default schedules: what every client site gets wired for on onboarding.

Phase D3 WP-E. `ensure_default_schedules` is idempotent — any schedules row with
(site_id, job_type) counts as existing and is NEVER duplicated and NEVER mutated,
so operator-tuned every_minutes/payload survive re-invocation (site add, wa-connect,
ads connect, backfill all funnel through here).

Cadence constants live here and ONLY here (COMMON contract): DAILY/WEEKLY/MONTHLY,
with MONTHLY = 43200 minutes — D2's 30-day convention.

`handle_assemble_receipt_monthly` exists because assemble_receipt REQUIRES an
explicit period payload (receipts.py determinism rule) — a fixed-payload schedules
row cannot drive it directly. The thin job derives period = the calendar month
BEFORE ctx.job.created_at (created_at, not now(): retries pin the same month) and
enqueues assemble_receipt idempotency-keyed on (site, period). Missed months are
NOT auto-backfilled — the catch-up scheduler collapses missed ticks into one late
run, so a long outage produces one receipt, and the operator runs the explicit
`gm receipt` verb for any months in between.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from gm.infra import jobs

log = logging.getLogger(__name__)

# Cadences in minutes — the only copies (COMMON contract).
DAILY = 1440
WEEKLY = 10080
MONTHLY = 43200  # 30 days — D2's monthly convention

# Unconditional defaults for every non-control site; payload {} always.
# keyword_gap / refresh_competitor_profiles tolerate empty competitor_domains
# (they log the zero-spend note and do no work).
DEFAULT_SCHEDULES: tuple[tuple[str, int], ...] = (
    ("track_serps", WEEKLY),
    ("keyword_gap", MONTHLY),
    ("assemble_receipt_monthly", MONTHLY),
    ("refresh_competitor_profiles", MONTHLY),
)

# Created only when a status='ok' connection of any of the listed kinds exists —
# otherwise the weekly/daily job would just fail noisily against nothing.
CONDITIONAL_SCHEDULES: dict[str, tuple[tuple[str, ...], int]] = {
    "send_lead_card": (("whatsapp",), WEEKLY),
    "pull_ads_daily": (("google_ads", "meta_ads"), DAILY),
}


def first_run_at(job_type: str, *, today: dt.date | None = None) -> dt.datetime:
    """Deterministic first tick for a default schedule (today injectable for tests).

    Most jobs start now; send_lead_card waits for the next Monday 06:00 UTC
    (D1 weeks are Mon-start, so the first card covers a full tracked week);
    assemble_receipt_monthly waits for the 1st of next month 06:00 UTC (the
    first receipt covers a full calendar month).
    """
    if today is None:
        now = dt.datetime.now(dt.UTC)
        today = now.date()
    else:
        now = dt.datetime.combine(today, dt.time(0, 0), tzinfo=dt.UTC)
    if job_type == "send_lead_card":
        days_ahead = (7 - today.weekday()) % 7 or 7  # strictly the NEXT Monday
        monday = today + dt.timedelta(days=days_ahead)
        return dt.datetime.combine(monday, dt.time(6, 0), tzinfo=dt.UTC)
    if job_type == "assemble_receipt_monthly":
        first_next = (
            dt.date(today.year + 1, 1, 1)
            if today.month == 12
            else dt.date(today.year, today.month + 1, 1)
        )
        return dt.datetime.combine(first_next, dt.time(6, 0), tzinfo=dt.UTC)
    return now


def _plan(conn: psycopg.Connection, site_id: Any) -> tuple[list[tuple[str, int]], list[str], dict]:
    """What ensure_default_schedules WOULD do: (to_create, existing, skipped)."""
    existing_types = {
        r["job_type"]
        for r in conn.execute(
            "select job_type from schedules where site_id = %s", (site_id,)
        ).fetchall()
    }
    kinds_ok = {
        r["kind"]
        for r in conn.execute(
            "select kind from connections where site_id = %s and status = 'ok'", (site_id,)
        ).fetchall()
    }
    to_create: list[tuple[str, int]] = []
    existing: list[str] = []
    skipped: dict[str, str] = {}
    for job_type, every in DEFAULT_SCHEDULES:
        if job_type in existing_types:
            existing.append(job_type)
        else:
            to_create.append((job_type, every))
    for job_type, (kinds, every) in CONDITIONAL_SCHEDULES.items():
        if job_type in existing_types:  # existing beats connection state: never mutated
            existing.append(job_type)
        elif not (kinds_ok & set(kinds)):
            skipped[job_type] = f"no {'/'.join(kinds)} connection"
        else:
            to_create.append((job_type, every))
    return to_create, existing, skipped


def ensure_default_schedules(
    conn: psycopg.Connection, *, org_id: Any, site_id: Any, today: dt.date | None = None
) -> dict:
    """Create every missing default schedule for a site; idempotent.

    Returns {"created": [job_type...], "existing": [...], "skipped": {job_type: reason}}.
    Existing (site_id, job_type) rows — enabled or not, tuned or not — are counted
    as existing and left byte-identical.
    """
    to_create, existing, skipped = _plan(conn, site_id)
    for job_type, every in to_create:
        conn.execute(
            "insert into schedules (org_id, site_id, job_type, payload, every_minutes,"
            " next_run_at) values (%s, %s, %s, %s, %s, %s)",
            (org_id, site_id, job_type, Jsonb({}), every, first_run_at(job_type, today=today)),
        )
    return {"created": [jt for jt, _ in to_create], "existing": existing, "skipped": skipped}


def backfill_default_schedules(
    conn: psycopg.Connection, *, org_id: Any, dry_run: bool = False
) -> dict:
    """ensure_default_schedules for every non-control site in the org.

    Returns {"sites": {domain_norm: ensure-result}}. dry_run reports what WOULD
    be created without writing anything.
    """
    sites = conn.execute(
        "select id, domain_norm from sites where org_id = %s and not is_control"
        " order by domain_norm",
        (org_id,),
    ).fetchall()
    out: dict[str, dict] = {}
    for site in sites:
        if dry_run:
            to_create, existing, skipped = _plan(conn, site["id"])
            out[site["domain_norm"]] = {
                "created": [jt for jt, _ in to_create],
                "existing": existing,
                "skipped": skipped,
            }
        else:
            out[site["domain_norm"]] = ensure_default_schedules(
                conn, org_id=org_id, site_id=site["id"]
            )
    return {"sites": out}


def handle_assemble_receipt_monthly(ctx: jobs.JobContext) -> None:
    """Job 'assemble_receipt_monthly': enqueue assemble_receipt for last month.

    period = the calendar month BEFORE ctx.job.created_at (created_at, not now(),
    so retries of the same job row always derive the same month). The enqueue is
    idempotency-keyed 'receipt:{site_id}:{period}' — a retry after a successful
    enqueue dedupes to nothing. Missed months are not auto-backfilled; the
    operator runs `gm receipt <domain> --period YYYY-MM` explicitly.
    """
    site_id = ctx.job.site_id or (ctx.job.payload or {}).get("site_id")
    if not site_id:
        raise RuntimeError("assemble_receipt_monthly job requires site_id")
    org_id = ctx.job.org_id
    if org_id is None:
        row = ctx.conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"site not found: {site_id}")
        org_id = row["org_id"]
    created_at = ctx.job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    anchor = created_at.astimezone(dt.UTC).date()
    prior_last = anchor.replace(day=1) - dt.timedelta(days=1)
    period = f"{prior_last.year:04d}-{prior_last.month:02d}"
    job_id = jobs.enqueue(
        ctx.conn,
        type="assemble_receipt",
        org_id=org_id,
        site_id=str(site_id),
        payload={"period": period},
        idempotency_key=f"receipt:{site_id}:{period}",
    )
    log.info(
        "assemble_receipt_monthly site=%s period=%s enqueued=%s",
        site_id,
        period,
        job_id if job_id is not None else "deduped",
    )
