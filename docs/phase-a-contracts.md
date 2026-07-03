# Phase A module contracts

Binding interfaces for parallel module builds. The schema is `ops/migrations/001_phase_a.sql`;
shared plumbing is `gm.config`, `gm.db`, and `gm.intel.engines.base` (already written — read them
before coding, do not modify them). Style: Python 3.12, type hints, ruff (line length 100),
psycopg3 sync with dict rows. Tests use pytest; DB tests must skip cleanly when DATABASE_URL is unset.

## gm/infra/jobs.py

```python
@dataclass
class JobRow:  # mirrors the jobs table row (id, type, org_id, site_id, payload, attempts, ...)

def enqueue(conn, *, type: str, org_id=None, site_id=None, payload: dict | None = None,
            run_after=None, idempotency_key: str | None = None, priority: int = 5,
            max_attempts: int = 3) -> int | None
    # INSERT ... ON CONFLICT (idempotency_key) DO NOTHING; returns job id or None if deduped.

def claim_one(conn, worker_id: str, types: list[str], lease_seconds: int) -> JobRow | None
    # Short autocommit claim: UPDATE jobs SET status='running', locked_by, locked_until,
    # attempts=attempts+1 WHERE id = (SELECT id FROM jobs WHERE status='queued' AND type = ANY(%s)
    # AND run_after <= now() ORDER BY priority, run_after LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *.

def heartbeat(conn, job_id: int, worker_id: str, lease_seconds: int) -> bool
    # Extends locked_until iff still owned by worker_id and status='running'. False = lost lease.

def complete(conn, job_id: int, worker_id: str) -> None       # status='done', finished_at=now()
def fail(conn, job_id: int, worker_id: str, error: str) -> None
    # attempts >= max_attempts -> status='dead' (+finished_at); else status='queued' with
    # run_after = now() + backoff (min(300s, 10s * 2**attempts)) and locked_* cleared.

def reap_stale(conn) -> int
    # status='running' AND locked_until < now() -> requeue via the same fail() semantics
    # with error='lease expired'. Returns count.

class JobContext:
    job: JobRow
    conn: psycopg.Connection      # inside an open tx with SET LOCAL app.org_id already applied
    def heartbeat(self) -> None   # uses a separate autocommit conn; raises LostLease if False

Handler = Callable[[JobContext], None]

class Worker:
    def __init__(self, handlers: dict[str, Handler], worker_id: str | None = None,
                 lease_seconds: int = 120, poll_seconds: float = 2.0): ...
    def run_once(self) -> bool    # claim+run at most one job; True if a job was processed
    def run_forever(self, stop_event: threading.Event | None = None) -> None
    # Mechanics: claim on an autocommit conn; open a work conn, BEGIN, db.set_org(conn, job.org_id),
    # call handler, COMMIT, then complete(). On exception: ROLLBACK + fail(). SIGTERM/SIGINT set the
    # stop event: finish the current job, exit the loop. Every ~10 polls, call reap_stale.
```

## gm/infra/scheduler.py

```python
def run_due(conn) -> int
    # For each enabled schedule with next_run_at <= now():
    #   enqueue(type=job_type, payload=payload, org_id, site_id,
    #           idempotency_key=f"sched:{id}:{next_run_at.isoformat()}")
    #   catch-up: advance next_run_at += every_minutes REPEATEDLY until > now()
    #   (enqueue only once per sweep — missed ticks collapse into one late run);
    #   set last_enqueued_at. Returns schedules fired.

def scheduler_loop(stop_event, tick_seconds: float = 15.0) -> None
    # Dedicated NON-pooled connection; pg_try_advisory_lock(hashtext('gm_scheduler')::bigint);
    # if not leader, retry each tick. Leader runs run_due every tick on that same connection.
```

## gm/intel/engines/ (adapters)

Files: `openai_engine.py`, `perplexity_engine.py`, `gemini_engine.py`, `fake_engine.py`.
Each implements `EngineAdapter` from `base.py` (already written). httpx, 60s total timeout,
3 retries on 429/5xx with exponential backoff + jitter; raise `EngineError(retryable=...)` otherwise.
API keys/models from env per `platform/.env.example`. Cost: estimate from usage tokens with a
per-model $/1M-token table (approximate constants are fine, documented inline).

- OpenAI: POST /v1/responses, model `OPENAI_MODEL`, tools=[{"type": "web_search"}] (fall back to
  "web_search_preview" if the API rejects the tool type); citations from url_citation annotations.
- Perplexity: POST https://api.perplexity.ai/chat/completions, model `PERPLEXITY_MODEL`;
  cited urls from `citations` / `search_results` fields.
- Gemini: POST v1beta models/{GEMINI_MODEL}:generateContent with tools=[{"google_search": {}}];
  cited urls from groundingMetadata.groundingChunks[].web.uri; answer from candidates[0].content.
- FakeEngine(answers: list[EngineSample] | None): deterministic, for tests and --dry-run;
  cycles through provided samples.

`registry() -> dict[str, EngineAdapter]` in `gm/intel/engines/__init__.py` mapping
name -> constructed adapter for {"openai", "perplexity", "gemini"} (constructed lazily; missing
API key -> adapter excluded with a warning function `available() -> list[str]`).

## gm/intel/variance.py  (pure functions, no DB)

```python
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]   # (low, high); n=0 -> (0.0, 1.0)
def fmt_rate(k: int, n: int) -> str                                  # "7/9"

@dataclass
class Window: k: int; n: int
    # rate property; samples with error are excluded upstream

@dataclass
class PromptVerdict:
    prompt_id: str; before: Window; after: Window
    gain: float                       # after.rate - before.rate
    ci_before: tuple[float, float]; ci_after: tuple[float, float]
    sufficient: bool                  # both windows >= min_samples_per_window

def prompt_verdicts(windows: dict[str, tuple[Window, Window]], thresholds: dict) -> list[PromptVerdict]

@dataclass
class GateVerdict:
    status: str                       # "PASS" | "FAIL" | "INCONCLUSIVE"
    moved_prompt_ids: list[str]
    control_mean_drift: float
    details: list[PromptVerdict]
    reasons: list[str]                # human-readable, e.g. "control drift 0.21 > 0.15"

def gate_verdict(treatment: dict[str, tuple[Window, Window]],
                 control_gains: list[float],
                 thresholds: dict) -> GateVerdict
    # moved = sufficient AND gain >= movement.min_absolute_rate_gain
    #         AND (gain - mean(control_gains)) >= movement.min_gain_over_control
    # INCONCLUSIVE (reported as FAIL per pre-registration) when
    #         mean(|control_gains|) > gate.max_control_drift
    # PASS when len(moved) >= gate.min_prompts_moved
```

Thresholds dict = parsed `ops/gate1-thresholds.yaml` (caller parses; variance.py takes a dict).

## gm/delivery/evidence.py  (pure formatting, no DB)

```python
def export_markdown(report: dict) -> str
```
`report` keys: site (domain, is_control), window_before/window_after ({label, run_ids, date_range}),
prompts: [{prompt_text, engine_breakdown: {engine: {before: Window, after: Window}},
pooled: PromptVerdict-like dict}], gate: GateVerdict-like dict, controls: [{domain, gain}],
levers: [{applied_at, lever_class, description}], panel_hash, thresholds_status, generated_at.
Output: a dated, client-forwardable evidence log — headline verdict, per-prompt table with
"named in 7/9 runs, was 1/9" phrasing + Wilson CIs, control table, lever appendix,
raw-sample-ref appendix, and the claim-ceiling line ("movement vs control, lever unattributed").

## Test requirements

- `tests/test_jobs.py`: requires DATABASE_URL (skip module otherwise). Cover: enqueue/claim/complete;
  idempotency dedupe; fail->backoff->dead after max_attempts; lease expiry + reap_stale requeue;
  heartbeat extends; catch-up scheduler collapses missed ticks into one enqueue and advances past now.
  Create a throwaway schema or truncate the jobs/schedules tables per test.
- `tests/test_engines.py`: no network — adapters parse RECORDED response fixtures (inline dicts) for
  each engine: citations extracted, usage mapped, cost computed; retry logic via a stubbed transport;
  detect() cases: exact host, subdomain, www-stripping, brand-term mention, no match.
- `tests/test_variance.py`: wilson known values; window insufficiency; gate PASS / FAIL /
  INCONCLUSIVE-on-control-drift; fmt_rate; export_markdown golden-ish assertions (contains verdict
  line, claim ceiling, a "7/9" rate).
```
