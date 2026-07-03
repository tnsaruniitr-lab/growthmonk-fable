"""Durable job queue: claim-then-work with leases (ADR-11).

Claiming is a single short autocommit UPDATE ... RETURNING with FOR UPDATE SKIP LOCKED
in the subselect, so concurrent workers never double-claim and never queue behind each
other. Work happens in a separate transaction; the lease (locked_until) is the crash
recovery mechanism — reap_stale requeues expired leases through the same fail semantics
so max_attempts is always honored.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from gm import db

log = logging.getLogger(__name__)


class LostLease(Exception):
    """Heartbeat found the job no longer owned by this worker: stop working on it."""


@dataclass
class JobRow:
    id: int
    type: str
    org_id: uuid.UUID | None
    site_id: uuid.UUID | None
    payload: dict
    status: str
    priority: int
    run_after: dt.datetime
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    concurrency_key: str | None
    locked_by: str | None
    locked_until: dt.datetime | None
    last_error: str | None
    created_at: dt.datetime
    finished_at: dt.datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> JobRow:
        return cls(**{f.name: row[f.name] for f in fields(cls)})


def enqueue(
    conn: psycopg.Connection,
    *,
    type: str,
    org_id: str | uuid.UUID | None = None,
    site_id: str | uuid.UUID | None = None,
    payload: dict | None = None,
    run_after: dt.datetime | None = None,
    idempotency_key: str | None = None,
    priority: int = 5,
    max_attempts: int = 3,
) -> int | None:
    """Insert a job; returns its id, or None when deduped on idempotency_key."""
    row = conn.execute(
        """
        insert into jobs (type, org_id, site_id, payload, run_after,
                          idempotency_key, priority, max_attempts)
        values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s)
        on conflict (idempotency_key) do nothing
        returning id
        """,
        (type, org_id, site_id, Jsonb(payload or {}), run_after,
         idempotency_key, priority, max_attempts),
    ).fetchone()
    return row["id"] if row else None


def claim_one(
    conn: psycopg.Connection, worker_id: str, types: list[str], lease_seconds: int
) -> JobRow | None:
    """Claim the next due job (single autocommit statement). None when nothing is due."""
    row = conn.execute(
        """
        update jobs
           set status = 'running',
               locked_by = %s,
               locked_until = now() + make_interval(secs => %s),
               attempts = attempts + 1
         where id = (
               select id from jobs
                where status = 'queued' and type = any(%s) and run_after <= now()
                order by priority, run_after
                limit 1
                for update skip locked)
        returning *
        """,
        (worker_id, lease_seconds, types),
    ).fetchone()
    return JobRow.from_row(row) if row else None


def heartbeat(
    conn: psycopg.Connection, job_id: int, worker_id: str, lease_seconds: int
) -> bool:
    """Extend the lease iff still owned by worker_id and running. False = lost lease."""
    row = conn.execute(
        """
        update jobs
           set locked_until = now() + make_interval(secs => %s)
         where id = %s and locked_by = %s and status = 'running'
        returning id
        """,
        (lease_seconds, job_id, worker_id),
    ).fetchone()
    return row is not None


def complete(conn: psycopg.Connection, job_id: int, worker_id: str) -> None:
    conn.execute(
        """
        update jobs
           set status = 'done', finished_at = now(), locked_by = null, locked_until = null
         where id = %s and locked_by = %s and status = 'running'
        """,
        (job_id, worker_id),
    )


# Shared SET clause so fail() and reap_stale() have identical retry/dead semantics.
# Backoff is computed from the CURRENT attempts value (already incremented at claim).
_FAIL_SET = """
    status = case when attempts >= max_attempts then 'dead' else 'queued' end,
    finished_at = case when attempts >= max_attempts then now() else finished_at end,
    run_after = case when attempts >= max_attempts then run_after
                     else now() + make_interval(secs => least(300.0, 10.0 * (2.0 ^ attempts)))
                end,
    locked_by = null,
    locked_until = null,
    last_error = %(error)s
"""


def fail(conn: psycopg.Connection, job_id: int, worker_id: str, error: str) -> None:
    """Requeue with exponential backoff, or mark dead once attempts >= max_attempts."""
    conn.execute(
        f"update jobs set {_FAIL_SET}"
        " where id = %(job_id)s and locked_by = %(worker_id)s and status = 'running'",
        {"error": error, "job_id": job_id, "worker_id": worker_id},
    )


def reap_stale(conn: psycopg.Connection) -> int:
    """Requeue (or dead-letter) every running job whose lease expired. Returns count."""
    cur = conn.execute(
        f"update jobs set {_FAIL_SET} where status = 'running' and locked_until < now()",
        {"error": "lease expired"},
    )
    return cur.rowcount


class JobContext:
    """Handed to handlers: the claimed job plus an org-scoped work transaction."""

    def __init__(
        self, job: JobRow, conn: psycopg.Connection, worker_id: str, lease_seconds: int
    ):
        self.job = job
        self.conn = conn
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def heartbeat(self) -> None:
        """Extend the lease on a separate autocommit connection; raises LostLease."""
        with db.connect(autocommit=True) as hb_conn:
            if not heartbeat(hb_conn, self.job.id, self._worker_id, self._lease_seconds):
                raise LostLease(f"job {self.job.id}: lease no longer held by {self._worker_id}")


Handler = Callable[[JobContext], None]


class Worker:
    def __init__(
        self,
        handlers: dict[str, Handler],
        worker_id: str | None = None,
        lease_seconds: int = 120,
        poll_seconds: float = 2.0,
    ):
        self.handlers = handlers
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds

    def run_once(self) -> bool:
        """Claim and run at most one job; True if a job was processed (even if it failed)."""
        if not self.handlers:
            return False
        with db.connect(autocommit=True) as ctl_conn:
            job = claim_one(ctl_conn, self.worker_id, list(self.handlers), self.lease_seconds)
            if job is None:
                return False
            self._run_job(ctl_conn, job)
            return True

    def _run_job(self, ctl_conn: psycopg.Connection, job: JobRow) -> None:
        # BaseException on purpose: a poisonous handler (including SystemExit /
        # KeyboardInterrupt raised inside it) must fail the job, not kill the loop.
        try:
            with db.connect() as work_conn:
                try:
                    db.set_org(work_conn, job.org_id)  # implicit BEGIN + SET LOCAL app.org_id
                    self.handlers[job.type](
                        JobContext(job, work_conn, self.worker_id, self.lease_seconds)
                    )
                    work_conn.commit()
                except BaseException:
                    work_conn.rollback()
                    raise
        except BaseException as exc:  # noqa: B036 - poisonous handlers must not kill the loop
            log.exception("job %s (%s) failed", job.id, job.type)
            fail(ctl_conn, job.id, self.worker_id, f"{type(exc).__name__}: {exc}")
        else:
            complete(ctl_conn, job.id, self.worker_id)

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event if stop_event is not None else threading.Event()
        previous: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            polls = 0
            while not stop.is_set():
                if polls % 10 == 0:
                    with db.connect(autocommit=True) as conn:
                        reap_stale(conn)
                polls += 1
                if not self.run_once():
                    stop.wait(self.poll_seconds)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
