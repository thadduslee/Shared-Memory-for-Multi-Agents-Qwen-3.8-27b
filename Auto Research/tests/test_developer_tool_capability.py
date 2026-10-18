from __future__ import annotations

import config
import nodes._transport as transport
from llm.client import ChatResult


class _Client:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


async def test_required_tool_choice_smoke_pass_keeps_required(monkeypatch):
    client = _Client(ChatResult(
        text="", content="", model="dev", route="vllm",
        tool_calls=[{"id": "call_1", "name": "developer_tool_smoke_test", "arguments": {}}],
    ))
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "DEV_TOOL_CHOICE", "required")
    monkeypatch.setattr("llm.get_llm_client", lambda: client)

    outcome = await transport.validate_developer_tool_calling()

    assert outcome["status"] == "passed"
    assert config.DEV_TOOL_CHOICE == "required"
    assert client.calls[0]["tool_choice"] == "required"


async def test_vllm_tool_parser_400_falls_back_to_auto(monkeypatch):
    client = _Client(ChatResult(
        text="", model="dev", route="vllm",
        error='HTTP 400: tool_choice="required" requires --tool-call-parser to be set',
    ))
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "DEV_TOOL_CHOICE", "required")
    monkeypatch.setattr("llm.get_llm_client", lambda: client)

    outcome = await transport.validate_developer_tool_calling()

    assert outcome["status"] == "fallback_to_auto"
    assert outcome["tool_parser_error"] is True
    assert outcome["effective_tool_choice"] == "auto"
    assert config.DEV_TOOL_CHOICE == "auto"
