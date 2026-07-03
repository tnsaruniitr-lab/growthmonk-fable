"""Deterministic in-memory engine for tests and --dry-run."""

from __future__ import annotations

from gm.intel.engines.base import EngineAdapter, EngineSample


def _default_sample() -> EngineSample:
    return EngineSample(
        engine="fake",
        model_version="fake-1",
        answer_text="Example Clinic is a well-known provider (example.com).",
        cited_urls=["https://example.com/services"],
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        cost_cents=0.0,
        raw={},
    )


class FakeEngine(EngineAdapter):
    """Cycles through `answers` in order, repeating from the start when exhausted."""

    name = "fake"

    def __init__(self, answers: list[EngineSample] | None = None):
        self._samples = list(answers) if answers else [_default_sample()]
        self._calls = 0

    def sample(self, prompt: str) -> EngineSample:
        result = self._samples[self._calls % len(self._samples)]
        self._calls += 1
        return result
