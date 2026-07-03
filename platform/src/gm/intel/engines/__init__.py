"""Engine adapter registry + shared HTTP plumbing for citation sampling.

`post_json` is the single retry/timeout path all real adapters go through:
60s total budget, up to 3 retries on 429/5xx/transport errors with exponential
backoff + jitter, EngineError(retryable=...) on everything else.
"""

from __future__ import annotations

import logging
import os
import random
import time

import httpx

from gm.intel.engines.base import (
    DetectResult,
    EngineAdapter,
    EngineError,
    EngineSample,
    detect,
    normalize_host,
)

__all__ = [
    "DetectResult",
    "EngineAdapter",
    "EngineError",
    "EngineSample",
    "available",
    "detect",
    "normalize_host",
    "post_json",
    "registry",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3

_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    engine: str,
    max_retries: int = MAX_RETRIES,
    total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """POST `payload` as JSON and return the parsed JSON body.

    Retries 429/5xx and transport failures (max_retries times, backoff + jitter)
    within a single total time budget. Any other failure raises a non-retryable
    EngineError; exhaustion/timeouts raise a retryable one. The HTTP status, when
    known, is attached to the error as `.status_code` (body text as `.body_text`).
    """
    deadline = time.monotonic() + total_budget_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EngineError(
                f"{engine}: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
            )
        try:
            resp = client.post(
                url, headers=headers, json=payload, timeout=min(remaining, total_budget_seconds)
            )
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                raise EngineError(
                    f"{engine}: transport failure after {attempt + 1} attempts: {exc}",
                    retryable=True,
                ) from exc
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                err = EngineError(
                    f"{engine}: HTTP {resp.status_code} after {attempt + 1} attempts",
                    retryable=True,
                )
                err.status_code = resp.status_code
                raise err
            attempt += 1
            _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
            continue
        if resp.status_code >= 400:
            err = EngineError(
                f"{engine}: HTTP {resp.status_code}: {resp.text[:500]}", retryable=False
            )
            err.status_code = resp.status_code
            err.body_text = resp.text
            raise err
        try:
            data = resp.json()
        except ValueError as exc:
            raise EngineError(f"{engine}: non-JSON response body", retryable=False) from exc
        if not isinstance(data, dict):
            raise EngineError(
                f"{engine}: unexpected JSON payload type {type(data).__name__}", retryable=False
            )
        return data


def available() -> list[str]:
    """Names of engines whose API keys are set; warns about each excluded one."""
    names: list[str] = []
    for name, env_key in _ENV_KEYS.items():
        if os.environ.get(env_key):
            names.append(name)
        else:
            logger.warning("engine %r unavailable: %s not set", name, env_key)
    return names


def registry() -> dict[str, EngineAdapter]:
    """name -> constructed adapter for every engine with an API key present."""
    from gm.intel.engines.gemini_engine import GeminiEngine
    from gm.intel.engines.openai_engine import OpenAIEngine
    from gm.intel.engines.perplexity_engine import PerplexityEngine

    ctors: dict[str, type[EngineAdapter]] = {
        "openai": OpenAIEngine,
        "perplexity": PerplexityEngine,
        "gemini": GeminiEngine,
    }
    return {name: ctors[name]() for name in available()}
