"""DB tests for the durable job queue and catch-up scheduler.

Run against a real Postgres (CI: Postgres 16). Skips cleanly when DATABASE_URL is unset.
Lease-expiry tests use a 1-second lease so no sleep exceeds ~1.1s.
"""

import datetime as dt
import os
import signal
import threading
import time
import uuid

import pytest

from gm import db
from gm.infra import jobs, scheduler

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    with db.connect(autocommit=True) as c:
        c.execute("truncate jobs restart identity")
        c.execute("truncate schedules")
        yield c


def _job(conn, job_id: int) -> dict:
    return conn.execute("select * from jobs where id = %s", (job_id,)).fetchone()


def _job_count(conn) -> int:
    return conn.execute("select count(*) as n from jobs").fetchone()["n"]


# --- enqueue / claim / complete -------------------------------------------------------


def test_enqueue_claim_complete(conn):
    job_id = jobs.enqueue(conn, type="t", payload={"a": 1})
    assert isinstance(job_id, int)

    job = jobs.claim_one(conn, "w1", ["t"], lease_seconds=60)
    assert job is not None
    assert job.id == job_id
    assert job.status == "running"
    assert job.attempts == 1
    assert job.locked_by == "w1"
    assert job.locked_until is not None
    assert job.payload == {"a": 1}

    # already claimed: nothing left for a second worker
    assert jobs.claim_one(conn, "w2", ["t"], lease_seconds=60) is None

    jobs.complete(conn, job.id, "w1")
    row = _job(conn, job_id)
    assert row["status"] == "done"
    assert row["finished_at"] is not None
    assert row["locked_by"] is None


def test_claim_respects_type_run_after_and_priority(conn):
    jobs.enqueue(conn, type="other")
    assert jobs.claim_one(conn, "w1", ["t"], 60) is None  # wrong type

    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    jobs.enqueue(conn, type="t", run_after=future)
    assert jobs.claim_one(conn, "w1", ["t"], 60) is None  # not due yet

    low = jobs.enqueue(conn, type="t", priority=5)
    high = jobs.enqueue(conn, type="t", priority=1)
    assert jobs.claim_one(conn, "w1", ["t"], 60).id == high
    assert jobs.claim_one(conn, "w1", ["t"], 60).id == low


def test_idempotency_dedupe(conn):
    first = jobs.enqueue(conn, type="t", idempotency_key="k1")
    assert first is not None
    assert jobs.enqueue(conn, type="t", idempotency_key="k1") is None
    assert _job_count(conn) == 1


# --- fail -> backoff -> dead ----------------------------------------------------------


def test_fail_backoff_then_dead(conn):
    job_id = jobs.enqueue(conn, type="t", max_attempts=2)

    job = jobs.claim_one(conn, "w1", ["t"], 60)
    assert job.attempts == 1
    jobs.fail(conn, job.id, "w1", "boom 1")

    # attempts=1 < max_attempts=2: requeued with backoff min(300, 10*2^1) = 20s
    row = conn.execute(
        """
        select status, locked_by, locked_until, last_error, finished_at,
               run_after > now() + interval '15 seconds' as after_15s,
               run_after < now() + interval '25 seconds' as before_25s
          from jobs where id = %s
        """,
        (job_id,),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["locked_by"] is None
    assert row["locked_until"] is None
    assert row["finished_at"] is None
    assert row["last_error"] == "boom 1"
    assert row["after_15s"] and row["before_25s"]

    # make it due again, fail a second time -> attempts=2 >= max_attempts -> dead
    conn.execute("update jobs set run_after = now() where id = %s", (job_id,))
    job = jobs.claim_one(conn, "w1", ["t"], 60)
    assert job.attempts == 2
    jobs.fail(conn, job.id, "w1", "boom 2")

    row = _job(conn, job_id)
    assert row["status"] == "dead"
    assert row["finished_at"] is not None
    assert row["last_error"] == "boom 2"
    assert row["locked_by"] is None


def test_fail_requires_ownership(conn):
    job_id = jobs.enqueue(conn, type="t")
    jobs.claim_one(conn, "w1", ["t"], 60)
    jobs.fail(conn, job_id, "intruder", "nope")
    assert _job(conn, job_id)["status"] == "running"  # untouched


# --- leases: heartbeat + reaper -------------------------------------------------------


def test_heartbeat_extends_lease(conn):
    job_id = jobs.enqueue(conn, type="t")
    jobs.claim_one(conn, "w1", ["t"], lease_seconds=1)

    assert jobs.heartbeat(conn, job_id, "w2", 60) is False  # wrong worker
    assert jobs.heartbeat(conn, job_id, "w1", 60) is True

    row = conn.execute(
        "select locked_until > now() + interval '30 seconds' as extended"
        " from jobs where id = %s",
        (job_id,),
    ).fetchone()
    assert row["extended"]
    assert jobs.reap_stale(conn) == 0


def test_lease_expiry_reap_requeues_and_deads(conn):
    # A: expires and requeues; B: expires with max_attempts=1 -> dead;
    # C: heartbeated past the sleep -> survives. One shared 1.1s sleep.
    a = jobs.enqueue(conn, type="t")
    b = jobs.enqueue(conn, type="t", max_attempts=1)
    c = jobs.enqueue(conn, type="t")
    for worker in ("wa", "wb", "wc"):
        assert jobs.claim_one(conn, worker, ["t"], lease_seconds=1) is not None
    assert jobs.heartbeat(conn, c, "wc", 60) is True

    time.sleep(1.1)
    assert jobs.reap_stale(conn) == 2

    row_a = _job(conn, a)
    assert row_a["status"] == "queued"
    assert row_a["last_error"] == "lease expired"
    assert row_a["locked_by"] is None
    assert row_a["locked_until"] is None

    row_b = _job(conn, b)
    assert row_b["status"] == "dead"
    assert row_b["last_error"] == "lease expired"
    assert row_b["finished_at"] is not None

    assert _job(conn, c)["status"] == "running"

    # the expired worker has lost its lease
    assert jobs.heartbeat(conn, a, "wa", 60) is False


def test_job_context_heartbeat_raises_lost_lease(conn):
    job_id = jobs.enqueue(conn, type="t")
    job = jobs.claim_one(conn, "w1", ["t"], 60)
    ctx = jobs.JobContext(job, conn, "w1", 60)
    ctx.heartbeat()  # still owned: no raise

    conn.execute("update jobs set locked_by = 'thief' where id = %s", (job_id,))
    with pytest.raises(jobs.LostLease):
        ctx.heartbeat()


# --- Worker ---------------------------------------------------------------------------


def test_worker_run_once_success_and_org_scope(conn):
    org_id = str(uuid.uuid4())
    seen = {}

    def handler(ctx: jobs.JobContext) -> None:
        seen["payload"] = ctx.job.payload
        seen["org"] = ctx.conn.execute(
            "select current_setting('app.org_id', true) as org"
        ).fetchone()["org"]

    job_id = jobs.enqueue(conn, type="ok", org_id=org_id, payload={"x": 2})
    worker = jobs.Worker({"ok": handler}, worker_id="w1")
    assert worker.run_once() is True
    assert worker.run_once() is False  # queue drained

    assert seen["payload"] == {"x": 2}
    assert seen["org"] == org_id
    row = _job(conn, job_id)
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_worker_failure_rolls_back_and_requeues(conn):
    def handler(ctx: jobs.JobContext) -> None:
        ctx.conn.execute(
            "insert into cost_events (provider, purpose) values ('test', 'rollback-check')"
        )
        raise ValueError("handler exploded")

    job_id = jobs.enqueue(conn, type="bad")
    worker = jobs.Worker({"bad": handler}, worker_id="w1")
    assert worker.run_once() is True  # a job was processed, even though it failed

    row = _job(conn, job_id)
    assert row["status"] == "queued"
    assert "ValueError" in row["last_error"]
    assert row["attempts"] == 1
    n = conn.execute(
        "select count(*) as n from cost_events where purpose = 'rollback-check'"
    ).fetchone()["n"]
    assert n == 0  # work transaction rolled back


def test_worker_survives_poisonous_base_exception(conn):
    def handler(ctx: jobs.JobContext) -> None:
        raise SystemExit("poison")

    job_id = jobs.enqueue(conn, type="poison", max_attempts=1)
    worker = jobs.Worker({"poison": handler}, worker_id="w1")
    assert worker.run_once() is True  # does not propagate SystemExit

    row = _job(conn, job_id)
    assert row["status"] == "dead"
    assert "SystemExit" in row["last_error"]


def test_run_forever_processes_then_stops_and_restores_signals(conn):
    stop = threading.Event()

    def handler(ctx: jobs.JobContext) -> None:
        stop.set()  # simulate SIGTERM arriving mid-run: finish this job, then exit

    job_id = jobs.enqueue(conn, type="one")
    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)

    worker = jobs.Worker({"one": handler}, worker_id="w1", poll_seconds=0.05)
    worker.run_forever(stop)  # returns because the handler set the stop event

    assert _job(conn, job_id)["status"] == "done"
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int


def test_run_forever_sigterm_sets_stop_event(conn):
    stop = threading.Event()
    backstop = threading.Timer(3.0, stop.set)  # bound the test if the signal path breaks
    backstop.start()
    threading.Timer(0.25, os.kill, args=(os.getpid(), signal.SIGTERM)).start()

    started = time.monotonic()
    worker = jobs.Worker({}, worker_id="w1", poll_seconds=0.1)
    worker.run_forever(stop)  # empty queue: idles until SIGTERM sets the stop event
    elapsed = time.monotonic() - started
    backstop.cancel()

    assert stop.is_set()
    assert elapsed < 2.5  # stopped via the signal handler, not the backstop


# --- scheduler ------------------------------------------------------------------------


def test_scheduler_catchup_collapses_missed_ticks(conn):
    sched_id = uuid.uuid4()
    old_tick = conn.execute(
        """
        insert into schedules (id, job_type, payload, every_minutes, next_run_at)
        values (%s, 'tick', '{"k": 1}', 10, now() - interval '35 minutes')
        returning next_run_at
        """,
        (sched_id,),
    ).fetchone()["next_run_at"]

    assert scheduler.run_due(conn) == 1
    rows = conn.execute("select * from jobs").fetchall()
    assert len(rows) == 1  # 4 missed ticks collapsed into one late run
    assert rows[0]["type"] == "tick"
    assert rows[0]["payload"] == {"k": 1}
    assert rows[0]["idempotency_key"] == f"sched:{sched_id}:{old_tick.isoformat()}"

    sched = conn.execute(
        "select next_run_at, last_enqueued_at, next_run_at > now() as future"
        " from schedules where id = %s",
        (sched_id,),
    ).fetchone()
    assert sched["future"]
    # advanced on the original grid: -35min + 4 * 10min = +5min
    assert sched["next_run_at"] - old_tick == dt.timedelta(minutes=40)
    assert sched["last_enqueued_at"] is not None

    # nothing due anymore: no second enqueue
    assert scheduler.run_due(conn) == 0
    assert _job_count(conn) == 1


def test_scheduler_skips_disabled(conn):
    conn.execute(
        "insert into schedules (job_type, every_minutes, next_run_at, enabled)"
        " values ('tick', 5, now() - interval '1 minute', false)"
    )
    assert scheduler.run_due(conn) == 0
    assert _job_count(conn) == 0


def test_scheduler_loop_waits_for_advisory_lock(conn):
    # Hold the leader lock from this session: the loop must idle, not enqueue.
    conn.execute("select pg_advisory_lock(hashtext('gm_scheduler')::bigint)")
    conn.execute(
        "insert into schedules (job_type, every_minutes, next_run_at)"
        " values ('lt', 1, now() - interval '1 minute')"
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=scheduler.scheduler_loop, args=(stop,), kwargs={"tick_seconds": 0.05}
    )
    thread.start()
    try:
        time.sleep(0.4)
        assert _job_count(conn) == 0  # not leader: never fired

        conn.execute("select pg_advisory_unlock(hashtext('gm_scheduler')::bigint)")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and _job_count(conn) == 0:
            time.sleep(0.05)
        assert _job_count(conn) == 1  # took over leadership and fired the late run
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
