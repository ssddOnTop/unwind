"""Explicit, backend-only gateway configuration; LangChain owns model execution."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
    from langchain_core.language_models import BaseChatModel

_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "requesty": "https://router.requesty.ai/v1",
}
_KEY_NAMES = {"openrouter": "OPENROUTER_API_KEY", "requesty": "REQUESTY_API_KEY"}


def _configuration() -> tuple[str, str, str] | None:
    """No implicit provider/model, arbitrary URLs, or front-end key parameters."""
    provider = os.environ.get("COMPANION_LLM_PROVIDER", "").strip().lower()
    model = os.environ.get("COMPANION_LLM_MODEL", "").strip()
    if provider not in _BASE_URLS or not model or len(model) > 200:
        return None
    key = os.environ.get(_KEY_NAMES[provider], "").strip()
    if not key:
        return None
    return provider, model, key


def is_configured() -> bool:
    return _configuration() is not None


def is_timeout(error: BaseException) -> bool:
    """Classify transport timeouts without exposing provider error text."""
    if isinstance(error, TimeoutError):
        return True
    try:
        import httpx
        from openai import APITimeoutError
    except ImportError:
        return False
    return isinstance(error, (httpx.TimeoutException, APITimeoutError))


def create_chat_model(
    *,
    http_client: httpx.Client | None = None,
    http_async_client: httpx.AsyncClient | None = None,
) -> BaseChatModel | None:
    """Return the sponsor's chat model, not an alternate LLM/tool loop.

    Clients are injectable for in-process MockTransport tests only. Runtime callers
    receive fixed gateway URLs, a short transport timeout and zero SDK retries.
    The synthesis boundary additionally limits the entire run and every model call.
    """
    config = _configuration()
    if config is None:
        return None
    from langchain_openai import ChatOpenAI

    provider, model, key = config
    options = {}
    if http_client is not None:
        options["http_client"] = http_client
    if http_async_client is not None:
        options["http_async_client"] = http_async_client
    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=_BASE_URLS[provider],
        use_responses_api=False,
        timeout=10.0,
        max_retries=0,
        max_tokens=250,
        streaming=False,
        callbacks=[],
        **options,
    )
