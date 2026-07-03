"""Google Search Console client (service-account path, Phase C wave-1 contract).

Token acquisition goes through google-auth service_account Credentials scoped to
webmasters.readonly; all API traffic goes through httpx with the retry/backoff
pattern copied locally from gm.intel.engines (429/5xx/transport retries within a
total time budget). 401/403 raise GscAuthError so callers can mark the connection
broken. google-auth's refresh uses an httpx-backed transport adapter (the
`requests` package is deliberately not a dependency).
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from datetime import date
from urllib.parse import quote

import httpx
from google.auth.transport import Request as AuthRequest
from google.auth.transport import Response as AuthResponse
from google.oauth2 import service_account

API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
PAGE_SIZE = 25_000

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class GscError(Exception):
    """Non-auth GSC API failure."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class GscAuthError(GscError):
    """401/403 from the GSC API — caller should mark the connection broken."""


class _HttpxAuthResponse(AuthResponse):
    def __init__(self, resp: httpx.Response):
        self._resp = resp

    @property
    def status(self) -> int:
        return self._resp.status_code

    @property
    def headers(self):
        return dict(self._resp.headers)

    @property
    def data(self) -> bytes:
        return self._resp.content


class _HttpxAuthRequest(AuthRequest):
    """google.auth.transport.Request implemented over an httpx.Client."""

    def __init__(self, client: httpx.Client):
        self._client = client

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        resp = self._client.request(
            method,
            url,
            content=body,
            headers=headers,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
        )
        return _HttpxAuthResponse(resp)


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict | None = None,
    max_retries: int = MAX_RETRIES,
    total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Issue one API call and return the parsed JSON body.

    Retries 429/5xx and transport failures (max_retries times, backoff + jitter)
    within a single total time budget. 401/403 raise GscAuthError immediately;
    other 4xx raise a non-retryable GscError; exhaustion raises a retryable one.
    """
    deadline = time.monotonic() + total_budget_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GscError(
                f"gsc: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
            )
        try:
            resp = client.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=min(remaining, total_budget_seconds),
            )
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                raise GscError(
                    f"gsc: transport failure after {attempt + 1} attempts: {exc}",
                    retryable=True,
                ) from exc
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code in (401, 403):
            raise GscAuthError(f"gsc: HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                raise GscError(
                    f"gsc: HTTP {resp.status_code} after {attempt + 1} attempts", retryable=True
                )
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code >= 400:
            raise GscError(f"gsc: HTTP {resp.status_code}: {resp.text[:500]}", retryable=False)
        try:
            data = resp.json()
        except ValueError as exc:
            raise GscError("gsc: non-JSON response body", retryable=False) from exc
        if not isinstance(data, dict):
            raise GscError(
                f"gsc: unexpected JSON payload type {type(data).__name__}", retryable=False
            )
        return data


def _fmt_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


class GscClient:
    """Search Console Search Analytics client for one property.

    `property_url` is either a URL-prefix property ("https://example.com/") or a
    domain property ("sc-domain:example.com"); both are percent-encoded into the
    request path. Pass `credentials` to inject a fake token source in tests —
    otherwise service-account credentials are built from `service_account_info`.
    """

    def __init__(
        self,
        service_account_info: dict,
        property_url: str,
        client: httpx.Client | None = None,
        credentials=None,
    ):
        self.property_url = property_url
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        if credentials is None:
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=SCOPES
            )
        self._credentials = credentials

    def _headers(self) -> dict[str, str]:
        if not getattr(self._credentials, "valid", False):
            self._credentials.refresh(_HttpxAuthRequest(self._client))
        return {"Authorization": f"Bearer {self._credentials.token}"}

    @property
    def _property_path(self) -> str:
        return quote(self.property_url, safe="")

    def query(
        self,
        *,
        start_date: date | str,
        end_date: date | str,
        dimensions: list[str],
        row_limit: int = PAGE_SIZE,
        start_row: int = 0,
        search_type: str = "web",
        data_state: str = "final",
    ) -> list[dict]:
        """One Search Analytics page: list of {keys, clicks, impressions, ctr, position}."""
        url = f"{API_BASE}/sites/{self._property_path}/searchAnalytics/query"
        payload = {
            "startDate": _fmt_date(start_date),
            "endDate": _fmt_date(end_date),
            "dimensions": dimensions,
            "rowLimit": row_limit,
            "startRow": start_row,
            "type": search_type,
            "dataState": data_state,
        }
        data = _request_json(self._client, "POST", url, headers=self._headers(), payload=payload)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []

    def query_all(self, **kw) -> Iterator[list[dict]]:
        """Paginate query() by advancing start_row one page at a time until empty."""
        row_limit = kw.pop("row_limit", PAGE_SIZE)
        start_row = kw.pop("start_row", 0)
        while True:
            rows = self.query(row_limit=row_limit, start_row=start_row, **kw)
            if not rows:
                return
            yield rows
            start_row += row_limit

    def list_sites(self) -> list[dict]:
        """Properties visible to the service account (siteEntry list)."""
        data = _request_json(self._client, "GET", f"{API_BASE}/sites", headers=self._headers())
        entries = data.get("siteEntry")
        return entries if isinstance(entries, list) else []
