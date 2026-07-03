"""LLM gateway tests — zero network; all HTTP goes through httpx.MockTransport.

No DB access anywhere in this module, so nothing here skips on a missing
DATABASE_URL (that discipline applies to the DB-backed test modules).
"""

from __future__ import annotations

import json

import httpx
import pytest

import gm.infra.llm as llm_mod
from gm.infra.llm import (
    API_VERSION,
    CallBudget,
    CostCapExceeded,
    FakeLlm,
    LlmClient,
    LlmError,
    cost_cents,
    parse_json,
    strip_fences,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_mod, "_sleep", lambda _s: None)


def anthropic_body(
    text: str, *, model: str = "claude-sonnet-5", input_tokens: int = 1000, output_tokens: int = 500
) -> dict:
    return {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def make_client(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[httpx.Request]]:
    """MockTransport client replaying `responses` in order (last one repeats).

    Returns (client, requests) collecting each raw request for header/body asserts.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def make_llm(
    responses: list[tuple[int, object]], **kwargs
) -> tuple[LlmClient, list[httpx.Request]]:
    client, requests = make_client(responses)
    kwargs.setdefault("api_key", "sk-ant-test")
    return LlmClient(client=client, **kwargs), requests


# --- success path -------------------------------------------------------------


def test_success_parses_json_and_reports_usage():
    body = anthropic_body('{"check_id": "a1", "status": "pass"}')
    llm, requests = make_llm([(200, body)])
    result = llm.complete(system="classify", user="evidence")

    assert result.parsed == {"check_id": "a1", "status": "pass"}
    assert result.parse_error is None
    assert result.usage == {"input_tokens": 1000, "output_tokens": 500}
    assert result.model == "claude-sonnet-5"
    # 1000 in @ $3/1M + 500 out @ $15/1M = $0.0105 = 1.05 cents
    assert result.cost_cents == pytest.approx(1.05)
    assert len(requests) == 1


def test_request_headers_and_payload_shape():
    llm, requests = make_llm([(200, anthropic_body("{}"))], model="claude-sonnet-5")
    llm.complete(system="sys prompt", user="user prompt", max_tokens=1234)

    req = requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert req.headers["anthropic-version"] == API_VERSION
    payload = json.loads(req.content)
    assert payload["model"] == "claude-sonnet-5"
    assert payload["max_tokens"] == 1234
    assert payload["messages"] == [{"role": "user", "content": "user prompt"}]
    # json_only=True appends the JSON-only instruction to the system prompt
    assert payload["system"].startswith("sys prompt")
    assert "ONLY with valid JSON" in payload["system"]


def test_json_only_false_skips_instruction_and_parsing():
    llm, requests = make_llm([(200, anthropic_body("plain prose"))])
    result = llm.complete(system="sys", user="u", json_only=False)

    assert result.text == "plain prose"
    assert result.parsed is None
    assert result.parse_error is None
    payload = json.loads(requests[0].content)
    assert payload["system"] == "sys"


def test_model_default_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    llm, _ = make_llm([(200, anthropic_body("{}"))])
    assert llm.model == "claude-sonnet-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    llm2, _ = make_llm([(200, anthropic_body("{}"))])
    assert llm2.model == "claude-haiku-4-5"


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LlmError) as exc_info:
        LlmClient()
    assert exc_info.value.retryable is False


# --- defensive JSON parsing ---------------------------------------------------


def test_fence_stripping_variants():
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('```\n[1, 2]\n```') == "[1, 2]"
    assert strip_fences('  {"a": 1}  ') == '{"a": 1}'


def test_complete_strips_markdown_fences():
    body = anthropic_body('```json\n{"status": "pass"}\n```')
    llm, _ = make_llm([(200, body)])
    result = llm.complete(system="s", user="u")
    assert result.parsed == {"status": "pass"}
    assert result.parse_error is None


def test_parse_failure_sets_error_never_raises():
    llm, _ = make_llm([(200, anthropic_body("sorry, I cannot produce JSON here"))])
    result = llm.complete(system="s", user="u")
    assert result.parsed is None
    assert result.parse_error is not None
    assert "invalid JSON" in result.parse_error


def test_parse_scalar_is_a_parse_failure():
    parsed, error = parse_json("42")
    assert parsed is None
    assert "expected JSON object or array" in error
    parsed, error = parse_json('["ok", {"a": 1}]')
    assert parsed == ["ok", {"a": 1}]
    assert error is None


# --- retries ------------------------------------------------------------------


def test_retry_then_success_on_429():
    good = anthropic_body('{"ok": true}')
    llm, requests = make_llm([(429, {"error": "rate limited"}), (200, good)])
    result = llm.complete(system="s", user="u")
    assert result.parsed == {"ok": True}
    assert len(requests) == 2


def test_retry_then_success_on_529_and_500():
    good = anthropic_body('{"ok": true}')
    llm, requests = make_llm([(529, "overloaded"), (500, "boom"), (200, good)])
    result = llm.complete(system="s", user="u")
    assert result.parsed == {"ok": True}
    assert len(requests) == 3


def test_retry_exhaustion_raises_retryable():
    llm, requests = make_llm([(529, "overloaded")])
    with pytest.raises(LlmError) as exc_info:
        llm.complete(system="s", user="u")
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == 529
    assert len(requests) == 1 + llm_mod.MAX_RETRIES


def test_transport_error_retries_then_raises_retryable():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = LlmClient(api_key="sk-ant-test", client=client)
    with pytest.raises(LlmError) as exc_info:
        llm.complete(system="s", user="u")
    assert exc_info.value.retryable is True
    assert calls["n"] == 1 + llm_mod.MAX_RETRIES


@pytest.mark.parametrize("status", [400, 401])
def test_non_retryable_client_errors(status: int):
    llm, requests = make_llm([(status, {"error": {"message": "bad request"}})])
    with pytest.raises(LlmError) as exc_info:
        llm.complete(system="s", user="u")
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == status
    assert len(requests) == 1  # no retries on 4xx


def test_non_json_success_body_raises_non_retryable():
    llm, _ = make_llm([(200, "<html>not json</html>")])
    with pytest.raises(LlmError) as exc_info:
        llm.complete(system="s", user="u")
    assert exc_info.value.retryable is False


# --- cost table ---------------------------------------------------------------


def test_cost_math_spot_check():
    # claude-sonnet-5: $3/1M in, $15/1M out
    assert cost_cents("claude-sonnet-5", 1000, 500) == pytest.approx(1.05)
    # claude-haiku-4-5: $1/1M in, $5/1M out -> (1M*1 + 0*5)/1M * 100 = 100 cents
    assert cost_cents("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(100.0)
    # opus prefix match
    assert cost_cents("claude-opus-4-8", 0, 1_000_000) == pytest.approx(2500.0)


def test_unknown_model_uses_conservative_fallback():
    # fallback is the priciest tier ($10/$50)
    assert cost_cents("some-future-model", 1_000_000, 0) == pytest.approx(1000.0)
    assert cost_cents("some-future-model", 0, 1_000_000) == pytest.approx(5000.0)


# --- CallBudget ---------------------------------------------------------------


def test_budget_charge_accumulates_and_caps():
    budget = CallBudget(cap_cents=10.0)
    budget.charge(4.0)
    budget.charge(6.0)  # exactly at cap is allowed
    assert budget.spent_cents == pytest.approx(10.0)
    with pytest.raises(CostCapExceeded):
        budget.charge(0.01)
    assert budget.spent_cents == pytest.approx(10.0)  # failed charge not recorded


def test_budget_precheck_raises_before_any_call():
    # cap far below the worst case for max_tokens at the output rate:
    # 4096 tokens @ $15/1M = ~6.1 cents estimated
    budget = CallBudget(cap_cents=1.0)
    llm, requests = make_llm([(200, anthropic_body("{}"))], model="claude-sonnet-5")
    with pytest.raises(CostCapExceeded):
        llm.complete(system="s", user="u", budget=budget)
    assert requests == []  # raised BEFORE the HTTP call
    assert budget.spent_cents == 0.0


def test_budget_charged_with_actuals_after_call():
    budget = CallBudget(cap_cents=100.0)
    body = anthropic_body('{"ok": 1}', input_tokens=1000, output_tokens=500)
    llm, requests = make_llm([(200, body)], model="claude-sonnet-5")
    result = llm.complete(system="s", user="u", max_tokens=2000, budget=budget)
    assert len(requests) == 1
    # charged the actual usage-based cost, not the pre-call estimate
    assert budget.spent_cents == pytest.approx(result.cost_cents)
    assert budget.spent_cents == pytest.approx(1.05)


def test_budget_estimate_uses_model_output_rate():
    # haiku output rate $5/1M -> 4096 tokens ~ 2.05 cents; a 3-cent cap passes precheck
    budget = CallBudget(cap_cents=3.0)
    body = anthropic_body('{"ok": 1}', model="claude-haiku-4-5", input_tokens=10, output_tokens=10)
    llm, requests = make_llm([(200, body)], model="claude-haiku-4-5")
    llm.complete(system="s", user="u", budget=budget)
    assert len(requests) == 1


# --- FakeLlm ------------------------------------------------------------------


def test_fake_llm_cycles_list_responses():
    fake = FakeLlm(['{"n": 1}', '{"n": 2}'])
    assert fake.complete(system="s", user="a").parsed == {"n": 1}
    assert fake.complete(system="s", user="b").parsed == {"n": 2}
    assert fake.complete(system="s", user="c").parsed == {"n": 1}  # cycles


def test_fake_llm_callable_responses():
    fake = FakeLlm(lambda system, user: json.dumps({"echo": user}))
    result = fake.complete(system="s", user="hello")
    assert result.parsed == {"echo": "hello"}


def test_fake_llm_zero_cost_and_usage():
    fake = FakeLlm(["not json at all"])
    result = fake.complete(system="s", user="u")
    assert result.cost_cents == 0.0
    assert result.usage == {"input_tokens": 0, "output_tokens": 0}
    assert result.parsed is None
    assert result.parse_error is not None


def test_fake_llm_records_calls_and_strips_fences():
    fake = FakeLlm(['```json\n{"a": 1}\n```'])
    result = fake.complete(system="the system", user="the user", max_tokens=99)
    assert result.parsed == {"a": 1}
    assert fake.calls == [{"system": "the system", "user": "the user", "max_tokens": 99}]


def test_fake_llm_json_only_false():
    fake = FakeLlm(["prose"])
    result = fake.complete(system="s", user="u", json_only=False)
    assert result.text == "prose"
    assert result.parsed is None
    assert result.parse_error is None
