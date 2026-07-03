"""Engine adapter tests — no network; all HTTP goes through httpx.MockTransport.

No DB access anywhere in this module, so nothing here skips on a missing
DATABASE_URL (that discipline applies to tests/test_jobs.py).
"""

from __future__ import annotations

import json

import httpx
import pytest

import gm.intel.engines as engines_pkg
from gm.intel.engines import available, registry
from gm.intel.engines import gemini_engine as gemini_mod
from gm.intel.engines import openai_engine as openai_mod
from gm.intel.engines import perplexity_engine as perplexity_mod
from gm.intel.engines.base import EngineError, EngineSample, detect
from gm.intel.engines.fake_engine import FakeEngine
from gm.intel.engines.gemini_engine import GeminiEngine
from gm.intel.engines.openai_engine import OpenAIEngine
from gm.intel.engines.perplexity_engine import PerplexityEngine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(engines_pkg, "_sleep", lambda _s: None)


def make_client(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[dict]]:
    """MockTransport client replaying `responses` in order (last one repeats).

    Returns (client, requests) where requests collects each request's JSON body.
    """
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


# --- recorded-shape fixtures -------------------------------------------------

OPENAI_RESPONSE = {
    "id": "resp_abc123",
    "model": "gpt-4o-2024-08-06",
    "output": [
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Glow Clinic is a leading med-spa in Dubai.",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://glowclinic.ae/pricing",
                            "title": "Pricing — Glow Clinic",
                            "start_index": 0,
                            "end_index": 10,
                        },
                        {"type": "file_citation", "file_id": "file_1"},
                        {"type": "url_citation"},  # missing url — must be tolerated
                    ],
                }
            ],
        },
    ],
    "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
}

PERPLEXITY_RESPONSE = {
    "id": "ppx_1",
    "model": "sonar",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Top clinics include Glow Clinic."},
        }
    ],
    "citations": ["https://glowclinic.ae", "https://example.com/best-clinics"],
    "search_results": [
        {"title": "Glow Clinic", "url": "https://glowclinic.ae"},  # dupe of citations[0]
        {"title": "Directory", "url": "https://dubaidirectory.ae/spas"},
        {"title": "no url field"},
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 300, "total_tokens": 312},
}

GEMINI_RESPONSE = {
    "modelVersion": "gemini-2.5-flash",
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [{"text": "Glow Clinic "}, {"text": "is popular in Dubai."}],
            },
            "finishReason": "STOP",
            "groundingMetadata": {
                "webSearchQueries": ["best med spa dubai"],
                "groundingChunks": [
                    {
                        "web": {
                            "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x",
                            "title": "glowclinic.ae",
                        }
                    },
                    {"retrievedContext": {"uri": "gs://bucket/doc"}},  # no web key
                    {"web": {}},  # missing uri — must be tolerated
                ],
            },
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 10000,
        "candidatesTokenCount": 1000,
        "totalTokenCount": 11000,
    },
}


# --- OpenAI ------------------------------------------------------------------


class TestOpenAI:
    def test_parses_answer_citations_usage_cost(self):
        client, requests = make_client([(200, OPENAI_RESPONSE)])
        sample = OpenAIEngine(api_key="k", model="gpt-4o", client=client).sample("best med spa?")
        assert sample.engine == "openai"
        assert sample.model_version == "gpt-4o-2024-08-06"
        assert sample.answer_text == "Glow Clinic is a leading med-spa in Dubai."
        assert sample.cited_urls == ["https://glowclinic.ae/pricing"]
        assert sample.usage == {"prompt_tokens": 1000, "completion_tokens": 500}
        # gpt-4o: (1000*2.50 + 500*10.00) / 1e6 * 100 cents
        assert sample.cost_cents == pytest.approx(0.75)
        assert requests[0]["tools"] == [{"type": "web_search"}]

    def test_web_search_tool_fallback_to_preview(self):
        rejection = {
            "error": {"message": "Invalid value: 'web_search' is not a supported tool type."}
        }
        client, requests = make_client([(400, rejection), (200, OPENAI_RESPONSE)])
        sample = OpenAIEngine(api_key="k", client=client).sample("q")
        assert sample.cited_urls == ["https://glowclinic.ae/pricing"]
        assert [r["tools"][0]["type"] for r in requests] == ["web_search", "web_search_preview"]

    def test_unrelated_400_is_not_retried_as_preview(self):
        client, requests = make_client([(400, {"error": {"message": "bad model"}})])
        with pytest.raises(EngineError) as exc_info:
            OpenAIEngine(api_key="k", client=client).sample("q")
        assert exc_info.value.retryable is False
        assert len(requests) == 1

    def test_missing_output_yields_empty_answer_and_citations(self):
        client, _ = make_client([(200, {"model": "gpt-4o", "output": None})])
        sample = OpenAIEngine(api_key="k", client=client).sample("q")
        assert sample.answer_text == ""
        assert sample.cited_urls == []
        assert sample.usage == {"prompt_tokens": 0, "completion_tokens": 0}

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(EngineError):
            OpenAIEngine()

    def test_cost_table_prefix_match(self):
        assert openai_mod._cost_cents("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(15.0)
        # unknown model falls back to gpt-4o pricing
        assert openai_mod._cost_cents("gpt-99-turbo", 0, 1_000_000) == pytest.approx(1000.0)


# --- Perplexity ---------------------------------------------------------------


class TestPerplexity:
    def test_parses_answer_citations_usage_cost(self):
        client, requests = make_client([(200, PERPLEXITY_RESPONSE)])
        sample = PerplexityEngine(api_key="k", model="sonar", client=client).sample("q")
        assert sample.engine == "perplexity"
        assert sample.answer_text == "Top clinics include Glow Clinic."
        # citations first, then search_results urls, deduped in order
        assert sample.cited_urls == [
            "https://glowclinic.ae",
            "https://example.com/best-clinics",
            "https://dubaidirectory.ae/spas",
        ]
        assert sample.usage == {"prompt_tokens": 12, "completion_tokens": 300}
        # sonar: (12*1.00 + 300*1.00) / 1e6 * 100 cents
        assert sample.cost_cents == pytest.approx(0.0312)
        assert requests[0]["messages"] == [{"role": "user", "content": "q"}]

    def test_missing_fields_tolerated(self):
        client, _ = make_client([(200, {"model": "sonar"})])
        sample = PerplexityEngine(api_key="k", client=client).sample("q")
        assert sample.answer_text == ""
        assert sample.cited_urls == []

    def test_retry_then_success(self):
        client, requests = make_client(
            [(429, {"error": "rate limit"}), (500, "oops"), (200, PERPLEXITY_RESPONSE)]
        )
        sample = PerplexityEngine(api_key="k", client=client).sample("q")
        assert sample.answer_text == "Top clinics include Glow Clinic."
        assert len(requests) == 3

    def test_retry_exhaustion_raises_retryable(self):
        client, requests = make_client([(500, "boom")])
        with pytest.raises(EngineError) as exc_info:
            PerplexityEngine(api_key="k", client=client).sample("q")
        assert exc_info.value.retryable is True
        assert len(requests) == 4  # initial attempt + 3 retries

    def test_auth_failure_is_not_retried(self):
        client, requests = make_client([(401, {"error": "bad key"})])
        with pytest.raises(EngineError) as exc_info:
            PerplexityEngine(api_key="k", client=client).sample("q")
        assert exc_info.value.retryable is False
        assert len(requests) == 1

    def test_cost_prefix_prefers_longest_match(self):
        assert perplexity_mod._cost_cents("sonar-pro", 0, 1_000_000) == pytest.approx(1500.0)
        assert perplexity_mod._cost_cents("sonar", 1_000_000, 0) == pytest.approx(100.0)


# --- Gemini --------------------------------------------------------------------


class TestGemini:
    def test_parses_answer_citations_usage_cost(self):
        client, requests = make_client([(200, GEMINI_RESPONSE)])
        engine = GeminiEngine(api_key="k", model="gemini-2.5-flash", client=client)
        sample = engine.sample("best med spa?")
        assert sample.engine == "gemini"
        assert sample.model_version == "gemini-2.5-flash"
        assert sample.answer_text == "Glow Clinic is popular in Dubai."
        assert sample.cited_urls == [
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x"
        ]
        assert sample.usage == {"prompt_tokens": 10000, "completion_tokens": 1000}
        # gemini-2.5-flash: (10000*0.30 + 1000*2.50) / 1e6 * 100 cents
        assert sample.cost_cents == pytest.approx(0.55)
        assert requests[0]["tools"] == [{"google_search": {}}]
        assert requests[0]["contents"] == [{"parts": [{"text": "best med spa?"}]}]

    def test_empty_candidates_tolerated(self):
        client, _ = make_client([(200, {"candidates": [], "usageMetadata": {}})])
        sample = GeminiEngine(api_key="k", client=client).sample("q")
        assert sample.answer_text == ""
        assert sample.cited_urls == []
        assert sample.cost_cents == 0.0

    def test_thought_tokens_count_as_completion(self):
        body = {
            "modelVersion": "gemini-2.5-flash",
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 20,
            },
        }
        client, _ = make_client([(200, body)])
        sample = GeminiEngine(api_key="k", client=client).sample("q")
        assert sample.usage == {"prompt_tokens": 10, "completion_tokens": 25}

    def test_cost_table_spot_check(self):
        assert gemini_mod._cost_cents("gemini-2.5-pro", 1_000_000, 0) == pytest.approx(125.0)
        assert gemini_mod._cost_cents("gemini-2.5-flash-lite", 0, 1_000_000) == pytest.approx(40.0)


# --- FakeEngine ------------------------------------------------------------------


class TestFakeEngine:
    def test_cycles_through_samples_deterministically(self):
        a = EngineSample(engine="fake", model_version="v", answer_text="a", cited_urls=[])
        b = EngineSample(engine="fake", model_version="v", answer_text="b", cited_urls=[])
        fake = FakeEngine([a, b])
        assert [fake.sample("q").answer_text for _ in range(5)] == ["a", "b", "a", "b", "a"]

    def test_default_sample_when_none_provided(self):
        sample = FakeEngine().sample("q")
        assert sample.engine == "fake"
        assert sample.cited_urls


# --- detect() (lives in base.py — exercised, not reimplemented) -------------------


def _sample(urls: list[str], text: str = "") -> EngineSample:
    return EngineSample(engine="x", model_version="v", answer_text=text, cited_urls=urls)


class TestDetect:
    def test_exact_host_match(self):
        result = detect(_sample(["https://glowclinic.ae/pricing"]), "glowclinic.ae")
        assert result.cited is True
        assert result.cited_url == "https://glowclinic.ae/pricing"

    def test_subdomain_counts_as_citation(self):
        result = detect(_sample(["https://blog.glowclinic.ae/post"]), "glowclinic.ae")
        assert result.cited is True

    def test_www_is_stripped_on_both_sides(self):
        result = detect(_sample(["https://www.glowclinic.ae/"]), "www.glowclinic.ae")
        assert result.cited is True

    def test_brand_term_mention_without_citation(self):
        result = detect(
            _sample([], text="Glow Clinic is a top choice."),
            "glowclinic.ae",
            brand_terms=["Glow Clinic"],
        )
        assert result.cited is False
        assert result.cited_url is None
        assert result.mentioned is True

    def test_no_match(self):
        result = detect(
            _sample(["https://other.com/x"], text="Nothing relevant here."),
            "glowclinic.ae",
            brand_terms=["Glow Clinic"],
        )
        assert result.cited is False
        assert result.mentioned is False

    def test_similar_suffix_domain_is_not_a_citation(self):
        result = detect(_sample(["https://notglowclinic.ae/"]), "glowclinic.ae")
        assert result.cited is False


# --- registry / available ----------------------------------------------------------


class TestRegistry:
    def test_only_engines_with_keys_are_available(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert available() == ["openai"]
        reg = registry()
        assert set(reg) == {"openai"}
        assert isinstance(reg["openai"], OpenAIEngine)

    def test_all_engines_when_all_keys_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k1")
        monkeypatch.setenv("PERPLEXITY_API_KEY", "k2")
        monkeypatch.setenv("GEMINI_API_KEY", "k3")
        reg = registry()
        assert set(reg) == {"openai", "perplexity", "gemini"}
        assert isinstance(reg["perplexity"], PerplexityEngine)
        assert isinstance(reg["gemini"], GeminiEngine)

    def test_missing_key_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with caplog.at_level("WARNING", logger="gm.intel.engines"):
            assert available() == []
        assert "OPENAI_API_KEY" in caplog.text
