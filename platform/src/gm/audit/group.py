"""Group autopsy rollup — the multi-location artifact (AED 1,850/location).

A clinic group runs the same CMS template across N location pages. The whole
point of this artifact is the sitewide / per-location split: a template-level
finding that fails on (almost) every location is ONE fix that closes N pages,
and it must appear ONCE in the fix queue with pages_affected=N — never N times.

Three layers, deliberately separated so the assembly rules are pure and
unit-testable without a database:

  - `run_group_audit`: runs `gm.audit.pipeline.run_page_audit` per URL
    sequentially (reused, never reimplemented), collecting audit_ids. A page
    that fails or comes back inconclusive is included with its persisted
    status — never dropped.
  - `assemble_group` / `assemble_rows`: pure assembly from persisted
    audits/findings. Sitewide = a check failing on >= ceil(60% of GRADED
    pages) with a template-shaped fix_type (sitewide_template, cms_constraint,
    schema); inconclusive pages are excluded from the denominator. Everything
    else failing lands per-location (including template-shaped checks under
    the threshold AND page-shaped checks failing everywhere — nothing is
    silently dropped between the two buckets).
  - `handle_audit_group`: job handler (type 'audit_group', payload
    {"urls": [...]}) that persists the assembled dict into ONE extra audits
    row with url=NULL and gate_state='group_rollup' — so share tokens can
    point at the group report without schema changes.

The 60% threshold uses exact integer math — ceil(3n/5) — so the contract's
boundary cases (3-of-5, 2-of-3) never depend on float rounding.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb

from gm.audit.pipeline import run_page_audit
from gm.audit.registry import Registry, load_registry

log = logging.getLogger(__name__)

JOB_TYPE = "audit_group"

# gate_state marking the group summary audits row (url NULL, scores = rollup).
GROUP_GATE_STATE = "group_rollup"

# fix_types where one fix plausibly closes the finding on every location page.
SITEWIDE_FIX_TYPES = frozenset({"sitewide_template", "cms_constraint", "schema"})

# Registry `severity` -> rank for ordering. Unknown/missing severities rank as
# medium — a data gap should not sink a finding to the bottom of the queue.
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
DEFAULT_SEVERITY = "medium"

TOP_ISSUES_PER_LOCATION = 3

_EFFORT_HINTS = {
    "sitewide_template": "one template change",
    "cms_constraint": "one CMS/platform setting",
    "schema": "one structured-data template update",
    "page_html": "per-page HTML edit",
    "content_restructure": "per-page content rework",
    "offpage_entity": "off-page entity work",
    "cannot_fix_from_page": "not fixable from the page itself",
}


def sitewide_threshold(graded_pages: int) -> int:
    """ceil(60% of graded pages) in exact integer math: ceil(3n/5) = (3n+4)//5.

    Boundary cases pinned by contract: 5 graded pages -> 3, 3 graded -> 2.
    Integer math avoids the float trap where ceil(0.6*n) depends on rounding.
    """
    return (3 * graded_pages + 4) // 5


# ---------------------------------------------------------------------------
# Registry lookups (tolerant: audits may pin an older registry version)
# ---------------------------------------------------------------------------

def _severity(registry: Registry, check_id: str) -> str:
    check = registry.checks.get(check_id) or {}
    sev = str(check.get("severity") or "").strip().lower()
    return sev if sev in SEVERITY_RANK else DEFAULT_SEVERITY


def _impact(registry: Registry, check_id: str) -> float:
    """severity_rank * registry weight — the ordering metric for issues."""
    return SEVERITY_RANK[_severity(registry, check_id)] * registry.weight_of(check_id)


def _name(registry: Registry, check_id: str) -> str:
    check = registry.checks.get(check_id) or {}
    return str(check.get("name") or check_id)


def _fix_type(registry: Registry, check_id: str, finding: dict | None = None) -> str | None:
    """Registry is authoritative for fix_type; the persisted finding row is the
    fallback for checks no longer in the current registry."""
    check = registry.checks.get(check_id) or {}
    if check.get("fix_type"):
        return str(check["fix_type"])
    if finding is not None and finding.get("fix_type"):
        return str(finding["fix_type"])
    return None


def _badge(registry: Registry, check_id: str, finding: dict | None = None) -> str | None:
    check = registry.checks.get(check_id) or {}
    if check.get("badge"):
        return str(check["badge"])
    if finding is not None and finding.get("badge"):
        return str(finding["badge"])
    return None


def _effort_hint(fix_type: str | None, scope: str, pages_affected: int) -> str:
    base = _EFFORT_HINTS.get(fix_type or "", "manual fix")
    if scope == "sitewide":
        return f"{base} — one fix, {pages_affected} pages benefit"
    plural = "page" if pages_affected == 1 else "pages"
    return f"{base} on {pages_affected} {plural}"


def _score_of(scores: dict) -> float | None:
    v = scores.get("overall_score")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _evidence_note(finding: dict) -> str:
    ev = finding.get("evidence")
    return str(ev.get("note") or "") if isinstance(ev, dict) else ""


# ---------------------------------------------------------------------------
# Pure assembly (no DB) — the rules the tests pin
# ---------------------------------------------------------------------------

def assemble_rows(rows: list[dict], registry: Registry) -> dict:
    """Assemble the group rollup from already-loaded audit rows.

    Each row: {audit_id, url, status, gate_state?, scores: dict,
    findings: [{check_id, status, badge?, fix_type?, evidence?}]}.
    Pure function of its inputs; JSON-serializable output.
    """
    locations: list[dict] = []
    graded_rows: list[dict] = []
    grade_dist: Counter[str] = Counter()

    for row in rows:
        scores = row.get("scores") or {}
        score = _score_of(scores)
        grade = str(scores.get("overall_grade") or "INCONCLUSIVE")
        findings = row.get("findings") or []
        fails = [f for f in findings if f.get("status") == "fail"]
        top = sorted(fails, key=lambda f: (-_impact(registry, f["check_id"]), f["check_id"]))
        locations.append({
            "audit_id": str(row["audit_id"]),
            "url": row.get("url"),
            "status": row.get("status"),
            "grade": grade,
            "score": score,
            "top_issues": [
                {
                    "check_id": f["check_id"],
                    "name": _name(registry, f["check_id"]),
                    "severity": _severity(registry, f["check_id"]),
                    "badge": _badge(registry, f["check_id"], f),
                    "fix_type": _fix_type(registry, f["check_id"], f),
                }
                for f in top[:TOP_ISSUES_PER_LOCATION]
            ],
        })
        grade_dist[grade] += 1
        if row.get("status") == "done" and score is not None:
            graded_rows.append(row)

    graded_n = len(graded_rows)
    graded_scores = [_score_of(r.get("scores") or {}) for r in graded_rows]
    graded_scores = [s for s in graded_scores if s is not None]
    rollup = {
        "avg_score": round(sum(graded_scores) / graded_n, 1) if graded_n else None,
        "min_score": min(graded_scores) if graded_n else None,
        "max_score": max(graded_scores) if graded_n else None,
        "grade_distribution": dict(grade_dist),
        "pages_audited": len(rows),
        "pages_graded": graded_n,
        "pages_inconclusive": len(rows) - graded_n,
    }

    # Sitewide detection: fail counts over GRADED pages only (inconclusive
    # pages are out of the denominator AND out of the numerator).
    fails_by_check: dict[str, list[tuple[dict, dict]]] = {}
    for row in graded_rows:
        for f in row.get("findings") or []:
            if f.get("status") == "fail":
                fails_by_check.setdefault(f["check_id"], []).append((row, f))

    threshold = sitewide_threshold(graded_n)
    sitewide: list[dict] = []
    sitewide_ids: set[str] = set()
    if graded_n > 0:  # threshold 0 on an empty group would make everything "sitewide"
        for cid, hits in fails_by_check.items():
            fix_type = _fix_type(registry, cid, hits[0][1])
            if fix_type not in SITEWIDE_FIX_TYPES or len(hits) < threshold:
                continue
            note = next((n for n in (_evidence_note(f) for _, f in hits) if n), "")
            sitewide.append({
                "check_id": cid,
                "name": _name(registry, cid),
                "fix_type": fix_type,
                "badge": _badge(registry, cid, hits[0][1]),
                "severity": _severity(registry, cid),
                "pages_affected": len(hits),
                "affected_urls": [r.get("url") for r, _ in hits],
                "evidence_note": note,
            })
            sitewide_ids.add(cid)
    sitewide.sort(
        key=lambda e: (-e["pages_affected"], -_impact(registry, e["check_id"]), e["check_id"])
    )

    # Per-location residue: every fail not classified sitewide — including
    # sitewide-shaped checks under the threshold and page-shaped checks that
    # fail everywhere. Grouped per location, worst first.
    per_location: list[dict] = []
    for row, loc in zip(rows, locations, strict=True):
        cids = sorted(
            {
                f["check_id"]
                for f in row.get("findings") or []
                if f.get("status") == "fail" and f["check_id"] not in sitewide_ids
            },
            key=lambda c: (-_impact(registry, c), c),
        )
        if cids:
            per_location.append({"audit_id": loc["audit_id"], "url": loc["url"],
                                 "check_ids": cids})

    # Fix queue: sitewide first (one fix, N pages benefit), then per-location
    # checks deduped by check_id and ordered by severity impact.
    fix_queue: list[dict] = []
    for e in sitewide:
        fix_queue.append({
            "check_id": e["check_id"],
            "name": e["name"],
            "fix_type": e["fix_type"],
            "badge": e["badge"],
            "severity": e["severity"],
            "scope": "sitewide",
            "pages_affected": e["pages_affected"],
            "urls": e["affected_urls"],
            "effort_hint": _effort_hint(e["fix_type"], "sitewide", e["pages_affected"]),
        })

    local_hits: dict[str, list[tuple[dict, dict]]] = {}
    for row in rows:  # ALL rows: a failed-but-persisted finding is never dropped
        for f in row.get("findings") or []:
            if f.get("status") == "fail" and f["check_id"] not in sitewide_ids:
                local_hits.setdefault(f["check_id"], []).append((row, f))
    for cid in sorted(
        local_hits, key=lambda c: (-_impact(registry, c), -len(local_hits[c]), c)
    ):
        hits = local_hits[cid]
        fix_type = _fix_type(registry, cid, hits[0][1])
        fix_queue.append({
            "check_id": cid,
            "name": _name(registry, cid),
            "fix_type": fix_type,
            "badge": _badge(registry, cid, hits[0][1]),
            "severity": _severity(registry, cid),
            "scope": "per_location",
            "pages_affected": len(hits),
            "urls": [r.get("url") for r, _ in hits],
            "effort_hint": _effort_hint(fix_type, "per_location", len(hits)),
        })

    return {
        "registry_version": registry.version,
        "locations": locations,
        "rollup": rollup,
        "sitewide": sitewide,
        "per_location_issues": per_location,
        "fix_queue": fix_queue,
        "member_audit_ids": [loc["audit_id"] for loc in locations],
    }


# ---------------------------------------------------------------------------
# DB assembly
# ---------------------------------------------------------------------------

def load_group_rows(conn, audit_ids: list[str]) -> list[dict]:
    """Load persisted audits + findings for `assemble_rows`, preserving the
    input order. Raises ValueError when an audit id is missing (or invisible
    under the current org context) — a silent drop would forge the rollup."""
    ids = [str(a) for a in audit_ids]
    if not ids:
        return []
    audits = conn.execute(
        "select id, url, status, gate_state, scores from audits where id = any(%s::uuid[])",
        (ids,),
    ).fetchall()
    by_id = {str(a["id"]): a for a in audits}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError(f"audits not found (or not visible in org context): {missing}")

    findings = conn.execute(
        "select audit_id, check_id, status, badge, fix_type, evidence from audit_findings"
        " where audit_id = any(%s::uuid[]) order by id",
        (ids,),
    ).fetchall()
    by_audit: dict[str, list[dict]] = {}
    for f in findings:
        by_audit.setdefault(str(f["audit_id"]), []).append(dict(f))

    return [
        {
            "audit_id": i,
            "url": by_id[i]["url"],
            "status": by_id[i]["status"],
            "gate_state": by_id[i]["gate_state"],
            "scores": by_id[i]["scores"] or {},
            "findings": by_audit.get(i, []),
        }
        for i in ids
    ]


def assemble_group(conn, audit_ids: list[str], registry: Registry | None = None) -> dict:
    """Pure assembly from persisted audits/findings (no LLM calls, no fetches)."""
    reg = registry if registry is not None else load_registry()
    return assemble_rows(load_group_rows(conn, audit_ids), reg)


# ---------------------------------------------------------------------------
# The group run + summary persistence
# ---------------------------------------------------------------------------

def run_group_audit(
    conn,
    *,
    org_id: str,
    site_id: str,
    urls: list[str],
    llm: Any,
    registry: Registry | None = None,
    fetcher_factory: Callable | None = None,
    cost_cap_cents_per_page: float = 250.0,
) -> dict:
    """Run one page audit per URL sequentially, then assemble the rollup.

    `run_page_audit` is reused as-is: it always leaves a terminal audits row
    (done / failed / inconclusive), so every page appears in the rollup with
    its honest status — a broken location is reported, never dropped. Each
    page gets a FRESH cost cap (`cost_cap_cents_per_page`).
    """
    if not urls:
        raise ValueError("run_group_audit requires a non-empty urls list")
    reg = registry if registry is not None else load_registry()

    audit_ids: list[str] = []
    for url in urls:
        audit_id = run_page_audit(
            conn,
            org_id=org_id,
            site_id=site_id,
            url=url,
            llm=llm,
            registry=reg,
            fetcher_factory=fetcher_factory,
            cost_cap_cents=cost_cap_cents_per_page,
        )
        audit_ids.append(audit_id)
        log.info("group audit: %s -> audit %s", url, audit_id)

    return assemble_group(conn, audit_ids, registry=reg)


def persist_group_summary(
    conn,
    *,
    org_id: str,
    site_id: str,
    assembled: dict,
    model_version: str | None = None,
) -> str:
    """Store the assembled rollup as ONE extra audits row.

    url=NULL + gate_state='group_rollup' + scores = the assembled dict (which
    carries member_audit_ids) — share tokens can point at the group report
    without any schema change. Returns the group audit id (str).
    """
    row = conn.execute(
        "insert into audits (org_id, site_id, url, registry_version, model_version, status,"
        " gate_state, scores, started_at, finished_at)"
        " values (%s, %s, null, %s, %s, 'done', %s, %s, now(), now()) returning id",
        (
            org_id,
            site_id,
            str(assembled.get("registry_version") or "unknown"),
            model_version,
            GROUP_GATE_STATE,
            Jsonb(assembled),
        ),
    ).fetchone()
    return str(row["id"])


def handle_audit_group(ctx) -> None:
    """Job handler for type 'audit_group': payload {"urls": [...]}.

    Runs the group audit and persists the group summary row in the same
    org-scoped work transaction the Worker opened."""
    from gm.infra.llm import LlmClient  # deferred: same-wave gateway module

    payload = ctx.job.payload or {}
    urls = payload.get("urls")
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
        raise ValueError(f"job {ctx.job.id}: payload 'urls' must be a non-empty list of strings")
    if ctx.job.org_id is None or ctx.job.site_id is None:
        raise ValueError(f"job {ctx.job.id}: audit_group requires org_id and site_id")

    llm = LlmClient()
    assembled = run_group_audit(
        ctx.conn,
        org_id=str(ctx.job.org_id),
        site_id=str(ctx.job.site_id),
        urls=urls,
        llm=llm,
        cost_cap_cents_per_page=float(payload.get("cost_cap_cents_per_page", 250.0)),
    )
    group_id = persist_group_summary(
        ctx.conn,
        org_id=str(ctx.job.org_id),
        site_id=str(ctx.job.site_id),
        assembled=assembled,
        model_version=getattr(llm, "model", None),
    )
    log.info("group audit job %s: summary audit %s (%d locations)",
             ctx.job.id, group_id, len(urls))
