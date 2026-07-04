"""Systematic competitor discovery (Phase D2 WP-B).

discover_competitors asks DataForSEO Labs' competitors_domain endpoint who
shares SERP real estate with the client (one call per run, via
LabsClient.competitors_domain), filters the noise deterministically, and
upserts queue_items kind='competitor_candidate' for operator review:

- the client itself is dropped, subdomain-aware (blog.client.com IS client.com);
- domains already configured in sites.competitor_domains are dropped
  (both sides normalized before comparison);
- gm.audit.compare.DOMAIN_DENYLIST mega-platforms are dropped, subdomain-aware
  (read-only import — a med-spa can't 'out-page' Instagram);
- rows with intersections < MIN_INTERSECTIONS (3) are dropped (too little
  keyword overlap to be a real competitor).

Survivors are ranked by intersections (desc; ties broken by avg_position asc
then domain, for determinism) and the top `limit` are queued. `candidates`
counts every survivor, `queued` the rows actually written after the cap.
Queue rows reuse detectors._upsert_item, so the standard discipline comes
free: open rows refresh, dismissed rows reopen only after an elapsed snooze,
actioned/done rows are never touched.

confirm_candidate appends the host to sites.competitor_domains (normalized,
deduped) and marks the queue row 'actioned'; a missing candidate row still
appends, so `gm site set-competitors` hand-picking stays legal.
dismiss_candidate marks the row 'dismissed' with a snooze.

Cost: the response-envelope cost captured by LabsClient.last_cost_cents (which
already falls back to TASK_COST_CENTS + ROW_COST_CENTS x rows) is recorded as
one cost_event, purpose 'labs_competitors_domain'. MAX_COMPETITORS = 10:
a limit above it is refused with an honest note and ZERO spend — never
silently truncated.
"""

from __future__ import annotations

import logging

import psycopg

from gm.audit.compare import DOMAIN_DENYLIST
from gm.infra import jobs
from gm.infra.costs import record_cost
from gm.intel.detectors import _upsert_item, target_hash
from gm.intel.engines.base import normalize_host
from gm.intel.labs import PROVIDER, LabsClient
from gm.intel.serp import SerpError

log = logging.getLogger(__name__)

KIND = "competitor_candidate"
MAX_COMPETITORS = 10   # COMMON cap, mirrored from the D2 contract
MIN_INTERSECTIONS = 3  # inclusive floor: keep rows with intersections >= 3


# --- filtering (pure) -------------------------------------------------------------------


def _same_site(a: str, b: str) -> bool:
    """Subdomain-aware equality, both ways (local copy of gm.audit.compare's
    private helper — same rationale as labs.py's copied retry helpers)."""
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _denylisted(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DOMAIN_DENYLIST)


def _rank_key(row: dict) -> tuple:
    avg = row.get("avg_position")
    if isinstance(avg, bool) or not isinstance(avg, int | float):
        avg = float("inf")  # unknown position sorts last within its intersections tier
    return (-row["intersections"], float(avg), row["domain"])


def _filter_candidates(
    rows: list[dict],
    *,
    client_host: str,
    configured: list[str],
    limit: int | None = None,
) -> list[dict]:
    """Deterministic filter + rank over competitors_domain rows.

    Drops the client (subdomain-aware), already-configured domains, denylisted
    platforms and rows with intersections < MIN_INTERSECTIONS (or junk rows);
    dedupes by host (first occurrence wins); ranks by intersections desc
    (ties: avg_position asc, then domain) and caps at `limit` when given.
    """
    configured_hosts = {normalize_host(d) for d in configured or [] if d}
    kept: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        host = normalize_host(str(row.get("domain") or ""))
        if not host or host in seen:
            continue
        inter = row.get("intersections")
        if isinstance(inter, bool) or not isinstance(inter, int) or inter < MIN_INTERSECTIONS:
            continue
        if _same_site(host, client_host):
            continue
        if host in configured_hosts:
            continue
        if _denylisted(host):
            continue
        seen.add(host)
        kept.append({**row, "domain": host})
    kept.sort(key=_rank_key)
    return kept if limit is None else kept[:limit]


# --- discovery --------------------------------------------------------------------------


def discover_competitors(
    conn: psycopg.Connection,
    *,
    org_id,
    site_id,
    labs_client: LabsClient | None = None,
    limit: int = 10,
) -> dict:
    """Discover competitor candidates for a site and queue them for review.

    Returns {"candidates", "queued", "cost_cents", "note"} — candidates counts
    every filter survivor, queued the rows upserted after the `limit` cap.
    """
    site = conn.execute(
        "select domain_norm, competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()
    if site is None:
        raise SerpError(f"unknown site_id {site_id}", retryable=False)
    if limit > MAX_COMPETITORS:
        return {
            "candidates": 0,
            "queued": 0,
            "cost_cents": 0.0,
            "note": f"limit {limit} exceeds MAX_COMPETITORS ({MAX_COMPETITORS});"
            " refusing to discover (never silently truncated)",
        }
    if limit < 1:
        return {
            "candidates": 0,
            "queued": 0,
            "cost_cents": 0.0,
            "note": f"limit must be at least 1 (got {limit}); nothing to discover",
        }

    client_host = normalize_host(site["domain_norm"])
    labs_client = labs_client or LabsClient()
    rows = labs_client.competitors_domain(client_host)
    cost = float(getattr(labs_client, "last_cost_cents", 0.0) or 0.0)
    record_cost(
        conn,
        provider=PROVIDER,
        purpose="labs_competitors_domain",
        cost_cents=cost,
        org_id=org_id,
        units={"target": client_host, "rows": len(rows)},
    )

    survivors = _filter_candidates(
        rows, client_host=client_host, configured=site["competitor_domains"] or []
    )
    kept = survivors[:limit]
    for row in kept:
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind=KIND,
            target={"domain": row["domain"]},
            at_stake={
                "intersections": row["intersections"],
                "avg_position": row.get("avg_position"),
                "their_keywords": row.get("their_keywords"),
                "their_etv": row.get("their_etv"),
                "basis": "labs",
            },
        )

    note = None
    if not rows:
        note = f"provider returned no competitor data for {client_host}"
    elif not kept:
        note = (
            "no candidates survived filtering"
            " (client/configured/denylist/intersections < 3)"
        )
    return {
        "candidates": len(survivors),
        "queued": len(kept),
        "cost_cents": round(cost, 4),
        "note": note,
    }


def handle_discover_competitors(ctx: jobs.JobContext) -> None:
    """Job 'discover_competitors': payload {"site_id", "limit": 10} / job.site_id."""
    payload = ctx.job.payload or {}
    site_id = ctx.job.site_id or payload.get("site_id")
    if not site_id:
        raise RuntimeError("discover_competitors job requires site_id")
    org_id = ctx.job.org_id
    if org_id is None:
        row = ctx.conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"site not found: {site_id}")
        org_id = row["org_id"]
    limit = payload.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = 10
    result = discover_competitors(ctx.conn, org_id=org_id, site_id=str(site_id), limit=limit)
    log.info(
        "discover_competitors site=%s candidates=%s queued=%s cost_cents=%s note=%s",
        site_id,
        result["candidates"],
        result["queued"],
        result["cost_cents"],
        result["note"],
    )


# --- operator actions ---------------------------------------------------------------------


def confirm_candidate(conn: psycopg.Connection, *, site_id, domain: str) -> bool:
    """Append `domain` to sites.competitor_domains (normalized, deduped) and mark
    the matching candidate queue row 'actioned' (open or dismissed rows only).

    A missing candidate row still appends — hand-picking stays legal. Returns
    True when the domain was newly appended, False when it was already
    configured (the queue row is actioned either way).
    """
    host = normalize_host(domain)
    if not host:
        raise SerpError(f"cannot normalize domain {domain!r}", retryable=False)
    site = conn.execute(
        "select competitor_domains from sites where id = %s for update", (site_id,)
    ).fetchone()
    if site is None:
        raise SerpError(f"unknown site_id {site_id}", retryable=False)
    configured = list(site["competitor_domains"] or [])
    appended = host not in {normalize_host(d) for d in configured if d}
    if appended:
        conn.execute(
            "update sites set competitor_domains = %s where id = %s",
            (configured + [host], site_id),
        )
    conn.execute(
        "update queue_items set status = 'actioned', snooze_until = null"
        " where site_id = %s and kind = %s and target_hash = %s"
        "   and status in ('open', 'dismissed')",
        (site_id, KIND, target_hash({"domain": host})),
    )
    return appended


def dismiss_candidate(
    conn: psycopg.Connection, *, site_id, domain: str, snooze_days: int = 90
) -> bool:
    """Mark the candidate queue row 'dismissed', snoozed for `snooze_days` —
    discovery re-queues it only after the snooze elapses. Returns True when a
    row was dismissed, False when there is no dismissible candidate
    (actioned/done rows are never touched)."""
    host = normalize_host(domain)
    if not host:
        raise SerpError(f"cannot normalize domain {domain!r}", retryable=False)
    cur = conn.execute(
        "update queue_items set status = 'dismissed',"
        " snooze_until = now() + make_interval(days => %s)"
        " where site_id = %s and kind = %s and target_hash = %s"
        "   and status in ('open', 'dismissed')",
        (snooze_days, site_id, KIND, target_hash({"domain": host})),
    )
    return cur.rowcount > 0
