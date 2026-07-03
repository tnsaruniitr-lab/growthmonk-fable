"""Comparative audit — "what are my competitors doing better, precisely".

The flow (docs/phase-c-wave2-contracts.md):

  1. SERP snapshot via `gm.intel.serp.get_snapshot` (reuse-before-buy lives
     there; imported lazily because the module is built in the same wave).
     The client's own rank, when present, is recorded in the summary.
  2. `pick_competitors`: the top organic entries ranked ABOVE the client (or
     the top entries when the client is absent), excluding the client's own
     domain subdomain-aware, non-auditable result types (maps/video/...), and
     mega-platforms via DOMAIN_DENYLIST — a med-spa can't 'out-page' Instagram.
  3. Client audit: the latest done audit of the client page if fresh
     (< CLIENT_AUDIT_MAX_AGE_DAYS), else one fresh `run_page_audit`.
  4. Competitor audits REUSE `run_page_audit` unchanged (it owns its own
     status writes): the audits row gets the CLIENT's site_id and the
     competitor URL, and AFTER the pipeline finishes we apply exactly one
     UPDATE that overrides gate_state to 'competitor_reference' (stashing the
     original gate_state into scores for honesty). That tag is what keeps
     competitor rows out of the client's own history/deltas: delta.py is pure
     (explicit findings lists), group.py loads explicit audit ids, and this
     module's own latest-client-audit lookup filters the tag out.
  5. Gap math (`compute_gaps`, pure): checks where the client is fail/warn
     AND >= ceil(n/2) of the n audited (status='done') competitors pass,
     ordered by severity_rank * registry weight (worst first).
  6. Persist ONE serp_comparisons row; return its id.

No competitor `sites` rows are ever created. (run_page_audit does upsert a
`pages` row per competitor URL under the client's site — audit-scoped surfaces
join on the client page's url_norm, so these never collide with client pages.)
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb

from gm.audit.group import DEFAULT_SEVERITY, SEVERITY_RANK, load_group_rows
from gm.audit.pipeline import canonicalize_url, run_page_audit
from gm.audit.registry import Registry, load_registry
from gm.intel.engines.base import normalize_host

log = logging.getLogger(__name__)

JOB_TYPE = "compare_serp"

# gate_state tag applied to competitor audits AFTER run_page_audit finishes.
COMPETITOR_GATE_STATE = "competitor_reference"

# A client audit younger than this is reused instead of re-run.
CLIENT_AUDIT_MAX_AGE_DAYS = 14

# Mega-platforms a local business can't 'out-page' — never picked as competitors.
# Matched subdomain-aware (entry == host or host endswith ".entry").
DOMAIN_DENYLIST = frozenset({
    "instagram.com", "facebook.com", "youtube.com", "tiktok.com", "twitter.com",
    "x.com", "pinterest.com", "linkedin.com", "reddit.com", "quora.com",
    "medium.com", "wikipedia.org", "tripadvisor.com", "booking.com", "yelp.com",
    "trustpilot.com", "groupon.com", "glassdoor.com", "amazon.com", "google.com",
    "apple.com", "justdial.com", "yellowpages.com",
})

# Normalized SERP result types that cannot be page-audited.
NON_AUDITABLE_TYPES = frozenset({
    "map", "maps", "local_pack", "video", "images", "image", "twitter", "app",
    "knowledge_graph", "people_also_ask",
})


def query_norm(q: str) -> str:
    """Contract-pinned normalization (mirrors gm.intel.serp.query_norm)."""
    return " ".join(q.lower().split())


# ---------------------------------------------------------------------------
# Competitor selection (pure)
# ---------------------------------------------------------------------------

def _host_of(entry: dict) -> str:
    """Normalized host of a SERP entry — its domain field, else its url."""
    return normalize_host(str(entry.get("domain") or entry.get("url") or ""))


def _same_site(a: str, b: str) -> bool:
    """Subdomain-aware equality: blog.client.com IS client.com (both ways —
    over-excluding the client beats auditing it as its own competitor)."""
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _denylisted(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DOMAIN_DENYLIST)


def _rank_of(entry: dict) -> int:
    r = entry.get("rank")
    if isinstance(r, bool) or not isinstance(r, int):
        return 10**6  # rankless entries sort last
    return r


def pick_competitors(results: list[dict], client_domain: str, *, limit: int = 3) -> list[dict]:
    """Top organic entries above (or absent) the client, excluding the client's
    own domain (subdomain-aware), non-auditable types, and DOMAIN_DENYLIST
    platforms. One entry per host; at most `limit`."""
    client = normalize_host(client_domain)
    entries = sorted(
        (e for e in results or [] if isinstance(e, dict) and e.get("url")),
        key=_rank_of,
    )
    client_rank = next((_rank_of(e) for e in entries if _same_site(_host_of(e), client)), None)

    picked: list[dict] = []
    seen_hosts: set[str] = set()
    for e in entries:
        host = _host_of(e)
        if _same_site(host, client):
            continue
        if client_rank is not None and _rank_of(e) >= client_rank:
            continue  # only entries ABOVE the client (absent client -> take the top)
        if str(e.get("type") or "organic").lower() in NON_AUDITABLE_TYPES:
            continue
        if _denylisted(host) or host in seen_hosts:
            continue
        seen_hosts.add(host)
        picked.append(e)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Gap math (pure)
# ---------------------------------------------------------------------------

_CLIENT_BAD = frozenset({"fail", "warn"})


def _impact(registry: Registry, check_id: str) -> float:
    """severity_rank * registry weight — same ordering metric as group.py."""
    check = registry.checks.get(check_id) or {}
    sev = str(check.get("severity") or "").strip().lower()
    rank = SEVERITY_RANK.get(sev, SEVERITY_RANK[DEFAULT_SEVERITY])
    return rank * registry.weight_of(check_id)


def compute_gaps(
    client_findings: list[dict],
    competitor_findings_by_url: dict[str, list[dict]],
    registry: Registry,
) -> list[dict]:
    """Checks where the client is fail/warn AND >= ceil(n/2) of the n audited
    competitors pass. `competitor_findings_by_url` must contain ONLY audited
    (status='done') competitors. Ordered by severity*weight, worst first."""
    n = len(competitor_findings_by_url)
    if n == 0:
        return []
    threshold = math.ceil(n / 2)

    status_by_url: dict[str, dict[str, str]] = {
        url: {
            str(f.get("check_id")): str(f.get("status") or "").lower()
            for f in findings or []
            if isinstance(f, dict) and f.get("check_id")
        }
        for url, findings in competitor_findings_by_url.items()
    }

    gaps: list[dict] = []
    for f in client_findings or []:
        cid = str(f.get("check_id") or "")
        status = str(f.get("status") or "").lower()
        if not cid or status not in _CLIENT_BAD:
            continue
        passing = [url for url, statuses in status_by_url.items() if statuses.get(cid) == "pass"]
        if len(passing) < threshold:
            continue
        check = registry.checks.get(cid) or {}
        gaps.append({
            "check_id": cid,
            "name": str(check.get("name") or cid),
            "severity": str(check.get("severity") or DEFAULT_SEVERITY),
            "client_status": status,
            "competitors_passing": len(passing),
            "competitor_urls": sorted(passing),
        })
    gaps.sort(key=lambda g: (-_impact(registry, g["check_id"]), g["check_id"]))
    return gaps


# ---------------------------------------------------------------------------
# DB pieces
# ---------------------------------------------------------------------------

def _serp_get_snapshot(conn, site_id, query: str, *, client=None) -> dict:
    """Indirection over gm.intel.serp.get_snapshot — imported lazily (built in
    the same wave); tests monkeypatch this function."""
    from gm.intel import serp

    return serp.get_snapshot(conn, site_id, query, client=client)


def _latest_client_audit(
    conn, site_id, page_url: str, *, max_age_days: int = CLIENT_AUDIT_MAX_AGE_DAYS
) -> str | None:
    """Latest done audit of the client page within the freshness window.
    Competitor-reference rows are explicitly excluded — they are not client
    history even though they carry the client's site_id."""
    row = conn.execute(
        "select a.id from audits a join pages p on p.id = a.page_id"
        " where p.site_id = %s and p.url_norm = %s and a.status = 'done'"
        " and coalesce(a.gate_state, '') <> %s"
        " and coalesce(a.finished_at, a.created_at) > now() - make_interval(days => %s)"
        " order by a.finished_at desc nulls last limit 1",
        (site_id, canonicalize_url(page_url), COMPETITOR_GATE_STATE, max_age_days),
    ).fetchone()
    return str(row["id"]) if row else None


def _tag_competitor_audit(conn, audit_id: str) -> None:
    """The single post-audit UPDATE: override gate_state to
    'competitor_reference' (run_page_audit owns all other status writes) while
    stashing the pipeline's own gate_state into scores for honesty."""
    conn.execute(
        "update audits set gate_state = %s,"
        " scores = scores || jsonb_build_object('original_gate_state', gate_state)"
        " where id = %s",
        (COMPETITOR_GATE_STATE, audit_id),
    )


def _score_of(scores: dict | None) -> float | None:
    v = (scores or {}).get("overall_score")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


# ---------------------------------------------------------------------------
# The comparison run
# ---------------------------------------------------------------------------

def run_comparison(
    conn,
    *,
    org_id: str,
    site_id: str,
    query: str,
    llm: Any,
    client_page_url: str | None = None,
    registry: Registry | None = None,
    fetcher_factory: Callable | None = None,
    serp_client: Any = None,
    cost_cap_cents_per_page: float = 150.0,
) -> str:
    """Run the comparative audit for one query. Returns serp_comparisons.id."""
    reg = registry if registry is not None else load_registry()
    qn = query_norm(query)

    site = conn.execute("select domain_norm from sites where id = %s", (site_id,)).fetchone()
    if site is None:
        raise ValueError(f"site {site_id} not found (or not visible in org context)")
    client_domain = str(site["domain_norm"])
    client_host = normalize_host(client_domain)

    # 1. Snapshot (reuse-before-buy inside serp.get_snapshot).
    snapshot = _serp_get_snapshot(conn, site_id, query, client=serp_client)
    results = [e for e in (snapshot.get("results") or []) if isinstance(e, dict)]

    client_entries = sorted(
        (e for e in results if _same_site(_host_of(e), client_host)), key=_rank_of
    )
    client_entry = client_entries[0] if client_entries else None
    client_rank = _rank_of(client_entry) if client_entry is not None else None

    # 2. Competitors (2-3; selection rules in pick_competitors).
    competitors = pick_competitors(results, client_domain)

    # 3. Client audit: fresh existing one, else run one now.
    page_url = (
        client_page_url
        or (str(client_entry["url"]) if client_entry is not None else None)
        or f"https://{client_domain}/"
    )
    client_audit_id = _latest_client_audit(conn, site_id, page_url)
    if client_audit_id is None:
        client_audit_id = run_page_audit(
            conn, org_id=org_id, site_id=site_id, url=page_url, llm=llm, registry=reg,
            fetcher_factory=fetcher_factory, cost_cap_cents=cost_cap_cents_per_page,
        )

    # 4. Competitor audits: run_page_audit reused unchanged (client site_id,
    #    competitor URL), then exactly one UPDATE to tag the row.
    competitor_audit_ids: list[str] = []
    for comp in competitors:
        audit_id = run_page_audit(
            conn, org_id=org_id, site_id=site_id, url=str(comp["url"]), llm=llm,
            registry=reg, fetcher_factory=fetcher_factory,
            cost_cap_cents=cost_cap_cents_per_page,
        )
        _tag_competitor_audit(conn, audit_id)
        competitor_audit_ids.append(audit_id)
        log.info("comparison %r: competitor %s -> audit %s", qn, comp["url"], audit_id)

    # 5. Gap math over persisted findings (done competitors only).
    rows = load_group_rows(conn, [client_audit_id, *competitor_audit_ids])
    client_row, competitor_rows = rows[0], rows[1:]
    audited = [r for r in competitor_rows if r["status"] == "done"]
    gaps = compute_gaps(
        client_row["findings"], {r["url"]: r["findings"] for r in audited}, reg
    )

    comp_scores = [s for s in (_score_of(r["scores"]) for r in audited) if s is not None]
    summary = {
        "query_norm": qn,
        "client_rank": client_rank,
        "client_url": page_url,
        "client_audit_status": client_row["status"],
        "competitor_ranks": [
            {"rank": e.get("rank"), "url": e.get("url"), "domain": _host_of(e)}
            for e in competitors
        ],
        "competitors_audited": len(audited),
        "avg_scores": {
            "client": _score_of(client_row["scores"]),
            "competitors": round(sum(comp_scores) / len(comp_scores), 1)
            if comp_scores else None,
        },
    }

    # 6. Persist.
    snapshot_id = snapshot.get("id")
    row = conn.execute(
        "insert into serp_comparisons (org_id, site_id, query_norm, snapshot_id,"
        " client_audit_id, competitor_audit_ids, gaps, summary)"
        " values (%s, %s, %s, %s, %s, %s::uuid[], %s, %s) returning id",
        (
            org_id, site_id, qn,
            str(snapshot_id) if snapshot_id else None,
            client_audit_id, competitor_audit_ids, Jsonb(gaps), Jsonb(summary),
        ),
    ).fetchone()
    return str(row["id"])


def handle_compare_serp(ctx) -> None:
    """Job handler for type 'compare_serp': payload {"query": ..., "page"?: ...}."""
    from gm.infra.llm import LlmClient  # deferred: heavier module, workers only

    payload = ctx.job.payload or {}
    query = payload.get("query")
    if not query or not isinstance(query, str):
        raise ValueError(f"job {ctx.job.id}: payload missing 'query'")
    if ctx.job.org_id is None or ctx.job.site_id is None:
        raise ValueError(f"job {ctx.job.id}: compare_serp requires org_id and site_id")

    comparison_id = run_comparison(
        ctx.conn,
        org_id=str(ctx.job.org_id),
        site_id=str(ctx.job.site_id),
        query=query,
        llm=LlmClient(),
        client_page_url=payload.get("page"),
        cost_cap_cents_per_page=float(payload.get("cost_cap_cents_per_page", 150.0)),
    )
    log.info("compare_serp job %s: comparison %s", ctx.job.id, comparison_id)
