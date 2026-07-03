"""Perplexity chat-completions adapter (search grounding is built in)."""

from __future__ import annotations

import os

import httpx

from gm.intel.engines import DEFAULT_TIMEOUT_SECONDS, post_json
from gm.intel.engines.base import EngineAdapter, EngineError, EngineSample

API_URL = "https://api.perplexity.ai/chat/completions"

# Approximate USD per 1M tokens (input, output). Conservative snapshots by
# longest-prefix match; unknown models fall back to sonar-pro pricing (the
# priciest common tier) so estimates err high. Perplexity's per-request
# search fees are not modeled here.
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "sonar-reasoning-pro": (2.00, 8.00),
    "sonar-reasoning": (1.00, 5.00),
    "sonar-deep-research": (2.00, 8.00),
    "sonar-pro": (3.00, 15.00),
    "sonar": (1.00, 1.00),
}
DEFAULT_PRICE_PER_1M = (3.00, 15.00)


def _cost_cents(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = DEFAULT_PRICE_PER_1M
    for prefix in sorted(PRICES_PER_1M, key=len, reverse=True):
        if model.startswith(prefix):
            price_in, price_out = PRICES_PER_1M[prefix]
            break
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000 * 100


def _parse_answer(data: dict) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
    return ""


def _parse_citations(data: dict) -> list[str]:
    """Merge top-level `citations` urls with `search_results[].url`, deduped in order."""
    urls: list[str] = []
    citations = data.get("citations")
    for url in citations if isinstance(citations, list) else []:
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    results = data.get("search_results")
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        url = result.get("url")
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return urls


class PerplexityEngine(EngineAdapter):
    name = "perplexity"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key or os.environ.get("PERPLEXITY_API_KEY", "")
        if not self.api_key:
            raise EngineError("PERPLEXITY_API_KEY is not set", retryable=False)
        self.model = model or os.environ.get("PERPLEXITY_MODEL", "sonar")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    def sample(self, prompt: str) -> EngineSample:
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = post_json(self._client, API_URL, headers=headers, payload=payload, engine=self.name)
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(usage_raw.get("prompt_tokens") or 0)
        completion_tokens = int(usage_raw.get("completion_tokens") or 0)
        model_version = str(data.get("model") or self.model)
        return EngineSample(
            engine=self.name,
            model_version=model_version,
            answer_text=_parse_answer(data),
            cited_urls=_parse_citations(data),
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            cost_cents=_cost_cents(model_version, prompt_tokens, completion_tokens),
            raw=data,
        )
