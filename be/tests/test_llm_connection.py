"""Real LangChain/OpenAI SDK transport tests; no credentials or remote calls."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langsmith import tracing_context
from openai import APITimeoutError, RateLimitError

from companion.connections import llm


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in ("COMPANION_LLM_PROVIDER", "COMPANION_LLM_MODEL", "OPENROUTER_API_KEY", "REQUESTY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    def deny(*args, **kwargs):
        raise AssertionError("Unexpected socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def configure(monkeypatch, provider="openrouter"):
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", provider)
    monkeypatch.setenv("COMPANION_LLM_MODEL", "openrouter/free")
    monkeypatch.setenv("OPENROUTER_API_KEY" if provider == "openrouter" else "REQUESTY_API_KEY",
                       "synthetic-not-a-credential")


def completion(message, finish="stop"):
    return {"id": "mock", "object": "chat.completion", "created": 1, "model": "synthetic-test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def test_requires_explicit_provider_model_and_key(monkeypatch):
    assert not llm.is_configured() and llm.create_chat_model() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-credential")
    assert llm.create_chat_model() is None
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", "openrouter")
    assert llm.create_chat_model() is None
    monkeypatch.setenv("COMPANION_LLM_MODEL", "openrouter/free")
    assert llm.is_configured()
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", "https://untrusted.invalid")
    assert not llm.is_configured()


@pytest.mark.parametrize("free_model", ["openrouter/free", "synthetic/model:free"])
def test_actual_sdk_tool_round_trip_with_mocked_transport(monkeypatch, free_model):
    configure(monkeypatch)
    monkeypatch.setenv("COMPANION_LLM_MODEL", free_model)
    monkeypatch.setattr(llm, "_load_catalog", lambda: {"data": [{"id": free_model,
        "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]}]})
    base = "https://openrouter.ai/api/v1"
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert str(request.url) == base + "/chat/completions"
        assert body["max_completion_tokens"] == 250
        assert body["provider"] == {"max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
                                    "allow_fallbacks": False, "require_parameters": True}
        assert body["plugins"] == []
        assert "models" not in body
        if len(seen) == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": None,
                "tool_calls": [{"id": "tool1", "type": "function", "function": {
                    "name": "coverage", "arguments": "{}"}}]}, "tool_calls"))
        assert body["messages"][-1] == {"role": "tool", "content": "unknown", "tool_call_id": "tool1"}
        return httpx.Response(200, json=completion({"role": "assistant", "content": '{"selected":[]}'}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, tracing_context(enabled=False):
        model = llm.create_chat_model(http_client=client)
        assert model.max_retries == 0 and model.request_timeout == 10.0
        bound = model.bind_tools([{"type": "function", "function": {"name": "coverage",
            "description": "Synthetic coverage", "parameters": {"type": "object", "properties": {}}}}])
        first = bound.invoke([HumanMessage(content="synthetic")])
        assert first.tool_calls[0]["name"] == "coverage"
        final = bound.invoke([HumanMessage(content="synthetic"), first,
                              ToolMessage(content="unknown", tool_call_id="tool1")])
        assert json.loads(final.content) == {"selected": []}
    assert len(seen) == 2


@pytest.mark.parametrize("failure", ["rate_limit", "timeout"])
def test_no_retry_amplification(monkeypatch, failure):
    configure(monkeypatch)
    seen = []

    def handler(request):
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "synthetic limit"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, tracing_context(enabled=False):
        model = llm.create_chat_model(http_client=client)
        with pytest.raises(APITimeoutError if failure == "timeout" else RateLimitError):
            model.invoke("synthetic")
    assert len(seen) == 1


@pytest.mark.parametrize("model", ["openai/gpt-paid", "openrouter/auto", "synthetic/model", "synthetic/model:free:online", "openrouter/free:online", ""])
def test_paid_and_alternate_routes_rejected(monkeypatch, model):
    configure(monkeypatch)
    monkeypatch.setenv("COMPANION_LLM_MODEL", model)
    assert not llm.is_configured()
    assert llm.create_chat_model() is None


def test_requesty_is_not_a_runtime_fallback(monkeypatch):
    configure(monkeypatch, "requesty")
    assert not llm.is_configured()
    assert llm.create_chat_model() is None
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", "openrouter")
    assert llm.create_chat_model() is None  # Requesty key must not be reused.


@pytest.mark.parametrize("fault", ["charged", "missing_price", "unknown", "no_tools", "invalid_price", "request_fee"])
def test_free_suffix_requires_zero_priced_tool_catalog(monkeypatch, fault):
    configure(monkeypatch)
    monkeypatch.setenv("COMPANION_LLM_MODEL", "synthetic/model:free")
    entry = {"id": "synthetic/model:free", "pricing": {"prompt": "0", "completion": "0", "request": "0"},
             "supported_parameters": ["tools"]}
    if fault == "charged":
        entry["pricing"]["completion"] = "0.00001"
    elif fault == "request_fee":
        entry["pricing"]["request"] = "0.001"
    elif fault == "missing_price":
        del entry["pricing"]["prompt"]
    elif fault == "invalid_price":
        entry["pricing"]["prompt"] = "NaN"
    elif fault == "no_tools":
        entry["supported_parameters"] = []
    else:
        entry["id"] = "another/model:free"
    monkeypatch.setattr(llm, "_load_catalog", lambda: {"data": [entry]})
    with pytest.raises(ValueError, match="catalog-verified"):
        llm.create_chat_model()


def test_named_free_router_needs_no_catalog_network(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(llm, "_load_catalog", lambda: pytest.fail("Router must not fetch catalog"))
    model = llm.create_chat_model()
    assert model.model_name == "openrouter/free"
    assert model.extra_body["provider"]["max_price"]["request"] == 0


def test_catalog_failure_fails_closed_without_model_fallback(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("COMPANION_LLM_MODEL", "synthetic/model:free")
    def unavailable():
        raise ValueError("Free model catalog validation unavailable.")
    monkeypatch.setattr(llm, "_load_catalog", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        llm.create_chat_model()


def test_async_transport(monkeypatch):
    configure(monkeypatch)
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion({"role": "assistant", "content": '{"selected":[]}'}))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with tracing_context(enabled=False):
                model = llm.create_chat_model(http_async_client=client)
                assert json.loads((await model.ainvoke("synthetic")).content) == {"selected": []}

    asyncio.run(exercise())
    assert len(seen) == 1
