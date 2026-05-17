import asyncio

import pytest

from agent import llm_fallback


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise llm_fallback.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=llm_fallback.httpx.Request("POST", "https://example.test"),
                response=llm_fallback.httpx.Response(self.status_code, text=self.text),
            )


class FakeAsyncClient:
    calls = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "api.deepseek.com" in url:
            return FakeResponse(429, {"error": "rate limited"}, "rate limit")
        return FakeResponse(
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": "fallback ok"}]}}
                ]
            },
        )


@pytest.fixture(autouse=True)
def clear_calls(monkeypatch):
    FakeAsyncClient.calls = []
    monkeypatch.setattr(llm_fallback.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")


def test_deepseek_429_falls_back_to_gemini():
    data = asyncio.run(
        llm_fallback.call_llm_with_fallback(
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "oi"}],
                "tools": [{"type": "function", "function": {"name": "gcal_list_events"}}],
            },
            primary_api_key="deepseek-test",
            primary_base_url="https://api.deepseek.com/v1",
        )
    )

    assert data["fallback_used"] is True
    assert data["provider"] == "gemini"
    assert data["choices"][0]["message"]["content"] == "fallback ok"
    assert "api.deepseek.com/v1/chat/completions" in FakeAsyncClient.calls[0][0]
    assert "generativelanguage.googleapis.com" in FakeAsyncClient.calls[1][0]
    assert FakeAsyncClient.calls[0][1]["json"]["tool_choice"] == "auto"
