"""Explicit, backend-only gateway configuration; LangChain owns model execution."""
from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
    from langchain_core.language_models import BaseChatModel

_BASE_URL = "https://openrouter.ai/api/v1"
_FREE_VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*:free")


def _free_model_name(model: str) -> bool:
    return model == "openrouter/free" or _FREE_VARIANT.fullmatch(model) is not None


def _load_catalog() -> dict:
    """Public metadata only: never send a key to the catalog endpoint."""
    import httpx

    try:
        with httpx.Client(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            response = client.get(_BASE_URL + "/models")
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("Free model catalog validation unavailable.") from None


def _validate_free_model(model: str) -> None:
    # This named router is documented to select only free models and has no fee.
    if model == "openrouter/free":
        return
    if not _free_model_name(model):
        raise ValueError("Only free OpenRouter models are permitted.")
    catalog = _load_catalog()
    for item in catalog.get("data", []):
        if item.get("id") != model:
            continue
        pricing = item.get("pricing", {})
        try:
            zero = (all(key in pricing for key in ("prompt", "completion"))
                    and all(Decimal(str(value)) == 0 for value in pricing.values()))
        except (InvalidOperation, ValueError, TypeError):
            zero = False
        if zero and "tools" in item.get("supported_parameters", []):
            return
        break
    raise ValueError("Model is not catalog-verified as zero-priced with tools.")


def _configuration() -> tuple[str, str, str] | None:
    """Free OpenRouter only; Requesty keys, paid IDs and fallbacks stay unused."""
    provider = os.environ.get("COMPANION_LLM_PROVIDER", "").strip().lower()
    model = os.environ.get("COMPANION_LLM_MODEL", "").strip()
    if provider != "openrouter" or len(model) > 200 or not _free_model_name(model):
        return None
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
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

    _, model, key = config
    _validate_free_model(model)
    options = {}
    if http_client is not None:
        options["http_client"] = http_client
    if http_async_client is not None:
        options["http_async_client"] = http_async_client
    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=_BASE_URL,
        use_responses_api=False,
        timeout=10.0,
        max_retries=0,
        max_tokens=250,
        streaming=False,
        callbacks=[],
        extra_body={
            "provider": {
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "plugins": [],
            "reasoning": {"enabled": False},
        },
        **options,
    )
