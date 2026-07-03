"""Engine adapter contract for citation sampling.

Adapters are read-only: one prompt in, one answer + citations out. They must:
- raise EngineError (with retryable flag) on failure — never return partial samples
- retry internally on 429/5xx (max 3, exponential backoff), honor a 60s total timeout
- report token usage and an estimated cost_cents on every successful sample
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urlparse


class EngineError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class EngineSample:
    engine: str
    model_version: str
    answer_text: str
    cited_urls: list[str]
    usage: dict = field(default_factory=dict)
    cost_cents: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class DetectResult:
    cited: bool
    cited_url: str | None
    mentioned: bool


class EngineAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def sample(self, prompt: str) -> EngineSample:  # pragma: no cover - interface
        ...


def normalize_host(url: str) -> str:
    host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    return host.removeprefix("www.")


def detect(sample: EngineSample, domain_norm: str, brand_terms: list[str] | None = None) -> DetectResult:
    """Citation = domain appears in cited_urls. Mention = domain or brand term in answer text."""
    target = normalize_host(domain_norm)
    cited_url = None
    for url in sample.cited_urls:
        host = normalize_host(url)
        if host == target or host.endswith("." + target):
            cited_url = url
            break
    text = sample.answer_text.lower()
    mentioned = target in text or any(t.lower() in text for t in (brand_terms or []) if t.strip())
    return DetectResult(cited=cited_url is not None, cited_url=cited_url, mentioned=mentioned)
