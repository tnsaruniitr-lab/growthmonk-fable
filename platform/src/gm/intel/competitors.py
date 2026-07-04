"""Competitor profiles — monthly Labs snapshots per configured competitor (Phase D2).

refresh_competitor_profiles buys one domain_rank_overview per stale competitor plus
ONE bulk_traffic_estimation call covering every miss (est_traffic: the bulk number
wins; the overview's raw organic.etv is the fallback when the bulk response lacks
the domain). Reuse-before-buy: a (site, domain) profile row with fetched_at within
max_age_days is a cache hit and costs nothing, so a monthly tick buys each domain
at most once. Rows upsert into the unique (site_id, domain, checked_on) slot —
same-day re-runs are idempotent. A domain the provider knows nothing about stores
a NULLs row (honest absence, never zeros); latest_profile returns None only when a
domain was never fetched — callers render "no data yet".

Costs: one cost_event per paid call (purpose labs_domain_rank_overview per domain,
labs_bulk_traffic for the single bulk call). The profile row's cost_cents carries
that domain's overview call cost; the bulk call's cost lives in its cost_event
only (no invented per-domain allocation). Configs larger than MAX_COMPETITORS are
refused with an honest note — never silently truncated.
"""

from __future__ import annotations

import datetime as dt
import logging

import psycopg
from psycopg.types.json import Jsonb

from gm.infra import jobs
from gm.infra.costs import record_cost
from gm.intel.labs import LabsClient
from gm.intel.serp import SerpError

log = logging.getLogger(__name__)

PROVIDER = "dataforseo"
MAX_COMPETITORS = 10


def refresh_competitor_profiles(
    conn: psycopg.Connection,
    *,
    org_id,
    site_id,
    labs_client: LabsClient | None = None,
    max_age_days: int = 25,
) -> dict:
    """Refresh competitor_profiles for every configured sites.competitor_domains entry.

    Returns {"competitors", "refreshed", "cached", "empty", "cost_cents", "note"}:
    refreshed = bought with data, cached = fresh rows skipped (reuse-before-buy),
    empty = bought but the provider had nothing (NULLs row stored). note is set
    (zero spend) on empty or >MAX_COMPETITORS configs.
    """
    site = conn.execute(
        "select competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()
    if site is None:
        raise SerpError(f"unknown site_id {site_id}", retryable=False)
    competitors = list(dict.fromkeys(d for d in (site["competitor_domains"] or []) if d))
    if not competitors:
        return {
            "competitors": [],
            "refreshed": 0,
            "cached": 0,
            "empty": 0,
            "cost_cents": 0.0,
            "note": "no competitor_domains configured for this site; refresh skipped",
        }
    if len(competitors) > MAX_COMPETITORS:
        return {
            "competitors": competitors,
            "refreshed": 0,
            "cached": 0,
            "empty": 0,
            "cost_cents": 0.0,
            "note": (
                f"{len(competitors)} competitor_domains configured but the max is"
                f" {MAX_COMPETITORS}; refresh refused — trim the list (nothing was bought)"
            ),
        }

    cached = {
        r["domain"]
        for r in conn.execute(
            "select distinct domain from competitor_profiles"
            " where site_id = %s and domain = any(%s)"
            " and fetched_at >= now() - make_interval(days => %s)",
            (site_id, competitors, max_age_days),
        ).fetchall()
    }
    misses = [d for d in competitors if d not in cached]
    if not misses:
        return {
            "competitors": competitors,
            "refreshed": 0,
            "cached": len(cached),
            "empty": 0,
            "cost_cents": 0.0,
            "note": None,
        }

    labs_client = labs_client or LabsClient()
    total_cost = 0.0
    overviews: dict[str, dict | None] = {}
    overview_costs: dict[str, float] = {}
    for domain in misses:
        overviews[domain] = labs_client.domain_rank_overview(domain)
        cost = float(getattr(labs_client, "last_cost_cents", 0.0) or 0.0)
        overview_costs[domain] = cost
        total_cost += cost
        record_cost(
            conn,
            provider=PROVIDER,
            purpose="labs_domain_rank_overview",
            cost_cents=cost,
            org_id=org_id,
            units={"target": domain},
        )
    bulk = labs_client.bulk_traffic_estimation(misses)
    bulk_cost = float(getattr(labs_client, "last_cost_cents", 0.0) or 0.0)
    total_cost += bulk_cost
    record_cost(
        conn,
        provider=PROVIDER,
        purpose="labs_bulk_traffic",
        cost_cents=bulk_cost,
        org_id=org_id,
        units={"targets": misses, "rows": len(bulk)},
    )

    today = dt.date.today()
    refreshed = empty = 0
    for domain in misses:
        overview = overviews[domain]
        traffic = bulk.get(domain)
        if overview is None and traffic is None:
            empty += 1
        else:
            refreshed += 1
        est_traffic = (traffic or {}).get("est_traffic")
        if est_traffic is None and overview is not None:
            raw_etv = ((overview.get("raw") or {}).get("organic") or {}).get("etv")
            est_traffic = float(raw_etv) if isinstance(raw_etv, int | float) else None
        total_keywords = overview["total_keywords"] if overview else None
        if total_keywords is None and traffic is not None:
            total_keywords = traffic.get("total_keywords")
        conn.execute(
            "insert into competitor_profiles"
            " (org_id, site_id, domain, checked_on, total_keywords, top10_keywords,"
            "  est_traffic, movers, raw_metrics, provider, cost_cents)"
            " values (%(org_id)s, %(site_id)s, %(domain)s, %(checked_on)s,"
            "  %(total_keywords)s, %(top10_keywords)s, %(est_traffic)s, %(movers)s,"
            "  %(raw_metrics)s, %(provider)s, %(cost_cents)s)"
            " on conflict (site_id, domain, checked_on) do update set"
            "  total_keywords = excluded.total_keywords,"
            "  top10_keywords = excluded.top10_keywords,"
            "  est_traffic = excluded.est_traffic, movers = excluded.movers,"
            "  raw_metrics = excluded.raw_metrics, cost_cents = excluded.cost_cents,"
            "  fetched_at = now()",
            {
                "org_id": org_id,
                "site_id": site_id,
                "domain": domain,
                "checked_on": today,
                "total_keywords": total_keywords,
                "top10_keywords": overview["top10_keywords"] if overview else None,
                "est_traffic": est_traffic,
                "movers": Jsonb(overview["movers"] if overview else {}),
                "raw_metrics": Jsonb(overview["raw"] if overview else {}),
                "provider": PROVIDER,
                "cost_cents": overview_costs.get(domain, 0.0),
            },
        )
    return {
        "competitors": competitors,
        "refreshed": refreshed,
        "cached": len(cached),
        "empty": empty,
        "cost_cents": round(total_cost, 4),
        "note": None,
    }


def handle_refresh_competitor_profiles(ctx: jobs.JobContext) -> None:
    """Job 'refresh_competitor_profiles': job/payload site_id scopes to one site;
    no site_id means every site with a non-empty competitor_domains list."""
    site_id = ctx.job.site_id or (ctx.job.payload or {}).get("site_id")
    if site_id:
        org_id = ctx.job.org_id
        if org_id is None:
            row = ctx.conn.execute(
                "select org_id from sites where id = %s", (site_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"site not found: {site_id}")
            org_id = row["org_id"]
        targets = [(org_id, str(site_id))]
    else:
        targets = [
            (r["org_id"], str(r["id"]))
            for r in ctx.conn.execute(
                "select id, org_id from sites"
                " where cardinality(competitor_domains) > 0 order by created_at"
            ).fetchall()
        ]
    for org_id, sid in targets:
        result = refresh_competitor_profiles(ctx.conn, org_id=org_id, site_id=sid)
        log.info(
            "refresh_competitor_profiles site=%s competitors=%s refreshed=%s cached=%s"
            " empty=%s cost_cents=%s note=%s",
            sid,
            len(result["competitors"]),
            result["refreshed"],
            result["cached"],
            result["empty"],
            result["cost_cents"],
            result["note"],
        )


def latest_profile(conn: psycopg.Connection, site_id, domain: str) -> dict | None:
    """Newest profile row for (site, domain); None when never fetched — callers
    render "no data yet". A stored NULLs row comes back as a dict of Nones (we
    checked and the provider had nothing), which is different from never-fetched."""
    row = conn.execute(
        "select domain, total_keywords, top10_keywords, est_traffic, movers, checked_on"
        "  from competitor_profiles where site_id = %s and domain = %s"
        " order by checked_on desc, fetched_at desc limit 1",
        (site_id, domain),
    ).fetchone()
    if row is None:
        return None
    return {
        "domain": row["domain"],
        "total_keywords": row["total_keywords"],
        "top10_keywords": row["top10_keywords"],
        "est_traffic": float(row["est_traffic"]) if row["est_traffic"] is not None else None,
        "movers": row["movers"],
        "checked_on": row["checked_on"],
    }
