"""SerpDataPort — DataForSEO adapter + reuse-before-buy caching (Phase C wave 2).

Two paid endpoints, both POSTed as a one-task array with Basic auth:
- /v3/serp/google/organic/live/regular  (cheapest live SERP: ~$0.002/req at depth<=10)
- /v3/keywords_data/google_ads/search_volume/live

DataForSEO wraps errors in a 200 envelope: the HTTP status is 200 even when the
task failed, so after the HTTP layer succeeds we check `status_code` fields —
20000 is ok, 40xxx is a client error (non-retryable SerpError), anything else
is treated as transient (retryable=True, caller may re-enqueue).

`get_snapshot` / `get_volumes` are the reuse-before-buy cache layer: they serve
rows from serp_snapshots / keyword_metrics within a TTL and only hit the paid
API for misses, recording a cost_event (response `cost` is dollars -> cents)
on every purchase.

Retry/backoff below is a local copy of the engines pattern
(gm/intel/engines/__init__.py) raising SerpError instead of EngineError.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from dataclasses import dataclass, field

import httpx
from psycopg.types.json import Jsonb

from gm.infra.costs import record_cost
from gm.intel.engines.base import normalize_host

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dataforseo.com"
SERP_LIVE_PATH = "/v3/serp/google/organic/live/regular"
SEARCH_VOLUME_PATH = "/v3/keywords_data/google_ads/search_volume/live"

PROVIDER = "dataforseo"
DEFAULT_LOCATION = "United Arab Emirates"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class SerpError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def query_norm(q: str) -> str:
    """Canonical cache key: lowercased, whitespace collapsed."""
    return " ".join(q.lower().split())


@dataclass
class SerpResult:
    query: str
    location: str
    organic: list[dict] = field(default_factory=list)
    # feature list: one {"type": ...} per non-organic item type present;
    # the people_also_ask entry carries its question strings under "questions".
    features: list[dict] = field(default_factory=list)
    cost_cents: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def paa_questions(self) -> list[str]:
        for feature in self.features:
            if feature.get("type") == "people_also_ask":
                return list(feature.get("questions") or [])
        return []


# --- retry/backoff (local copy of the engines pattern) ---------------------------------


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


# --- 200-envelope handling --------------------------------------------------------------


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


# --- normalization ----------------------------------------------------------------------


def _normalize_items(items: list) -> tuple[list[dict], list[dict]]:
    """SERP items -> (ranked organic entries, feature list with PAA questions)."""
    organic: list[dict] = []
    features: list[dict] = []
    feature_by_type: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if not item_type:
            continue
        if item_type == "organic":
            url = str(item.get("url") or "")
            rank = item.get("rank_group")
            organic.append(
                {
                    "rank": rank if isinstance(rank, int) else None,
                    "url": url,
                    "domain": str(item.get("domain") or "") or (normalize_host(url) if url else ""),
                    "title": str(item.get("title") or ""),
                    "description": str(item.get("description") or ""),
                    "type": item_type,
                }
            )
            continue
        feature = feature_by_type.get(item_type)
        if feature is None:
            feature = {"type": item_type}
            feature_by_type[item_type] = feature
            features.append(feature)
        if item_type == "people_also_ask":
            questions: list = feature.setdefault("questions", [])
            for sub in item.get("items") or []:
                if not isinstance(sub, dict):
                    continue
                question = str(sub.get("title") or "").strip()
                if question and question not in questions:
                    questions.append(question)
        if item_type == "ai_overview":
            # Phase D0 (agent A): retain the AI Overview parse on the feature so
            # serp_snapshots.features carries it — rank_tracker reads AIO citation
            # data from the snapshot without needing the (unstored) raw response.
            _collect_aio_hosts(item, feature.setdefault("cited_domains", []))
    for position, entry in enumerate(organic, start=1):
        if entry["rank"] is None:
            entry["rank"] = position
    organic.sort(key=lambda entry: entry["rank"])
    return organic, features


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _competition(entry: dict) -> float | None:
    """Prefer competition_index (0-100) -> 0..1; fall back to a numeric competition."""
    index = entry.get("competition_index")
    if isinstance(index, int | float):
        return float(index) / 100.0
    value = entry.get("competition")  # google_ads returns a LOW/MEDIUM/HIGH string here
    if isinstance(value, int | float):
        return float(value)
    return None


# --- client -----------------------------------------------------------------------------


class DataForSeoClient:
    """Thin DataForSEO client: Basic auth from env, injectable httpx.Client.

    `last_cost_cents` holds the cost of the most recent successful call so the
    cache layer can record cost_events for calls whose return value has no cost
    field (search_volume returns a plain metrics dict per the port contract).
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

    def serp_live(
        self,
        query: str,
        *,
        location: str = DEFAULT_LOCATION,
        language: str = "en",
        depth: int = 10,
    ) -> SerpResult:
        payload = [
            {
                "keyword": query,
                "location_name": location,
                "language_code": language,
                "depth": depth,
            }
        ]
        data = _post_json(
            self._client, BASE_URL + SERP_LIVE_PATH, headers=self._headers, payload=payload
        )
        task = _unwrap_task(data)
        cost_cents = _cost_cents(data, task)
        self.last_cost_cents = cost_cents
        result_list = task.get("result")
        entry = (
            result_list[0]
            if isinstance(result_list, list) and result_list and isinstance(result_list[0], dict)
            else {}
        )
        organic, features = _normalize_items(entry.get("items") or [])
        return SerpResult(
            query=query,
            location=location,
            organic=organic,
            features=features,
            cost_cents=cost_cents,
            raw=data,
        )

    def search_volume(
        self, queries: list[str], *, location_code: int = 2784, language: str = "en"
    ) -> dict:
        """{query_norm: {volume, cpc, competition}}; low-volume terms come back None."""
        keywords: list[str] = []
        for q in queries:
            nq = query_norm(q)
            if nq and nq not in keywords:
                keywords.append(nq)
        if not keywords:
            return {}
        payload = [
            {"keywords": keywords, "location_code": location_code, "language_code": language}
        ]
        data = _post_json(
            self._client, BASE_URL + SEARCH_VOLUME_PATH, headers=self._headers, payload=payload
        )
        task = _unwrap_task(data)
        self.last_cost_cents = _cost_cents(data, task)
        out: dict[str, dict] = {}
        for entry in task.get("result") or []:
            if not isinstance(entry, dict):
                continue
            keyword = query_norm(str(entry.get("keyword") or ""))
            if not keyword:
                continue
            out[keyword] = {
                "volume": _int_or_none(entry.get("search_volume")),
                "cpc": _float_or_none(entry.get("cpc")),
                "competition": _competition(entry),
            }
        return out


# --- reuse-before-buy cache layer --------------------------------------------------------


def _site_org(conn, site_id) -> object:
    row = conn.execute("select org_id from sites where id = %s", (site_id,)).fetchone()
    if row is None:
        raise SerpError(f"unknown site_id {site_id}", retryable=False)
    return row["org_id"]


def get_snapshot(
    conn,
    site_id,
    query: str,
    *,
    max_age_days: int = 7,
    client: DataForSeoClient | None = None,
    location: str = DEFAULT_LOCATION,
) -> dict:
    """Latest serp_snapshots row within the TTL, else buy one via `client`.

    Returns {id, results, features, fetched_at, fresh}: fresh=True means the
    snapshot was just purchased (a cost_event was recorded), False = cache hit.
    """
    q = query_norm(query)
    row = conn.execute(
        "select id, results, features, fetched_at from serp_snapshots"
        " where site_id = %s and query_norm = %s and location = %s"
        " and fetched_at > now() - make_interval(days => %s)"
        " order by fetched_at desc limit 1",
        (site_id, q, location, max_age_days),
    ).fetchone()
    if row is not None:
        return {
            "id": str(row["id"]),
            "results": row["results"],
            "features": row["features"],
            "fetched_at": row["fetched_at"],
            "fresh": False,
        }
    org_id = _site_org(conn, site_id)
    client = client or DataForSeoClient()
    result = client.serp_live(query, location=location)
    inserted = conn.execute(
        "insert into serp_snapshots"
        " (org_id, site_id, query_norm, location, results, features, provider, cost_cents)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s) returning id, fetched_at",
        (
            org_id,
            site_id,
            q,
            location,
            Jsonb(result.organic),
            Jsonb(result.features),
            PROVIDER,
            result.cost_cents,
        ),
    ).fetchone()
    record_cost(
        conn,
        provider=PROVIDER,
        purpose="serp_live",
        cost_cents=result.cost_cents,
        org_id=org_id,
        units={"query": q, "location": location},
    )
    return {
        "id": str(inserted["id"]),
        "results": result.organic,
        "features": result.features,
        "fetched_at": inserted["fetched_at"],
        "fresh": True,
    }


def get_volumes(
    conn,
    site_id,
    queries: list[str],
    *,
    max_age_days: int = 30,
    client: DataForSeoClient | None = None,
) -> dict:
    """{query_norm: {volume, cpc, competition}} served from keyword_metrics within
    the TTL; only the misses are bought (one API call + one cost_event). Terms the
    provider returns nothing for are stored as all-None rows so they are not
    re-bought on every call.
    """
    wanted: list[str] = []
    for q in queries:
        nq = query_norm(q)
        if nq and nq not in wanted:
            wanted.append(nq)
    if not wanted:
        return {}
    rows = conn.execute(
        "select query_norm, volume, cpc, competition from keyword_metrics"
        " where site_id = %s and query_norm = any(%s)"
        " and fetched_at > now() - make_interval(days => %s)",
        (site_id, wanted, max_age_days),
    ).fetchall()
    out: dict[str, dict] = {
        r["query_norm"]: {
            "volume": r["volume"],
            "cpc": float(r["cpc"]) if r["cpc"] is not None else None,
            "competition": r["competition"],
        }
        for r in rows
    }
    missing = [q for q in wanted if q not in out]
    if not missing:
        return out
    org_id = _site_org(conn, site_id)
    client = client or DataForSeoClient()
    fetched = client.search_volume(missing)
    for q in missing:
        metrics = fetched.get(q) or {"volume": None, "cpc": None, "competition": None}
        conn.execute(
            "insert into keyword_metrics"
            " (org_id, site_id, query_norm, volume, cpc, competition, provider)"
            " values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict (site_id, query_norm) do update set"
            " volume = excluded.volume, cpc = excluded.cpc,"
            " competition = excluded.competition, provider = excluded.provider,"
            " fetched_at = now()",
            (
                org_id,
                site_id,
                q,
                metrics["volume"],
                metrics["cpc"],
                metrics["competition"],
                PROVIDER,
            ),
        )
        out[q] = metrics
    record_cost(
        conn,
        provider=PROVIDER,
        purpose="search_volume",
        cost_cents=float(getattr(client, "last_cost_cents", 0.0) or 0.0),
        org_id=org_id,
        units={"keywords": len(missing)},
    )
    return out


# --- AI Overview extraction (Phase D0, agent A — append-only) -----------------------------


def _collect_aio_hosts(node: object, out: list) -> None:
    """Recursively collect every url/domain field under `node` as a normalized host.

    DataForSEO's ai_overview item shape varies (references / items / links arrays,
    nested elements), so instead of hard-coding one path we walk the whole item and
    normalize whatever url/domain values we find. Appends to `out` in encounter
    order, deduplicated.
    """
    if isinstance(node, dict):
        for key in ("url", "domain"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                host = normalize_host(value.strip())
                if host and host not in out:
                    out.append(host)
        for value in node.values():
            if isinstance(value, dict | list):
                _collect_aio_hosts(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_aio_hosts(value, out)


def _serp_items(raw_response: object) -> list:
    """tasks[0].result[0].items of a serp_live raw response; [] on any malformed level."""
    if not isinstance(raw_response, dict):
        return []
    tasks = raw_response.get("tasks")
    if not (isinstance(tasks, list) and tasks and isinstance(tasks[0], dict)):
        return []
    result = tasks[0].get("result")
    if not (isinstance(result, list) and result and isinstance(result[0], dict)):
        return []
    items = result[0].get("items")
    return items if isinstance(items, list) else []


def extract_ai_overview(raw_response: object) -> dict:
    """{"present": bool, "cited_domains": [normalized hosts]} from a serp_live raw response.

    Defensive across response-shape variants: an absent ai_overview item means
    present=False; when present, every url/domain field under the item (references,
    items, links, nested elements) is collected via normalize_host.
    """
    cited: list[str] = []
    present = False
    for item in _serp_items(raw_response):
        if isinstance(item, dict) and str(item.get("type") or "") == "ai_overview":
            present = True
            _collect_aio_hosts(item, cited)
    return {"present": present, "cited_domains": cited}
