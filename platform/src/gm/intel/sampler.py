"""Citation sampling: run creation + the sample_citations job handler + window assembly.

A run freezes its panel at creation (immutable denominator). Execution is resumable:
existing (prompt, engine, sample_index) rows are skipped, so a retried job never
duplicates samples or spend. Per-sample failures are recorded rows with error set —
visible, and excluded from rates (honest failure).
"""

from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.types.json import Jsonb

from gm import config
from gm.core import panel as panel_mod
from gm.infra import costs, jobs
from gm.intel import engines as engines_pkg
from gm.intel.engines.base import EngineError, detect


def enqueue_run(
    conn: psycopg.Connection,
    org_id: str,
    site_id: str,
    *,
    samples_per_run: int = 3,
    dry_run: bool = False,
) -> str:
    frozen = panel_mod.freeze_panel(conn, site_id)
    if not frozen:
        raise RuntimeError("Site has no active prompts — add prompts before starting a run")
    row = conn.execute(
        "insert into citation_runs (org_id, site_id, panel) values (%s, %s, %s) returning id",
        (org_id, site_id, Jsonb(frozen)),
    ).fetchone()
    run_id = str(row["id"])
    jobs.enqueue(
        conn,
        type="sample_citations",
        org_id=org_id,
        site_id=site_id,
        payload={"run_id": run_id, "samples_per_run": samples_per_run, "dry_run": dry_run},
        idempotency_key=f"sample:{run_id}",
    )
    return run_id


def _existing_samples(conn: psycopg.Connection, run_id: str) -> set[tuple[str, str, int]]:
    rows = conn.execute(
        "select prompt_id, engine, sample_index from citation_results where run_id=%s",
        (run_id,),
    ).fetchall()
    return {(str(r["prompt_id"]), r["engine"], r["sample_index"]) for r in rows}


def _store_raw(run_id: str, prompt_id: str, engine: str, idx: int, raw: dict) -> str:
    d = config.raw_store_dir() / run_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{prompt_id}_{engine}_{idx}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, default=str))
    return str(path)


def handle_sample_citations(ctx: jobs.JobContext) -> None:
    conn = ctx.conn
    payload = ctx.job.payload
    run_id = payload["run_id"]
    samples_per_run = int(payload.get("samples_per_run", 3))
    dry_run = bool(payload.get("dry_run", False))

    run = conn.execute("select * from citation_runs where id=%s", (run_id,)).fetchone()
    if not run:
        raise RuntimeError(f"citation_run not found: {run_id}")
    site = conn.execute("select * from sites where id=%s", (run["site_id"],)).fetchone()
    conn.execute(
        "update citation_runs set status='running', started_at=coalesce(started_at, now())"
        " where id=%s",
        (run_id,),
    )

    if dry_run:
        from gm.intel.engines.fake_engine import FakeEngine

        adapters = {name: FakeEngine() for name in ("openai", "perplexity", "gemini")}
    else:
        adapters = engines_pkg.registry()
    frozen = run["panel"] if isinstance(run["panel"], list) else json.loads(run["panel"])
    done = _existing_samples(conn, run_id)
    domain = site["domain_norm"]
    brand_terms = list(site["brand_terms"] or [])

    for entry in frozen:
        prompt_id, prompt_text = entry["prompt_id"], entry["prompt"]
        for engine_name in entry["engines"]:
            adapter = adapters.get(engine_name)
            for idx in range(samples_per_run):
                if (prompt_id, engine_name, idx) in done:
                    continue
                ctx.heartbeat()
                if adapter is None:
                    _insert_result(
                        conn, run, prompt_id, engine_name, idx,
                        error=f"adapter unavailable: {engine_name} (missing API key?)",
                    )
                    continue
                try:
                    sample = adapter.sample(prompt_text)
                except EngineError as e:
                    _insert_result(conn, run, prompt_id, engine_name, idx, error=str(e))
                    continue
                det = detect(sample, domain, brand_terms)
                raw_ref = _store_raw(run_id, prompt_id, engine_name, idx, sample.raw)
                _insert_result(
                    conn, run, prompt_id, engine_name, idx,
                    model_version=sample.model_version,
                    cited=det.cited, cited_url=det.cited_url, mentioned=det.mentioned,
                    answer_excerpt=sample.answer_text[:2000], raw_ref=raw_ref,
                )
                costs.record_cost(
                    conn, provider=engine_name, purpose="citation_sample",
                    cost_cents=sample.cost_cents, org_id=run["org_id"], job_id=ctx.job.id,
                    units=sample.usage,
                )
                costs.bump_quota(conn, "llm", engine_name)

    conn.execute(
        "update citation_runs set status='done', finished_at=now() where id=%s", (run_id,)
    )


def _insert_result(
    conn, run, prompt_id, engine, idx, *, model_version=None, cited=False,
    cited_url=None, mentioned=False, answer_excerpt=None, raw_ref=None, error=None,
) -> None:
    conn.execute(
        "insert into citation_results (org_id, run_id, prompt_id, engine, engine_model_version,"
        " sample_index, cited, cited_url, mentioned, answer_excerpt, raw_ref, error)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " on conflict (run_id, prompt_id, engine, sample_index) do nothing",
        (run["org_id"], run["id"], prompt_id, engine, model_version, idx, cited,
         cited_url, mentioned, answer_excerpt, raw_ref, error),
    )


# --- window assembly for verdicts ---------------------------------------------------------

def window_stats(conn: psycopg.Connection, run_ids: list[str]) -> dict[str, dict]:
    """Per-prompt pooled (k, n) plus per-engine breakdown, error rows excluded."""
    if not run_ids:
        return {}
    ids = [uuid.UUID(r) for r in run_ids]
    pooled: dict[str, dict] = {}
    for r in conn.execute(
        "select prompt_id, engine,"
        " count(*) filter (where error is null) as n,"
        " count(*) filter (where cited and error is null) as k"
        " from citation_results where run_id = any(%s)"
        " group by prompt_id, engine",
        (ids,),
    ).fetchall():
        p = pooled.setdefault(str(r["prompt_id"]), {"k": 0, "n": 0, "engines": {}})
        p["k"] += r["k"]
        p["n"] += r["n"]
        p["engines"][r["engine"]] = {"k": r["k"], "n": r["n"]}
    return pooled


def raw_ref_count(conn: psycopg.Connection, run_ids: list[str]) -> int:
    if not run_ids:
        return 0
    ids = [uuid.UUID(r) for r in run_ids]
    row = conn.execute(
        "select count(*) as c from citation_results where run_id = any(%s)"
        " and raw_ref is not null",
        (ids,),
    ).fetchone()
    return row["c"]
