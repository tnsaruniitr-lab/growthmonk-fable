"""Fix-closer job (docs/phase-c-wave3-contracts.md, agent A).

Job 'close_fixes' payload {content_item_id}: brief -> build_writer_request
(convergence fix applied request-side) -> engine.write_and_audit -> drafts
row (version=next, package, cost estimated from their response when present)
-> run_draft_audit against OUR registry (agent B's function, built
concurrently — imported lazily, called to its contract signature) ->
drafts.scorecard_audit_id + human_todos (the engine audit's open items + our
failing check names) -> content_items.status='review'.

Engine down / missing env -> EngineUnavailable -> the job fails with the
honest error (retryable by re-enqueue).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from psycopg.types.json import Jsonb

from gm.content.engine_port import ContentEngine, build_writer_request
from gm.infra.costs import record_cost

log = logging.getLogger(__name__)

JOB_TYPE = "close_fixes"

# USD keys observed across the engine's response shapes (write-and-audit has
# none today; auto-edit reports totalCostUsd) — best-effort estimate only.
_COST_USD_KEYS = ("total_cost_usd", "totalCostUsd", "cost_usd", "costUsd")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def estimate_cost_cents(response: dict) -> float:
    """Best-effort cost from the engine response (USD -> cents); 0.0 when
    the response carries no cost field."""
    for key in _COST_USD_KEYS:
        value = response.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return float(value) * 100.0
    return 0.0


def extract_open_items(response: dict) -> list[dict]:
    """The engine audit's open items as human_todos entries.

    Sources (shapes from serp-analyzer src/blog/auditor.ts + types.ts):
    audit.fix_summary [{check_id, issue, exact_fix}], the package
    validation's human_signal_gaps / pending_visual_placements, and failed
    editorial_checklist entries."""
    todos: list[dict] = []
    audit = response.get("audit") if isinstance(response.get("audit"), dict) else {}
    for entry in audit.get("fix_summary") or []:
        if not isinstance(entry, dict):
            continue
        todo = {
            "source": "engine_audit",
            "id": str(entry.get("check_id") or ""),
            "todo": str(entry.get("issue") or ""),
        }
        if entry.get("exact_fix"):
            todo["fix"] = str(entry["exact_fix"])
        todos.append(todo)
    package = (response.get("blog_package")
               if isinstance(response.get("blog_package"), dict) else {})
    validation = (package.get("validation")
                  if isinstance(package.get("validation"), dict) else {})
    for gap in validation.get("human_signal_gaps") or []:
        if isinstance(gap, str) and gap.strip():
            todos.append({"source": "engine_validation", "id": "human_signal_gap",
                          "todo": gap.strip()})
    for pending in validation.get("pending_visual_placements") or []:
        if isinstance(pending, str) and pending.strip():
            todos.append({"source": "engine_validation", "id": "pending_visual",
                          "todo": f"replace placeholder visual: {pending.strip()}"})
    for item in package.get("editorial_checklist") or []:
        if isinstance(item, dict) and item.get("pass") is False:
            todos.append({
                "source": "engine_checklist",
                "id": str(item.get("id") or ""),
                "todo": str(item.get("label") or "")
                + (f" — {item['detail']}" if item.get("detail") else ""),
            })
    return todos


def _check_names(registry: Any) -> dict[str, str]:
    checks = getattr(registry, "checks", None)
    if not isinstance(checks, dict):
        return {}
    return {cid: str((meta or {}).get("name") or cid) for cid, meta in checks.items()}


def our_failing_todos(finding_rows: list[dict], registry: Any = None) -> list[dict]:
    """audit_findings fail/warn rows -> human_todos entries carrying the
    check NAMES (registry-named where known, check_id otherwise)."""
    names = _check_names(registry)
    todos = []
    for row in finding_rows:
        check_id = str(row.get("check_id") or "")
        status = str(row.get("status") or "")
        name = names.get(check_id, check_id)
        todos.append({
            "source": "gm_audit",
            "id": check_id,
            "todo": f"{name} ({check_id}) — {status} in the draft scorecard audit",
        })
    return todos


# ---------------------------------------------------------------------------
# The job handler
# ---------------------------------------------------------------------------

def _load_rows(conn, content_item_id: str) -> tuple[dict, dict, dict]:
    item = conn.execute(
        "select * from content_items where id = %s", (content_item_id,)
    ).fetchone()
    if item is None:
        raise ValueError(f"close_fixes: content item {content_item_id} not found")
    if item["brief_id"] is None:
        raise ValueError(f"close_fixes: content item {content_item_id} has no brief")
    brief_row = conn.execute(
        "select * from briefs where id = %s", (item["brief_id"],)
    ).fetchone()
    if brief_row is None:
        raise ValueError(f"close_fixes: brief {item['brief_id']} not found")
    site = conn.execute(
        "select domain_norm, brand_terms, notes, author, first_party from sites"
        " where id = %s",
        (item["site_id"],),
    ).fetchone()
    if site is None:
        raise ValueError(f"close_fixes: site {item['site_id']} not found")
    return item, brief_row, site


def _next_version(conn, content_item_id) -> int:
    row = conn.execute(
        "select coalesce(max(version), 0) + 1 as next from drafts where content_item_id = %s",
        (content_item_id,),
    ).fetchone()
    return int(row["next"])


def _url_hint(target: dict, site: dict, package: dict) -> str:
    page = target.get("page") if isinstance(target, dict) else None
    if isinstance(page, str) and page:
        return page
    article = package.get("article") if isinstance(package.get("article"), dict) else {}
    slug = str(article.get("slug") or "draft").strip("/")
    return f"https://{site['domain_norm']}/{slug}"


def _default_registry() -> Any:
    try:
        from gm.audit.registry import load_registry
        return load_registry()
    except Exception as exc:  # names degrade to check ids, honestly logged
        log.warning("close_fixes: registry unavailable for check names: %s", exc)
        return None


def handle_close_fixes(ctx, *, engine: ContentEngine | None = None, llm: Any = None,
                       registry: Any = None) -> None:
    """Job handler for type 'close_fixes': payload {content_item_id}.

    Keyword-only ports (engine/llm/registry) exist for tests and callers
    that already hold instances; the worker registers the bare handler."""
    payload = ctx.job.payload or {}
    content_item_id = payload.get("content_item_id")
    if not content_item_id:
        raise ValueError(f"job {ctx.job.id}: payload missing 'content_item_id'")

    conn = ctx.conn
    item, brief_row, site = _load_rows(conn, content_item_id)

    # Request-side convergence fix; empty sites.author fails fast here.
    request = build_writer_request(site, brief_row, kind=str(item["kind"]))

    eng = engine if engine is not None else ContentEngine()  # may raise EngineUnavailable
    response = eng.write_and_audit(
        request, timeout=float(payload.get("timeout_seconds", 900.0))
    )

    cost_cents = estimate_cost_cents(response)
    version = _next_version(conn, content_item_id)
    draft_id = conn.execute(
        "insert into drafts (org_id, content_item_id, version, package, cost_cents)"
        " values (%s, %s, %s, %s, %s) returning id",
        (item["org_id"], content_item_id, version, Jsonb(response), cost_cents),
    ).fetchone()["id"]
    if cost_cents > 0:
        record_cost(
            conn, provider="content_engine", purpose="close_fixes_write",
            cost_cents=cost_cents, org_id=item["org_id"], job_id=ctx.job.id,
        )

    # Draft scorecard against OUR registry — agent B's function, built
    # concurrently: lazy import, called to its wave-3 contract signature.
    if llm is None:
        from gm.infra.llm import LlmClient  # deferred: needs ANTHROPIC_API_KEY
        llm = LlmClient()
    package = (response.get("blog_package")
               if isinstance(response.get("blog_package"), dict) else {})
    draft_html = str(package.get("html") or response.get("html") or "")
    pipeline = importlib.import_module("gm.audit.pipeline")
    run_draft_audit = pipeline.run_draft_audit
    audit_id = run_draft_audit(
        conn,
        org_id=str(item["org_id"]),
        site_id=str(item["site_id"]),
        draft_html=draft_html,
        url_hint=_url_hint(brief_row.get("target") or {}, site, package),
        llm=llm,
        cost_cap_cents=float(payload.get("cost_cap_cents", 150.0)),
        draft_id=draft_id,
    )

    failing = conn.execute(
        "select check_id, status from audit_findings"
        " where audit_id = %s and status in ('fail', 'warn') order by check_id",
        (audit_id,),
    ).fetchall()
    reg = registry if registry is not None else _default_registry()
    human_todos = extract_open_items(response) + our_failing_todos(failing, reg)

    conn.execute(
        "update drafts set scorecard_audit_id = %s, human_todos = %s where id = %s",
        (audit_id, Jsonb(human_todos), draft_id),
    )
    conn.execute(
        "update content_items set status = 'review', updated_at = now() where id = %s",
        (content_item_id,),
    )
