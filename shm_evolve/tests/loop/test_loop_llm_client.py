"""llm_client.py: payload rules (pinning, reasoning cap, thinking toggle) and
the retry ladder, against an in-process mock transport. No network."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from loop import config as config_mod
from loop.jsonx import extract_json
from loop.llm_client import AsyncLLMClient, is_transient_payment_required


@pytest.fixture
def cfg(monkeypatch):
    for k, v in {"SHM_FAKE_LLM": "0", "VLLM_BASE_URL": "http://vllm.test/v1", "OPENAI_BASE_URL": "http://judge.test/v1",
                 "OPENROUTER_BASE_URL": "https://openrouter.test/api/v1", "HTTP_BACKOFF_BASE_S": "0.001",
                 "HTTP_BACKOFF_MAX_S": "0.002", "HTTP_MAX_RETRIES": "4", "OPENROUTER_PROVIDER_ORDER": "deepinfra/fp8",
                 "EVALUATOR_THINKING": "true", "VLLM_DISABLE_THINKING": "true"}.items():
        monkeypatch.setenv(k, v)
    yield config_mod.reload()
    config_mod.reload()


def test_payload_rules(cfg):
    c = AsyncLLMClient(cfg)
    routes = cfg.routes()
    msgs = [{"role": "user", "content": "x"}]
    # vLLM: the "Qwen/" prefix gets the disable, except the answerer which keeps thinking ON
    judge = c.build_payload(routes["vllm"], "Qwen/Qwen3.8-27B", msgs, 0.0, None, "judge")
    assert judge["chat_template_kwargs"] == {"enable_thinking": False} and "max_tokens" not in judge
    answerer = c.build_payload(routes["vllm"], "Qwen/Qwen3.8-27B", msgs, 0.0, 128, "evaluator")
    assert "chat_template_kwargs" not in answerer and answerer["max_tokens"] == 128
    bare = c.build_payload(routes["vllm"], "qwen3.8-27b", msgs, 0.0, None, "judge")
    assert "chat_template_kwargs" not in bare          # the bare slug matches no prefix (the old bug, kept visible)
    # openai route on a non-OpenAI base URL is a vLLM
    assert routes["openai"].is_vllm
    # OpenRouter: pinning and reasoning cap only for matching models
    ds = c.build_payload(routes["openrouter"], "deepseek/deepseek-v4-flash-0731", msgs, 0.2, None, "reflector")
    assert ds["provider"] == {"order": ["deepinfra/fp8"], "allow_fallbacks": False, "only": ["deepinfra/fp8"]}
    assert ds["reasoning"] == {"effort": "low"} and "chat_template_kwargs" not in ds
    other = c.build_payload(routes["openrouter"], "openai/gpt-4.1", msgs, 0.2, None, "judge")
    assert "provider" not in other and "reasoning" not in other


def test_transient_402():
    assert is_transient_payment_required(402, "", "120")
    assert is_transient_payment_required(402, '{"reason":"in_flight_budget_exhausted"}', None)
    assert not is_transient_payment_required(402, "insufficient credits", None)
    assert not is_transient_payment_required(429, "", "1")


def test_retry_then_success_and_usage(cfg):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        if calls["n"] == 2:
            return httpx.Response(402, text='{"reason":"in_flight_budget_exhausted"}', headers={"Retry-After": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"ok\": 1}\n```"},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    async def go():
        c = AsyncLLMClient(cfg)
        c._bind_loop()
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        res = await c.chat(route="vllm", model="Qwen/Qwen3.8-27B", messages=[{"role": "user", "content": "q"}],
                           role="evaluator")
        bad = await c.chat(route="nope", model="m", messages=[])
        await c.aclose()
        return c, res, bad

    c, res, bad = asyncio.run(go())
    assert res.ok and extract_json(res.text) == {"ok": 1} and calls["n"] == 3
    assert c.usage_total["total_tokens"] == 10 and c.usage_by_role["evaluator"]["calls"] == 1
    assert not bad.ok and "unknown route" in bad.error


def test_non_retryable_4xx_and_empty_length(cfg):
    replies = iter([httpx.Response(400, text="bad request"),
                    httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})])

    async def go():
        c = AsyncLLMClient(cfg)
        c._bind_loop()
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(replies)))
        a = await c.chat(route="openai", model="m", messages=[{"role": "user", "content": "q"}])
        b = await c.chat(route="openai", model="m", messages=[{"role": "user", "content": "q"}], max_tokens=5)
        return a, b

    a, b = asyncio.run(go())
    assert not a.ok and a.error.startswith("HTTP 400")
    assert not b.ok and "budget exhausted" in b.error


def test_extract_json_prefers_committed_answer():
    text = "<think>draft {\"action\": \"refuse\"}</think>\nfinal: {\"action\": \"answer\", \"answer\": \"a}b\"}"
    assert extract_json(text) == {"action": "answer", "answer": "a}b"}
    assert extract_json('```json\n[{"op": "add",}]\n```', want=list) == [{"op": "add"}]
    assert extract_json("```json\n{\"a\": 1}\n```\n```json\n{\"a\": 2}\n```", last=True) == {"a": 2}
    assert extract_json("nothing") is None


@pytest.mark.live
def test_live_vllm_roundtrip():  # pragma: no cover - needs an endpoint
    import os

    if not os.environ.get("VLLM_BASE_URL"):
        pytest.skip("VLLM_BASE_URL not set")
    cfg = config_mod.reload()

    async def go():
        c = AsyncLLMClient(cfg)
        r = await c.chat(route="vllm", model=cfg.evaluator_model, messages=[{"role": "user", "content": "Say ok."}],
                         max_tokens=16, role="judge")
        await c.aclose()
        return r

    assert asyncio.run(go()).ok
