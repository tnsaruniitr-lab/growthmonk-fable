"""Catch-up scheduler: a single advisory-lock leader enqueues due schedules.

Missed ticks collapse: a schedule that is N intervals behind enqueues exactly once
per sweep (idempotency-keyed on the due tick) and its next_run_at is advanced past
now() in one step, so downtime produces one late run, never a burst.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

import psycopg

from gm import db
from gm.infra import jobs

log = logging.getLogger(__name__)

_TRY_LOCK_SQL = "select pg_try_advisory_lock(hashtext('gm_scheduler')::bigint) as locked"


def run_due(conn: psycopg.Connection) -> int:
    """Enqueue every enabled schedule whose next_run_at is due. Returns schedules fired."""
    rows = conn.execute(
        "select * from schedules where enabled and next_run_at <= now() order by next_run_at"
    ).fetchall()
    fired = 0
    for sched in rows:
        tick: dt.datetime = sched["next_run_at"]
        jobs.enqueue(
            conn,
            type=sched["job_type"],
            org_id=sched["org_id"],
            site_id=sched["site_id"],
            payload=sched["payload"],
            idempotency_key=f"sched:{sched['id']}:{tick.isoformat()}",
        )
        now: dt.datetime = conn.execute("select now() as now").fetchone()["now"]
        every = dt.timedelta(minutes=sched["every_minutes"])
        next_run = tick + every
        while next_run <= now:  # collapse missed ticks into the one enqueue above
            next_run += every
        conn.execute(
            "update schedules set next_run_at = %s, last_enqueued_at = now() where id = %s",
            (next_run, sched["id"]),
        )
        fired += 1
    return fired


def scheduler_loop(stop_event: threading.Event, tick_seconds: float = 15.0) -> None:
    """Leader-elected loop on a dedicated non-pooled connection.

    The advisory lock is session-scoped, so leadership lasts exactly as long as this
    connection: on any DB error the connection is dropped and leadership re-contested.
    """
    conn: psycopg.Connection | None = None
    leader = False
    try:
        while not stop_event.is_set():
            try:
                if conn is None or conn.closed:
                    conn = db.connect(autocommit=True)
                    leader = False
                if not leader:
                    leader = conn.execute(_TRY_LOCK_SQL).fetchone()["locked"]
                if leader:
                    run_due(conn)
            except psycopg.Error:
                log.exception("scheduler tick failed; resetting connection")
                if conn is not None:
                    try:
                        conn.close()
                    except psycopg.Error:
                        pass
                conn = None
                leader = False
            stop_event.wait(tick_seconds)
    finally:
        if conn is not None and not conn.closed:
            conn.close()
