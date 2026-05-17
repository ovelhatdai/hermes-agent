"""DeepSeek -> Gemini fallback for Hermes chat-completions calls.

The main Hermes agent loop is synchronous, but this module keeps the provider
HTTP calls async so the fallback behavior can be unit-tested without the OpenAI
SDK and without changing the agent's tool-call contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from types import SimpleNamespace
from typing import Any, Mapping

import httpx


log = logging.getLogger("hermes.llm")

_FALLBACKABLE_STATUS_CODES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504, 529}
_OPENAI_PAYLOAD_KEYS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "stop",
    "seed",
}


class FallbackableLLMError(RuntimeError):
    """Primary provider failed in a way that should trigger Gemini fallback."""


def _env_float(names: tuple[str, ...], default: float) -> float:
    for name in names:
        value = os.getenv(name)
        if value:
            try:
                return float(value)
            except ValueError:
                log.warning("Invalid %s=%r; using %.1fs", name, value, default)
                return default
    return default


def _timeout_seconds(timeout: float | None = None) -> float:
    if timeout is not None:
        return float(timeout)
    return _env_float(("LLM_TIMEOUT", "LLM_TIMEOUT_SECONDS", "HERMES_LLM_TIMEOUT"), 30.0)


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text") or part.get("input_text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _openai_to_gemini(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role") or "").lower()
        text = _text_from_content(message.get("content")).strip()
        if not text:
            continue

        if role in {"system", "developer"}:
            system_parts.append(text)
            continue
        if role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
            continue
        if role == "tool":
            tool_name = message.get("name") or message.get("tool_call_id") or "tool"
            text = f"Tool result ({tool_name}): {text}"
        contents.append({"role": "user", "parts": [{"text": text}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Respond to the user."}]})

    system_instruction = None
    if system_parts:
        system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return system_instruction, contents


def _primary_api_key(explicit: str | None) -> str:
    key = (explicit or "").strip()
    if key.startswith("${") or not key:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeek primary calls")
    return key


def _deepseek_chat_url(primary_base_url: str | None) -> str:
    base_url = (primary_base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _build_deepseek_payload(api_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: api_kwargs[key] for key in _OPENAI_PAYLOAD_KEYS if key in api_kwargs}
    payload["model"] = payload.get("model") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if payload.get("tools") and "tool_choice" not in payload:
        payload["tool_choice"] = "auto"
    return {key: value for key, value in payload.items() if value is not None}


def _is_quota_or_rate_limit(body: str) -> bool:
    lower = (body or "").lower()
    return any(term in lower for term in ("quota", "rate limit", "rate_limit", "insufficient_quota"))


def _raise_if_primary_failed(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body_preview = response.text[:500]
    if response.status_code in _FALLBACKABLE_STATUS_CODES or _is_quota_or_rate_limit(body_preview):
        raise FallbackableLLMError(f"DeepSeek HTTP {response.status_code}: {body_preview}")
    response.raise_for_status()


def _is_fallbackable_exception(exc: BaseException) -> bool:
    if isinstance(exc, FallbackableLLMError):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        body = exc.response.text if exc.response is not None else ""
        return bool(status in _FALLBACKABLE_STATUS_CODES or _is_quota_or_rate_limit(body))
    return False


async def call_gemini(messages: list[dict[str, Any]], timeout: float | None = None) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY is required for Gemini fallback")

    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    system_instruction, contents = _openai_to_gemini(messages)
    payload: dict[str, Any] = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=_timeout_seconds(timeout)) as client:
        response = await client.post(url, params={"key": gemini_key}, json=payload)
        response.raise_for_status()

    data = response.json()
    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    log.info("[LLM] provider=gemini fallback_used=true")
    return {
        "id": f"gemini-fallback-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": gemini_model,
        "provider": "gemini",
        "fallback_used": True,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": None,
    }


async def call_llm_with_fallback(
    api_kwargs: Mapping[str, Any],
    *,
    primary_api_key: str | None = None,
    primary_base_url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call DeepSeek first and fall back to Gemini on provider failure."""
    timeout_s = _timeout_seconds(timeout)
    payload = _build_deepseek_payload(api_kwargs)
    messages = list(payload.get("messages") or [])

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                _deepseek_chat_url(primary_base_url),
                headers={"Authorization": f"Bearer {_primary_api_key(primary_api_key)}"},
                json=payload,
            )
            _raise_if_primary_failed(response)
        data = response.json()
        data.setdefault("provider", "deepseek")
        data.setdefault("fallback_used", False)
        log.info("[LLM] provider=deepseek fallback_used=false")
        return data
    except Exception as exc:
        if not _is_fallbackable_exception(exc):
            raise
        log.warning(
            "[LLM] provider=deepseek fallback_used=false primary_failed=true error=%s",
            str(exc)[:500],
        )
        return await call_gemini(messages, timeout=timeout_s)


def _to_namespace(value: Any) -> Any:
    if isinstance(value, Mapping):
        return SimpleNamespace(**{str(key): _to_namespace(val) for key, val in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def openai_response_from_chat_completion(data: Mapping[str, Any]) -> SimpleNamespace:
    return _to_namespace(data)


def call_llm_with_fallback_response(
    api_kwargs: Mapping[str, Any],
    *,
    primary_api_key: str | None = None,
    primary_base_url: str | None = None,
    timeout: float | None = None,
) -> SimpleNamespace:
    data = asyncio.run(
        call_llm_with_fallback(
            api_kwargs,
            primary_api_key=primary_api_key,
            primary_base_url=primary_base_url,
            timeout=timeout,
        )
    )
    return openai_response_from_chat_completion(data)


def should_use_deepseek_gemini_fallback(
    *,
    api_mode: str | None,
    provider: str | None,
    model: str | None,
    base_url: str | None,
) -> bool:
    fallback_provider = os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower()
    if fallback_provider != "gemini":
        return False
    if (api_mode or "").strip().lower() != "chat_completions":
        return False

    provider_norm = (provider or "").strip().lower()
    model_norm = (model or "").strip().lower()
    base_norm = (base_url or "").strip().lower()
    return (
        provider_norm in {"deepseek", "custom:deepseek"}
        or model_norm.startswith("deepseek")
        or "api.deepseek.com" in base_norm
    )
