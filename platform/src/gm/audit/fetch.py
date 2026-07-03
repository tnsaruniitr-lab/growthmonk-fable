"""SSRF-guarded HTTP fetcher for the audit pipeline.

Every fetch validates the requested URL AND every redirect hop through
``gm.audit.safety`` (max 5 redirects, mirroring the source repo's
``curl --max-redirs 5``). A response that is still 3xx after the redirect cap
is returned as-is — the BEV layer classifies that as ``unresolved_redirect``
rather than drawing content conclusions from a redirect body.

The httpx client is injectable so tests can use ``httpx.MockTransport`` and
never touch the network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from gm.audit.safety import validate_url

# Same default browser profile the source repo's bots_eye_view.sh uses.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30.0


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    text: str
    elapsed_ms: int
    redirect_chain: list[str]  # every URL requested, in order (first = requested url)


Fetcher = Callable[[str], FetchResult]


def make_fetcher(client: httpx.Client | None = None, user_agent: str | None = None) -> Fetcher:
    """Build a Fetcher.

    - SSRF-validates the URL and every redirect hop (raises UnsafeURL).
    - Follows at most MAX_REDIRECTS redirects; a final 3xx is returned as-is.
    - 30s timeout when constructing its own client; an injected client keeps
      its own transport/timeout (tests inject httpx.MockTransport clients).
    """
    ua = user_agent or DEFAULT_USER_AGENT
    http = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS))
    headers = {"User-Agent": ua, "Cache-Control": "no-cache"}

    def fetch(url: str) -> FetchResult:
        current = validate_url(url)
        chain = [current]
        started = time.monotonic()
        response = http.request("GET", current, headers=headers, follow_redirects=False)
        redirects = 0
        while response.is_redirect and redirects < MAX_REDIRECTS:
            location = response.headers.get("location")
            if not location:
                break
            current = validate_url(urljoin(current, location))
            chain.append(current)
            redirects += 1
            response = http.request("GET", current, headers=headers, follow_redirects=False)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return FetchResult(
            url=url,
            final_url=current,
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            elapsed_ms=elapsed_ms,
            redirect_chain=chain,
        )

    return fetch
