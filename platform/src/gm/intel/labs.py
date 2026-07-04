"""DataForSEO Labs port — competitor keyword-gap detection (Phase D0).

LabsClient POSTs a one-task array to
/v3/dataforseo_labs/google/ranked_keywords/live with the same Basic-auth /
retry / 200-envelope discipline as serp.DataForSeoClient. The retry and
envelope helpers below are a LOCAL COPY of gm/intel/serp.py's private
_backoff_seconds/_post_json/_envelope_error/_unwrap_task/_cost_cents: they are
not exported there and the D0 contract forbids modifying serp.py, so we copy
them (same semantics, raising the shared serp.SerpError) rather than reaching
into another module's underscore names.

keyword_gap = queries where >= 1 configured competitor (sites.competitor_domains)
ranks <= position_max while the client is absent from BOTH:
  1. rank_history (any recorded rank, i.e. rank IS NOT NULL — a NULL rank means
     we checked and the client does NOT rank, which is exactly a gap), and
  2. 28 days of Search Console data (gsc_window_agg window_days=28 or gsc_daily,
     any impressions > 0),
with volume >= volume_floor (unknown volume fails the floor: no demand evidence).
Duplicates across competitors keep the best row (lowest position, then highest
volume). Queue rows are written by REUSING detectors._upsert_item (the contract
names it _upsert_queue_item; the landed helper is _upsert_item), which enforces
the standard discipline: open rows refresh, dismissed rows reopen only after an
elapsed snooze, actioned/done rows are never touched, vanished targets remain.

Cost: Labs live tasks bill $0.01/task + $0.0001/returned row. We record the
response envelope's `cost` field (dollars -> cents) when present, else fall
back to that formula; one cost_event per competitor call. Phase D4 (WP-I):
keyword_gap checks spend.require_dfs_budget ONCE before the paid competitor
loop and, on BudgetExceeded, returns a zero-cost result whose note carries the
refusal — recorded in the job result, never silently skipped. The relevance
filter is v2 (bigram hit OR >= RELEVANCE_THRESHOLD meaningful-token overlap,
v1 single-token fallback, empty signal passes everything).
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time

import httpx
import psycopg

from gm.infra import jobs
from gm.infra.costs import record_cost
from gm.intel.detectors import _upsert_item
from gm.intel.engines.base import normalize_host
from gm.intel.serp import SerpError, query_norm

log = logging.getLogger(__name__)

BASE_URL = "https://api.dataforseo.com"
RANKED_KEYWORDS_PATH = "/v3/dataforseo_labs/google/ranked_keywords/live"
DOMAIN_RANK_OVERVIEW_PATH = "/v3/dataforseo_labs/google/domain_rank_overview/live"
BULK_TRAFFIC_ESTIMATION_PATH = "/v3/dataforseo_labs/google/bulk_traffic_estimation/live"
COMPETITORS_DOMAIN_PATH = "/v3/dataforseo_labs/google/competitors_domain/live"

PROVIDER = "dataforseo"
DEFAULT_LOCATION_CODE = 2784  # United Arab Emirates
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3

# Labs live pricing, used only when the response envelope carries no cost field.
TASK_COST_CENTS = 1.0        # $0.01 per task
ROW_COST_CENTS = 0.01        # $0.0001 per returned row

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


# --- retry/backoff + envelope (local copy of gm/intel/serp.py's private helpers) -------


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def _post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: list | dict,
    max_retries: int = MAX_RETRIES,
    total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """POST `payload` as JSON and return the parsed JSON body.

    Retries 429/5xx and transport failures (max_retries times, backoff + jitter)
    within a single total time budget. Other HTTP failures raise a non-retryable
    SerpError; exhaustion/timeouts raise a retryable one.
    """
    deadline = time.monotonic() + total_budget_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SerpError(
                f"dataforseo: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
            )
        try:
            resp = client.post(
                url, headers=headers, json=payload, timeout=min(remaining, total_budget_seconds)
            )
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                raise SerpError(
                    f"dataforseo: transport failure after {attempt + 1} attempts: {exc}",
                    retryable=True,
                ) from exc
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                raise SerpError(
                    f"dataforseo: HTTP {resp.status_code} after {attempt + 1} attempts",
                    retryable=True,
                )
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code >= 400:
            raise SerpError(
                f"dataforseo: HTTP {resp.status_code}: {resp.text[:500]}", retryable=False
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SerpError("dataforseo: non-JSON response body", retryable=False) from exc
        if not isinstance(data, dict):
            raise SerpError(
                f"dataforseo: unexpected JSON payload type {type(data).__name__}", retryable=False
            )
        return data


def _envelope_error(status_code: object, message: object, where: str) -> SerpError:
    code = status_code if isinstance(status_code, int) else 0
    retryable = not (40000 <= code < 50000)  # 40xxx = client error -> don't retry
    return SerpError(
        f"dataforseo {where} status {status_code}: {message or 'unknown error'}",
        retryable=retryable,
    )


def _unwrap_task(data: dict) -> dict:
    """Return tasks[0] after checking both envelope levels (20000 = ok)."""
    status = data.get("status_code")
    if status != 20000:
        raise _envelope_error(status, data.get("status_message"), "api")
    tasks = data.get("tasks")
    task = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else None
    if task is None:
        raise SerpError("dataforseo: response has no tasks", retryable=False)
    task_status = task.get("status_code")
    if task_status != 20000:
        raise _envelope_error(task_status, task.get("status_message"), "task")
    return task


def _cost_cents(data: dict, task: dict) -> float:
    """Response `cost` is in dollars; convert to cents (numeric(10,4) column)."""
    for source in (data, task):
        cost = source.get("cost")
        if isinstance(cost, int | float) and cost:
            return float(cost) * 100.0
    return 0.0


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _result_items(task: dict) -> list:
    """items from tasks[0].result[0]; provider returns null result on empty targets."""
    result_list = task.get("result")
    entry = (
        result_list[0]
        if isinstance(result_list, list) and result_list and isinstance(result_list[0], dict)
        else {}
    )
    items = entry.get("items")
    return items if isinstance(items, list) else []


# --- client -----------------------------------------------------------------------------


class LabsClient:
    """Thin DataForSEO Labs client: Basic auth from env, injectable httpx.Client.

    `last_cost_cents` holds the cost of the most recent successful call so
    keyword_gap can record one cost_event per competitor purchase.
    """

    def __init__(
        self,
        login: str | None = None,
        password: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.login = login or os.environ.get("DATAFORSEO_LOGIN", "")
        self.password = password or os.environ.get("DATAFORSEO_PASSWORD", "")
        if not self.login or not self.password:
            raise SerpError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set", retryable=False)
        token = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        self.last_cost_cents = 0.0

    def ranked_keywords(
        self,
        domain: str,
        *,
        location_code: int = DEFAULT_LOCATION_CODE,
        language: str = "en",
        limit: int = 200,
        position_max: int = 20,
    ) -> list[dict]:
        """Organic keywords `domain` ranks for: [{query_norm, position, volume, cpc, url}].

        position comes from ranked_serp_element.serp_item.rank_absolute; volume/cpc
        from keyword_data.keyword_info. position_max is pushed into the request
        filter (so we do not pay for rows we discard) and re-applied defensively.
        """
        payload = [
            {
                "target": domain,
                "location_code": location_code,
                "language_code": language,
                "limit": limit,
                "item_types": ["organic"],
                "filters": ["ranked_serp_element.serp_item.rank_absolute", "<=", position_max],
            }
        ]
        data = _post_json(
            self._client, BASE_URL + RANKED_KEYWORDS_PATH, headers=self._headers, payload=payload
        )
        task = _unwrap_task(data)
        result_list = task.get("result")
        entry = (
            result_list[0]
            if isinstance(result_list, list) and result_list and isinstance(result_list[0], dict)
            else {}
        )
        items = entry.get("items")
        items = items if isinstance(items, list) else []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            keyword_data = item.get("keyword_data") or {}
            keyword = query_norm(str(keyword_data.get("keyword") or ""))
            info = keyword_data.get("keyword_info") or {}
            serp_item = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
            item_type = serp_item.get("type")
            rank = serp_item.get("rank_absolute")
            if not keyword or not isinstance(rank, int) or rank > position_max:
                continue
            if item_type is not None and item_type != "organic":
                continue  # defensive: we ask for organic only, but never trust the wire
            out.append(
                {
                    "query_norm": keyword,
                    "position": rank,
                    "volume": _int_or_none(info.get("search_volume")),
                    "cpc": _float_or_none(info.get("cpc")),
                    "url": str(serp_item.get("url") or ""),
                }
            )
        cost = _cost_cents(data, task)
        if not cost:
            cost = TASK_COST_CENTS + ROW_COST_CENTS * len(items)
        self.last_cost_cents = cost
        return out

    # --- Phase D2 appends (competitor intelligence pack) ---------------------------------

    def domain_rank_overview(
        self,
        domain: str,
        *,
        location_code: int = DEFAULT_LOCATION_CODE,
        language: str = "en",
    ) -> dict | None:
        """Sitewide organic footprint for `domain` (competitor profiles).

        Returns {"total_keywords", "top10_keywords", "pos_1", "movers", "raw"}:
        total_keywords = organic.count, top10_keywords = pos_1 + pos_2_3 + pos_4_10
        (None when the provider sent none of the buckets), movers =
        {"new","up","down","lost"} from is_new/is_up/is_down/is_lost, raw = the
        untouched metrics dict. None when the provider returns no items — the
        caller stores a NULLs row (honest absence, no invention).
        """
        payload = [
            {"target": domain, "location_code": location_code, "language_code": language}
        ]
        data = _post_json(
            self._client,
            BASE_URL + DOMAIN_RANK_OVERVIEW_PATH,
            headers=self._headers,
            payload=payload,
        )
        task = _unwrap_task(data)
        items = _result_items(task)
        cost = _cost_cents(data, task)
        if not cost:
            cost = TASK_COST_CENTS + ROW_COST_CENTS * len(items)
        self.last_cost_cents = cost
        item = next((i for i in items if isinstance(i, dict)), None)
        if item is None:
            return None
        metrics = item.get("metrics") or {}
        organic = metrics.get("organic") or {}
        buckets = [_int_or_none(organic.get(k)) for k in ("pos_1", "pos_2_3", "pos_4_10")]
        top10 = (
            sum(b for b in buckets if b is not None)
            if any(b is not None for b in buckets)
            else None
        )
        return {
            "total_keywords": _int_or_none(organic.get("count")),
            "top10_keywords": top10,
            "pos_1": buckets[0],
            "movers": {
                "new": _int_or_none(organic.get("is_new")),
                "up": _int_or_none(organic.get("is_up")),
                "down": _int_or_none(organic.get("is_down")),
                "lost": _int_or_none(organic.get("is_lost")),
            },
            "raw": metrics,
        }

    def bulk_traffic_estimation(
        self,
        domains: list[str],
        *,
        location_code: int = DEFAULT_LOCATION_CODE,
        language: str = "en",
    ) -> dict:
        """ONE call for all `domains`: {host: {"est_traffic", "total_keywords"}}.

        Keys are normalized hosts; targets the provider returned nothing for are
        absent from the mapping (callers treat absence honestly, never as zero).
        """
        payload = [
            {"targets": list(domains), "location_code": location_code, "language_code": language}
        ]
        data = _post_json(
            self._client,
            BASE_URL + BULK_TRAFFIC_ESTIMATION_PATH,
            headers=self._headers,
            payload=payload,
        )
        task = _unwrap_task(data)
        items = _result_items(task)
        out: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            host = normalize_host(str(item.get("target") or "").strip())
            if not host:
                continue
            organic = (item.get("metrics") or {}).get("organic") or {}
            out[host] = {
                "est_traffic": _float_or_none(organic.get("etv")),
                "total_keywords": _int_or_none(organic.get("count")),
            }
        cost = _cost_cents(data, task)
        if not cost:
            cost = TASK_COST_CENTS + ROW_COST_CENTS * len(items)
        self.last_cost_cents = cost
        return out

    def competitors_domain(
        self,
        domain: str,
        *,
        location_code: int = DEFAULT_LOCATION_CODE,
        language: str = "en",
        limit: int = 30,
    ) -> list[dict]:
        """Intersection-ranked competitor discovery for `domain`.

        [{"domain", "intersections", "avg_position", "their_keywords", "their_etv"}],
        hosts normalized. Items without a host or an integer `intersections` are
        dropped — no overlap evidence, nothing to rank a candidate by.
        """
        payload = [
            {
                "target": domain,
                "location_code": location_code,
                "language_code": language,
                "limit": limit,
            }
        ]
        data = _post_json(
            self._client,
            BASE_URL + COMPETITORS_DOMAIN_PATH,
            headers=self._headers,
            payload=payload,
        )
        task = _unwrap_task(data)
        items = _result_items(task)
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            host = normalize_host(str(item.get("domain") or "").strip())
            intersections = _int_or_none(item.get("intersections"))
            if not host or intersections is None:
                continue
            organic = (item.get("full_domain_metrics") or {}).get("organic") or {}
            out.append(
                {
                    "domain": host,
                    "intersections": intersections,
                    "avg_position": _float_or_none(item.get("avg_position")),
                    "their_keywords": _int_or_none(organic.get("count")),
                    "their_etv": _float_or_none(organic.get("etv")),
                }
            )
        cost = _cost_cents(data, task)
        if not cost:
            cost = TASK_COST_CENTS + ROW_COST_CENTS * len(items)
        self.last_cost_cents = cost
        return out


# --- keyword gap --------------------------------------------------------------------------


def _client_present_queries(conn: psycopg.Connection, site_id) -> set[str]:
    """Normalized queries where the client already shows up, from both sources:
    rank_history with an actual rank, and 28d of GSC data with impressions."""
    present: set[str] = set()
    for row in conn.execute(
        "select distinct query_norm from rank_history where site_id = %s and rank is not null",
        (site_id,),
    ).fetchall():
        present.add(query_norm(row["query_norm"]))
    for row in conn.execute(
        "select distinct query from gsc_window_agg"
        " where site_id = %s and window_days = 28 and impressions > 0",
        (site_id,),
    ).fetchall():
        present.add(query_norm(row["query"]))
    for row in conn.execute(
        "select distinct query from gsc_daily"
        " where site_id = %s and search_type = 'web'"
        " and date > current_date - 28 and impressions > 0",
        (site_id,),
    ).fetchall():
        present.add(query_norm(row["query"]))
    return present


_RELEVANCE_STOPWORDS = frozenset(
    "the and for with how what why when where best top free your our near".split()
)

# Keep a multi-token candidate when at least half its meaningful tokens are on-topic.
RELEVANCE_THRESHOLD = 0.5


def _relevance_signal(conn: psycopg.Connection, site_id) -> dict[str, set[str]]:
    """Topic signal from the site's own tracked queries + brand terms (filter v2).

    {"tokens": set[str], "bigrams": set[str]} — tokens filtered as v1 (len >= 3,
    minus _RELEVANCE_STOPWORDS), bigrams = adjacent RAW-token pairs ("dental
    clinic"). An empty signal means no topical signal is configured — the gap
    filter then passes everything through (small same-vertical competitors need
    no filter; the filter exists for giant content-publisher competitors whose
    top keywords span every topic)."""
    tokens: set[str] = set()
    bigrams: set[str] = set()

    def _add(text: str) -> None:
        raw = text.lower().split()
        tokens.update(raw)
        bigrams.update(f"{a} {b}" for a, b in zip(raw, raw[1:], strict=False))

    for r in conn.execute(
        "select query_norm from tracked_queries where site_id=%s and active", (site_id,)
    ).fetchall():
        _add(r["query_norm"])
    row = conn.execute("select brand_terms from sites where id=%s", (site_id,)).fetchone()
    for t in (row["brand_terms"] if row else None) or []:
        _add(t)
    return {
        "tokens": {t for t in tokens if len(t) >= 3 and t not in _RELEVANCE_STOPWORDS},
        "bigrams": bigrams,
    }


def _passes_relevance(query: str, signal: dict[str, set[str]]) -> bool:
    """Filter v2 keep rule for one candidate query against `_relevance_signal`.

    (1) any adjacent candidate bigram in signal.bigrams -> keep; else
    (2) |candidate meaningful tokens ∩ signal.tokens| / |candidate meaningful
        tokens| >= RELEVANCE_THRESHOLD -> keep; but
    (3) single-meaningful-token candidates, or a signal with NO derivable
        bigrams, fall back to v1's any-token-overlap rule; and
    (4) an empty signal passes everything (unchanged: no signal = no filter).
    """
    tokens, bigrams = signal["tokens"], signal["bigrams"]
    if not tokens and not bigrams:
        return True  # (4) no topical signal configured -> no filter
    raw = query.split()
    if bigrams and any(f"{a} {b}" in bigrams for a, b in zip(raw, raw[1:], strict=False)):
        return True  # (1) bigram hit
    meaningful = {t for t in raw if len(t) >= 3 and t not in _RELEVANCE_STOPWORDS}
    if not bigrams or len(meaningful) < 2:
        return bool(tokens & set(raw))  # (3) v1 fallback: any token overlap
    return len(meaningful & tokens) / len(meaningful) >= RELEVANCE_THRESHOLD  # (2)


def keyword_gap(
    conn: psycopg.Connection,
    *,
    org_id,
    site_id,
    labs_client: LabsClient | None = None,
    volume_floor: int = 10,
    position_max: int = 10,
    per_competitor_limit: int = 200,
) -> dict:
    """Detect queries competitors rank top-`position_max` for while the client is
    absent from both rank_history and 28d GSC; upsert queue_items kind='keyword_gap'.

    Returns {"competitors", "candidates", "queued", "cost_cents", "note"}.
    """
    site = conn.execute(
        "select competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()
    if site is None:
        raise SerpError(f"unknown site_id {site_id}", retryable=False)
    competitors = [d for d in (site["competitor_domains"] or []) if d]
    if not competitors:
        return {
            "competitors": [],
            "candidates": 0,
            "queued": 0,
            "cost_cents": 0.0,
            "note": "no competitor_domains configured for this site; keyword gap skipped",
        }

    # Phase D4 (WP-I): budget guard, checked ONCE before the paid competitor loop.
    # A refusal is recorded in the job result's note, never silently skipped, and
    # costs $0 — nothing was purchased yet. Lazy import: no module-level cycle.
    from gm.intel.spend import BudgetExceeded, require_dfs_budget

    try:
        require_dfs_budget(conn)
    except BudgetExceeded as exc:
        return {
            "competitors": competitors,
            "candidates": 0,
            "queued": 0,
            "cost_cents": 0.0,
            "note": str(exc),
        }

    present = _client_present_queries(conn, site_id)
    relevance = _relevance_signal(conn, site_id)
    labs_client = labs_client or LabsClient()

    best: dict[str, dict] = {}
    total_cost = 0.0
    for domain in competitors:
        rows = labs_client.ranked_keywords(
            domain, limit=per_competitor_limit, position_max=position_max
        )
        cost = float(getattr(labs_client, "last_cost_cents", 0.0) or 0.0)
        total_cost += cost
        record_cost(
            conn,
            provider=PROVIDER,
            purpose="labs_ranked_keywords",
            cost_cents=cost,
            org_id=org_id,
            units={"target": domain, "rows": len(rows)},
        )
        for row in rows:
            if row["position"] > position_max:
                continue  # defensive: ranked_keywords already filters
            volume = row["volume"]
            if volume is None or volume < volume_floor:
                continue
            query = row["query_norm"]
            if query in present:
                continue
            if not _passes_relevance(query, relevance):
                continue  # off-topic for this site (giant-competitor blog noise)
            current = best.get(query)
            # best-of dedupe across competitors: lowest position, then highest volume
            if current is None or (row["position"], -volume) < (
                current["their_position"],
                -current["volume"],
            ):
                best[query] = {
                    "volume": volume,
                    "best_competitor": domain,
                    "their_position": row["position"],
                }

    for query, stake in best.items():
        _upsert_item(
            conn,
            org_id=org_id,
            site_id=site_id,
            kind="keyword_gap",
            target={"query": query},
            at_stake={**stake, "basis": "labs"},
        )
    return {
        "competitors": competitors,
        "candidates": len(best),
        "queued": len(best),
        "cost_cents": round(total_cost, 4),
        "note": None,
    }


def handle_keyword_gap(ctx: jobs.JobContext) -> None:
    """Job 'keyword_gap': payload/job.site_id -> keyword_gap."""
    site_id = ctx.job.site_id or (ctx.job.payload or {}).get("site_id")
    if not site_id:
        raise RuntimeError("keyword_gap job requires site_id")
    org_id = ctx.job.org_id
    if org_id is None:
        row = ctx.conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"site not found: {site_id}")
        org_id = row["org_id"]
    result = keyword_gap(ctx.conn, org_id=org_id, site_id=str(site_id))
    log.info(
        "keyword_gap site=%s competitors=%s queued=%s cost_cents=%s note=%s",
        site_id,
        len(result["competitors"]),
        result["queued"],
        result["cost_cents"],
        result["note"],
    )
