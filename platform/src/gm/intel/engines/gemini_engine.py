"""Gemini generateContent adapter (google_search grounding tool)."""

from __future__ import annotations

import os

import httpx

from gm.intel.engines import DEFAULT_TIMEOUT_SECONDS, post_json
from gm.intel.engines.base import EngineAdapter, EngineError, EngineSample

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Approximate USD per 1M tokens (input, output). Conservative snapshots by
# longest-prefix match; unknown models fall back to gemini-2.5-pro pricing
# (the priciest tier) so estimates err high. Grounding request fees beyond
# the free daily allotment are not modeled here.
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
}
DEFAULT_PRICE_PER_1M = (1.25, 10.00)


def _cost_cents(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = DEFAULT_PRICE_PER_1M
    for prefix in sorted(PRICES_PER_1M, key=len, reverse=True):
        if model.startswith(prefix):
            price_in, price_out = PRICES_PER_1M[prefix]
            break
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000 * 100


def _first_candidate(data: dict) -> dict:
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def _parse_answer(candidate: dict) -> str:
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    texts: list[str] = []
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "".join(texts)


def _parse_citations(candidate: dict) -> list[str]:
    """groundingMetadata.groundingChunks[].web.uri, deduped in order; never raises."""
    urls: list[str] = []
    metadata = candidate.get("groundingMetadata")
    chunks = metadata.get("groundingChunks") if isinstance(metadata, dict) else None
    for chunk in chunks if isinstance(chunks, list) else []:
        if not isinstance(chunk, dict):
            continue
        web = chunk.get("web")
        uri = web.get("uri") if isinstance(web, dict) else None
        if isinstance(uri, str) and uri and uri not in urls:
            urls.append(uri)
    return urls


class GeminiEngine(EngineAdapter):
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise EngineError("GEMINI_API_KEY is not set", retryable=False)
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    def sample(self, prompt: str) -> EngineSample:
        url = f"{API_BASE}/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }
        headers = {"x-goog-api-key": self.api_key}
        data = post_json(self._client, url, headers=headers, payload=payload, engine=self.name)
        candidate = _first_candidate(data)
        usage_raw = (
            data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        )
        prompt_tokens = int(usage_raw.get("promptTokenCount") or 0)
        # Thinking tokens are billed as output; fold them in so cost errs high.
        completion_tokens = int(usage_raw.get("candidatesTokenCount") or 0) + int(
            usage_raw.get("thoughtsTokenCount") or 0
        )
        model_version = str(data.get("modelVersion") or self.model)
        return EngineSample(
            engine=self.name,
            model_version=model_version,
            answer_text=_parse_answer(candidate),
            cited_urls=_parse_citations(candidate),
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            cost_cents=_cost_cents(model_version, prompt_tokens, completion_tokens),
            raw=data,
        )
