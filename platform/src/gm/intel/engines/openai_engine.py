"""OpenAI Responses API adapter (web_search tool)."""

from __future__ import annotations

import os

import httpx

from gm.intel.engines import DEFAULT_TIMEOUT_SECONDS, post_json
from gm.intel.engines.base import EngineAdapter, EngineError, EngineSample

API_URL = "https://api.openai.com/v1/responses"

# Approximate USD per 1M tokens (input, output). Conservative list-price
# snapshots, matched by longest model-name prefix; unknown models fall back
# to gpt-4o pricing so cost is over- rather than under-estimated.
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}
DEFAULT_PRICE_PER_1M = (2.50, 10.00)


def _cost_cents(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = DEFAULT_PRICE_PER_1M
    for prefix in sorted(PRICES_PER_1M, key=len, reverse=True):
        if model.startswith(prefix):
            price_in, price_out = PRICES_PER_1M[prefix]
            break
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000 * 100


def _parse_output(output: object) -> tuple[str, list[str]]:
    """Answer text + url_citation urls from a Responses `output` list; never raises."""
    texts: list[str] = []
    urls: list[str] = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            texts.append(str(part.get("text") or ""))
            annotations = part.get("annotations")
            for ann in annotations if isinstance(annotations, list) else []:
                if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                    continue
                url = ann.get("url")
                if isinstance(url, str) and url and url not in urls:
                    urls.append(url)
    return "".join(texts), urls


class OpenAIEngine(EngineAdapter):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise EngineError("OPENAI_API_KEY is not set", retryable=False)
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    def sample(self, prompt: str) -> EngineSample:
        data = self._request(prompt)
        answer, urls = _parse_output(data.get("output"))
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(usage_raw.get("input_tokens") or 0)
        completion_tokens = int(usage_raw.get("output_tokens") or 0)
        model_version = str(data.get("model") or self.model)
        return EngineSample(
            engine=self.name,
            model_version=model_version,
            answer_text=answer,
            cited_urls=urls,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            cost_cents=_cost_cents(model_version, prompt_tokens, completion_tokens),
            raw=data,
        )

    def _request(self, prompt: str) -> dict:
        try:
            return self._post(prompt, "web_search")
        except EngineError as err:
            # Some models only accept the older preview tool name; one fallback try.
            if getattr(err, "status_code", None) == 400 and "web_search" in str(err):
                return self._post(prompt, "web_search_preview")
            raise

    def _post(self, prompt: str, tool_type: str) -> dict:
        payload = {"model": self.model, "input": prompt, "tools": [{"type": tool_type}]}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        return post_json(self._client, API_URL, headers=headers, payload=payload, engine=self.name)
