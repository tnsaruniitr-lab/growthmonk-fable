"""Read-only AdsPort — Google Ads + Meta Insights daily spend readers (Phase D3, WP-G).

Read-only BY CONSTRUCTION (architecture §6): this module holds read scopes and
report/insights endpoints only — no write call of any kind exists anywhere in
it, and tests grep this source file for write-verb URL fragments to document
the guarantee. BLOCKED-ON-CLIENT (HANDOFF §2.6): no client ad account exists
yet, so everything here is built against recorded fixture shapes and is never
run live; tests inject httpx.MockTransport clients.

Credentials never touch the database: connections rows for kind google_ads /
meta_ads carry NULL encrypted_credentials and hold only account ids in meta
(the wa-connect precedent) — tokens live in env only:

  GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_REFRESH_TOKEN,
  GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET   (manager-link pattern)
  META_ADS_TOKEN                                   (BM analyst user, ads_read only)

The retry/backoff pattern is a local copy of the connection-port convention
(429/5xx/transport retried with jittered exponential backoff inside a total
time budget; 401/403 never retried — callers mark the connection broken).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
from typing import Any, Protocol

import httpx
import psycopg

GOOGLE_ADS_BASE = "https://googleads.googleapis.com/v17"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GRAPH_BASE = "https://graph.facebook.com/v20.0"

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3
# Meta action_types counted as platform conversions (deterministic, documented:
# lead/purchase families only — clicks and engagement actions are not conversions).
META_CONVERSION_ACTION_PREFIXES = ("lead", "purchase", "offsite_conversion", "onsite_conversion")

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class AdsError(Exception):
    """Ads API failure. 429/5xx/transport are retryable; 401/403 are not."""

    def __init__(self, message: str, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code

    @property
    def auth_error(self) -> bool:
        """True for 401/403 — the caller should mark the connection broken."""
        return self.status_code in (401, 403)


class AdsReader(Protocol):
    """One connected ad account: daily campaign rows over an inclusive date range."""

    channel: str  # 'google_ads' | 'meta_ads'

    def daily_rows(self, *, since: dt.date, until: dt.date) -> list[dict]:
        """[{"date","campaign_id","campaign_name","spend": float (currency units),
        "currency","clicks": int|None,"platform_conversions": float|None}]"""
        ...


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    payload: dict | None = None,
    max_retries: int = MAX_RETRIES,
    total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """One API call -> parsed JSON (dict or list). Shared retry/backoff transport."""
    deadline = time.monotonic() + total_budget_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdsError(
                f"ads: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
            )
        try:
            resp = client.request(
                method, url, headers=headers, params=params, json=payload,
                timeout=min(remaining, total_budget_seconds),
            )
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                raise AdsError(
                    f"ads: transport failure after {attempt + 1} attempts: {exc}",
                    retryable=True,
                ) from exc
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code in (401, 403):
            raise AdsError(
                f"ads: HTTP {resp.status_code} auth failure: {resp.text[:300]}",
                retryable=False, status_code=resp.status_code,
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                raise AdsError(
                    f"ads: HTTP {resp.status_code} after {attempt + 1} attempts",
                    retryable=True, status_code=resp.status_code,
                )
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code >= 400:
            raise AdsError(
                f"ads: HTTP {resp.status_code}: {resp.text[:500]}",
                retryable=False, status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AdsError("ads: non-JSON response body", retryable=False) from exc


def _opt_int(value: Any) -> int | None:
    """Provider-absent metric -> None, never a fake 0."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GoogleAdsReader:
    """Google Ads searchStream reader for one customer (manager-link pattern).

    Auth: developer token + OAuth refresh-token exchange, all from env (READ
    use only — the sole endpoint hit is googleAds:searchStream, a report read).
    login_customer_id is the manager account the developer token is linked
    through; customer_id is the client account being read.
    """

    channel = "google_ads"

    def __init__(
        self,
        *,
        customer_id: str,
        login_customer_id: str,
        client: httpx.Client | None = None,
    ):
        self.customer_id = str(customer_id).replace("-", "")
        self.login_customer_id = str(login_customer_id).replace("-", "")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._access_token: str | None = None

    def _env(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise AdsError(f"ads: {name} not configured — set the env var", retryable=False)
        return value

    def _token(self) -> str:
        """OAuth access token via refresh-token grant (cached per reader)."""
        if self._access_token:
            return self._access_token
        data = _request(
            self._client, "POST", GOOGLE_TOKEN_URL,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": self._env("GOOGLE_ADS_REFRESH_TOKEN"),
                "client_id": self._env("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": self._env("GOOGLE_ADS_CLIENT_SECRET"),
            },
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise AdsError("ads: token exchange returned no access_token", retryable=False)
        self._access_token = str(token)
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "developer-token": self._env("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "login-customer-id": self.login_customer_id,
        }

    def daily_rows(self, *, since: dt.date, until: dt.date) -> list[dict]:
        """POST googleAds:searchStream — GAQL daily campaign report, cost in micros."""
        gaql = (
            "SELECT segments.date, campaign.id, campaign.name, customer.currency_code,"
            " metrics.cost_micros, metrics.clicks, metrics.conversions"
            " FROM campaign"
            f" WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
        )
        url = f"{GOOGLE_ADS_BASE}/customers/{self.customer_id}/googleAds:searchStream"
        data = _request(self._client, "POST", url, headers=self._headers(),
                        payload={"query": gaql})
        chunks = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        rows: list[dict] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            for result in chunk.get("results") or []:
                if not isinstance(result, dict):
                    continue
                campaign = result.get("campaign") or {}
                metrics = result.get("metrics") or {}
                segments = result.get("segments") or {}
                customer = result.get("customer") or {}
                cost_micros = _opt_float(metrics.get("costMicros"))
                rows.append({
                    "date": segments.get("date"),
                    "campaign_id": str(campaign.get("id") or ""),
                    "campaign_name": campaign.get("name"),
                    "spend": round(cost_micros / 1e6, 2) if cost_micros is not None else 0.0,
                    "currency": customer.get("currencyCode") or "AED",
                    "clicks": _opt_int(metrics.get("clicks")),
                    "platform_conversions": _opt_float(metrics.get("conversions")),
                })
        return rows


class MetaInsightsReader:
    """Meta Marketing API insights reader for one ad account (ads_read only)."""

    channel = "meta_ads"

    def __init__(self, *, act_id: str, token: str | None = None,
                 client: httpx.Client | None = None):
        self.act_id = str(act_id).removeprefix("act_")
        self._token = token or os.environ.get("META_ADS_TOKEN")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    @staticmethod
    def _conversions(actions: Any) -> float | None:
        """Sum of lead/purchase-family action values; None when actions are absent."""
        if not isinstance(actions, list):
            return None
        total, seen = 0.0, False
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("action_type") or "")
            if not action_type.startswith(META_CONVERSION_ACTION_PREFIXES):
                continue
            value = _opt_float(action.get("value"))
            if value is not None:
                total, seen = total + value, True
        return total if seen else None

    def daily_rows(self, *, since: dt.date, until: dt.date) -> list[dict]:
        """GET act_{id}/insights level=campaign&time_increment=1, following paging.next."""
        if not self._token:
            raise AdsError("ads: META_ADS_TOKEN not configured — set the env var",
                           retryable=False)
        url: str | None = f"{GRAPH_BASE}/act_{self.act_id}/insights"
        params: dict | None = {
            "level": "campaign",
            "time_increment": 1,
            "fields": "spend,clicks,actions,campaign_id,campaign_name,account_currency",
            "time_range": json.dumps(
                {"since": since.isoformat(), "until": until.isoformat()}
            ),
            "access_token": self._token,
        }
        rows: list[dict] = []
        while url:
            data = _request(self._client, "GET", url, params=params)
            params = None  # paging.next URLs carry their own query string
            if not isinstance(data, dict):
                raise AdsError("ads: unexpected insights payload shape", retryable=False)
            for entry in data.get("data") or []:
                if not isinstance(entry, dict):
                    continue
                rows.append({
                    "date": entry.get("date_start"),
                    "campaign_id": str(entry.get("campaign_id") or ""),
                    "campaign_name": entry.get("campaign_name"),
                    "spend": _opt_float(entry.get("spend")) or 0.0,
                    "currency": entry.get("account_currency") or "AED",
                    "clicks": _opt_int(entry.get("clicks")),
                    "platform_conversions": self._conversions(entry.get("actions")),
                })
            paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
            url = paging.get("next")
        return rows


def readers_for_site(conn: psycopg.Connection, site_id: Any) -> list[AdsReader]:
    """One reader per status='ok' ads connection for the site.

    Rows carry NULL credentials — meta holds account ids only ({"customer_id",
    "login_customer_id"} for google_ads, {"act_id"} for meta_ads); tokens come
    from env at call time (the wa-connect precedent). Each reader is annotated
    with connection_id so the ingest can mark a broken connection honestly.
    """
    rows = conn.execute(
        "select id, kind, meta from connections"
        " where site_id = %s and kind in ('google_ads','meta_ads') and status = 'ok'"
        " order by kind",
        (site_id,),
    ).fetchall()
    readers: list[AdsReader] = []
    for row in rows:
        meta = row["meta"] if isinstance(row["meta"], dict) else {}
        reader: Any
        if row["kind"] == "google_ads":
            reader = GoogleAdsReader(
                customer_id=str(meta.get("customer_id") or ""),
                login_customer_id=str(meta.get("login_customer_id") or ""),
            )
        else:
            reader = MetaInsightsReader(act_id=str(meta.get("act_id") or ""))
        reader.connection_id = row["id"]
        readers.append(reader)
    return readers
