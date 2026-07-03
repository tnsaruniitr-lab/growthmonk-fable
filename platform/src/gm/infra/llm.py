"""LLM gateway v0 — Anthropic Messages API only.

Single retry/timeout path mirroring the engines package (`gm.intel.engines.post_json`),
copied rather than imported so the audit pipeline has no dependency on the intel side:
120s total budget, up to 3 retries on 429/529/5xx/transport with backoff + jitter,
LlmError(retryable=...) on everything else.

Cost control: `CallBudget` is shared across one audit job. Before each call the
projected worst case (max_tokens at the model's output rate) is checked against the
cap and CostCapExceeded is raised BEFORE spending anything; after a successful call
the actual usage-based cost is charged.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096
TOTAL_BUDGET_SECONDS = 120.0
MAX_RETRIES = 3

# Approximate USD per 1M tokens (input, output), longest-prefix match. Unknown
# models fall back to the priciest tier so estimates err high (conservative).
PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku": (1.00, 5.00),
}
DEFAULT_PRICE_PER_1M = (10.00, 50.00)

JSON_ONLY_INSTRUCTION = (
    "\n\nRespond ONLY with valid JSON (an object or array). No prose, no markdown fences,"
    " no commentary before or after the JSON."
)

# Matches an optionally-labelled markdown code fence wrapping the whole payload.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```", re.DOTALL)

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class LlmError(Exception):
    """LLM gateway failure. `retryable` mirrors the engines-package convention."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class CostCapExceeded(LlmError):
    """The shared CallBudget would be (or was) blown by a charge."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


@dataclass
class LlmResult:
    text: str
    parsed: dict | list | None
    parse_error: str | None
    usage: dict
    cost_cents: float
    model: str


class CallBudget:
    """Cost cap shared across the LLM calls of one audit job (cents)."""

    def __init__(self, cap_cents: float):
        self.cap_cents = float(cap_cents)
        self.spent_cents = 0.0

    def precheck(self, estimated_cents: float) -> None:
        """Raise CostCapExceeded if a charge of `estimated_cents` would exceed the cap."""
        if self.spent_cents + estimated_cents > self.cap_cents:
            raise CostCapExceeded(
                f"cost cap {self.cap_cents:.2f}c would be exceeded:"
                f" spent {self.spent_cents:.4f}c + estimated {estimated_cents:.4f}c"
            )

    def charge(self, cents: float) -> None:
        """Record an actual spend; raises CostCapExceeded when the total would exceed the cap."""
        if self.spent_cents + cents > self.cap_cents:
            raise CostCapExceeded(
                f"cost cap {self.cap_cents:.2f}c exceeded:"
                f" spent {self.spent_cents:.4f}c + charge {cents:.4f}c"
            )
        self.spent_cents += cents


def price_per_1m(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for `model`, longest-prefix match."""
    for prefix in sorted(PRICES_PER_1M, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES_PER_1M[prefix]
    return DEFAULT_PRICE_PER_1M


def cost_cents(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = price_per_1m(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000 * 100


def strip_fences(text: str) -> str:
    """Return the payload inside a markdown code fence, or `text` stripped."""
    stripped = text.strip()
    match = _FENCE_RE.search(stripped)
    if match and stripped.startswith("```"):
        return match.group(1).strip()
    return stripped


def parse_json(text: str) -> tuple[dict | list | None, str | None]:
    """Defensive JSON parse: (parsed, None) on success, (None, error) on failure.

    Never raises. Strips markdown fences first; only objects and arrays count as
    parsed (a bare scalar is a parse failure for our purposes).
    """
    candidate = strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(parsed, dict | list):
        return None, f"expected JSON object or array, got {type(parsed).__name__}"
    return parsed, None


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def _post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    max_retries: int = MAX_RETRIES,
    total_budget_seconds: float = TOTAL_BUDGET_SECONDS,
) -> dict:
    """POST `payload` as JSON and return the parsed JSON body.

    Retries 429/529/5xx and transport failures (max_retries times, backoff + jitter)
    within a single total time budget. Any other failure raises a non-retryable
    LlmError; exhaustion/timeouts raise a retryable one. The HTTP status, when known,
    is attached to the error as `.status_code` (body text as `.body_text`).
    """
    deadline = time.monotonic() + total_budget_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LlmError(
                f"llm: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
            )
        try:
            resp = client.post(
                url, headers=headers, json=payload, timeout=min(remaining, total_budget_seconds)
            )
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                raise LlmError(
                    f"llm: transport failure after {attempt + 1} attempts: {exc}",
                    retryable=True,
                ) from exc
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code in (429, 529) or resp.status_code >= 500:
            if attempt >= max_retries:
                err = LlmError(
                    f"llm: HTTP {resp.status_code} after {attempt + 1} attempts", retryable=True
                )
                err.status_code = resp.status_code
                raise err
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code >= 400:
            err = LlmError(f"llm: HTTP {resp.status_code}: {resp.text[:500]}", retryable=False)
            err.status_code = resp.status_code
            err.body_text = resp.text
            raise err
        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError("llm: non-JSON response body", retryable=False) from exc
        if not isinstance(data, dict):
            raise LlmError(
                f"llm: unexpected JSON payload type {type(data).__name__}", retryable=False
            )
        return data


class LlmClient:
    """Anthropic Messages API client (httpx, injectable transport, no SDK)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LlmError("ANTHROPIC_API_KEY is not set", retryable=False)
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = client or httpx.Client(timeout=TOTAL_BUDGET_SECONDS)

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_only: bool = True,
        budget: CallBudget | None = None,
    ) -> LlmResult:
        if budget is not None:
            # Worst case for this call: full max_tokens billed at the output rate.
            _, price_out = price_per_1m(self.model)
            estimated = max_tokens * price_out / 1_000_000 * 100
            budget.precheck(estimated)

        effective_system = system + JSON_ONLY_INSTRUCTION if json_only else system
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": effective_system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        data = _post_json(self._client, API_URL, headers=headers, payload=payload)

        text = "".join(
            str(block.get("text") or "")
            for block in (data.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = {
            "input_tokens": int(usage_raw.get("input_tokens") or 0),
            "output_tokens": int(usage_raw.get("output_tokens") or 0),
        }
        model = str(data.get("model") or self.model)
        actual_cents = cost_cents(model, usage["input_tokens"], usage["output_tokens"])
        if budget is not None:
            budget.charge(actual_cents)

        parsed, parse_error = parse_json(text) if json_only else (None, None)
        return LlmResult(
            text=text,
            parsed=parsed,
            parse_error=parse_error,
            usage=usage,
            cost_cents=actual_cents,
            model=model,
        )


@dataclass
class FakeLlm:
    """Deterministic stand-in for tests and pipeline dry runs.

    `responses` is either a list of canned reply strings (cycled call by call) or
    a callable (system, user) -> str. cost_cents is always 0; usage is zeros.
    """

    responses: list[str] | Callable[[str, str], str]
    model: str = "fake-llm"
    calls: list[dict] = field(default_factory=list)
    _index: int = 0

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_only: bool = True,
        budget: CallBudget | None = None,
    ) -> LlmResult:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if callable(self.responses):
            text = self.responses(system, user)
        else:
            if not self.responses:
                raise LlmError("FakeLlm has no responses configured", retryable=False)
            text = self.responses[self._index % len(self.responses)]
            self._index += 1
        parsed, parse_error = parse_json(text) if json_only else (None, None)
        return LlmResult(
            text=text,
            parsed=parsed,
            parse_error=parse_error,
            usage={"input_tokens": 0, "output_tokens": 0},
            cost_cents=0.0,
            model=self.model,
        )
