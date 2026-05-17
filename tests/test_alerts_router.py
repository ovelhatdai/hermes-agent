import asyncio
import hmac
import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms._custom.alerts_router import _DEDUP_CACHE, _SLO_DEDUP_CACHE, mount_alerts_subapp
from gateway.platforms.api_server import (
    APIServerAdapter,
    body_limit_middleware,
    cors_middleware,
    security_headers_middleware,
)
from gateway.platforms.base import SendResult


class _FakeWhatsAppPlatform:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="msg-alert-123")


@pytest.fixture(autouse=True)
def _reset_alert_state(monkeypatch):
    _DEDUP_CACHE.clear()
    _SLO_DEDUP_CACHE.clear()
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "devtoken")
    monkeypatch.setenv("HERMES_SLO_ALERT_HMAC_SECRET", "slosecret")
    monkeypatch.setenv("ASHLEY_PHONE_E164", "5551991079067")


def _build_test_app(adapter: APIServerAdapter) -> web.Application:
    middlewares = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=middlewares)
    app["api_server_adapter"] = adapter
    mount_alerts_subapp(app, adapter)
    return app


def _slo_headers(payload: dict, secret: str = "slosecret") -> tuple[bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Signature": signature}


@pytest_asyncio.fixture
async def test_app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = SimpleNamespace(adapters={Platform.WHATSAPP: _FakeWhatsAppPlatform()})
    async with TestClient(TestServer(_build_test_app(adapter))) as client:
        yield client, adapter.gateway_runner.adapters[Platform.WHATSAPP]


@pytest.mark.asyncio
async def test_missing_bearer_401(test_app):
    client, _fake_platform = test_app
    response = await client.post("/api/gateway/alerts/des-sac", json={})
    assert response.status == 401
    assert await response.json() == {"ok": False, "error": "missing_bearer"}


@pytest.mark.asyncio
async def test_invalid_bearer_401(test_app):
    client, _fake_platform = test_app
    response = await client.post(
        "/api/gateway/alerts/des-sac",
        json={},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status == 401
    assert await response.json() == {"ok": False, "error": "invalid_bearer"}


@pytest.mark.asyncio
async def test_requires_application_json(test_app):
    client, _fake_platform = test_app
    response = await client.post(
        "/api/gateway/alerts/des-sac",
        data="{}",
        headers={"Authorization": "Bearer devtoken", "Content-Type": "text/plain"},
    )
    assert response.status == 415
    assert await response.json() == {"ok": False, "error": "unsupported_media_type"}


@pytest.mark.asyncio
async def test_invalid_payload_returns_400(test_app):
    client, _fake_platform = test_app
    response = await client.post(
        "/api/gateway/alerts/des-sac",
        json={"telefone": "+5551999999999"},
        headers={"Authorization": "Bearer devtoken"},
    )
    assert response.status == 400
    body = await response.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_request"
    assert body["detail"]


@pytest.mark.asyncio
async def test_valid_alert_is_queued_and_dispatches_to_whatsapp(test_app):
    client, fake_platform = test_app
    payload = {
        "cliente_nome": "Teste Task07",
        "telefone": "+5551999999999",
        "chatwoot_conv_url": "https://chatwoot.advogando100k.com.br/app/accounts/2/conversations/99999",
        "ultimo_atendimento_base44": "2025-12-01",
        "detectado_em": "2026-04-21T18:00:00Z",
        "chip_origem": "clara-des-joanne",
    }

    response = await client.post(
        "/api/gateway/alerts/des-sac",
        json=payload,
        headers={"Authorization": "Bearer devtoken"},
    )

    assert response.status == 200
    assert await response.json() == {
        "ok": True,
        "status": "queued",
        "canal": "whatsapp-hermes",
        "destino": "5551991079067",
    }

    await asyncio.sleep(0)
    assert fake_platform.calls == [
        {
            "chat_id": "5551991079067@s.whatsapp.net",
            "content": (
                "ja-cliente detectado no DES\n\n"
                "Cliente: Teste Task07\n"
                "Telefone: +5551999999999\n"
                "Detectado: 2026-04-21T18:00:00Z\n"
                "Ultimo atendimento Base44: 2025-12-01\n"
                "Chip origem: clara-des-joanne\n\n"
                "Conv Chatwoot: https://chatwoot.advogando100k.com.br/app/accounts/2/conversations/99999\n\n"
                "Clara DES nao respondeu porque o contato ja e cliente ativo. "
                "Favor assumir se for atendimento novo."
            ),
            "reply_to": None,
            "metadata": None,
        }
    ]


@pytest.mark.asyncio
async def test_same_lead_within_24h_is_deduplicated(test_app):
    client, fake_platform = test_app
    payload = {
        "cliente_nome": "Teste Task07",
        "telefone": "+55 (51) 99999-9999",
        "chatwoot_conv_url": "https://chatwoot.advogando100k.com.br/app/accounts/2/conversations/99999",
        "detectado_em": "2026-04-21T18:00:00Z",
    }

    first = await client.post(
        "/api/gateway/alerts/des-sac",
        json=payload,
        headers={"Authorization": "Bearer devtoken"},
    )
    second = await client.post(
        "/api/gateway/alerts/des-sac",
        json=payload,
        headers={"Authorization": "Bearer devtoken"},
    )

    assert first.status == 200
    assert second.status == 200
    assert await second.json() == {"ok": True, "status": "dedup_skipped"}
    await asyncio.sleep(0)
    assert len(fake_platform.calls) == 1


@pytest.mark.asyncio
async def test_missing_ashley_phone_returns_config_missing(monkeypatch, test_app):
    client, fake_platform = test_app
    monkeypatch.delenv("ASHLEY_PHONE_E164", raising=False)

    response = await client.post(
        "/api/gateway/alerts/des-sac",
        json={
            "cliente_nome": "Teste Task07",
            "telefone": "+5551999999999",
            "chatwoot_conv_url": "https://chatwoot.advogando100k.com.br/app/accounts/2/conversations/99999",
            "detectado_em": "2026-04-21T18:00:00Z",
        },
        headers={"Authorization": "Bearer devtoken"},
    )

    assert response.status == 200
    assert await response.json() == {"ok": False, "status": "config_missing"}
    await asyncio.sleep(0)
    assert fake_platform.calls == []


@pytest.mark.asyncio
async def test_slo_alert_missing_hmac_returns_401(test_app):
    client, _fake_platform = test_app
    response = await client.post(
        "/api/webhook/slo-alert",
        json={
            "severity": "warn",
            "chip": "dra-clara-des-kailany-v2",
            "metric": "ttfr",
            "value_seconds": 135,
            "threshold_seconds": 60,
            "windows_violated": 3,
            "recipients": ["5551991987972"],
        },
    )

    assert response.status == 401
    assert await response.json() == {"ok": False, "error": "missing_signature"}


@pytest.mark.asyncio
async def test_slo_alert_invalid_hmac_returns_401(test_app):
    client, _fake_platform = test_app
    payload = {
        "severity": "warn",
        "chip": "dra-clara-des-kailany-v2",
        "metric": "ttfr",
        "value_seconds": 135,
        "threshold_seconds": 60,
        "windows_violated": 3,
        "recipients": ["5551991987972"],
    }
    body, headers = _slo_headers(payload, secret="wrong")

    response = await client.post("/api/webhook/slo-alert", data=body, headers=headers)

    assert response.status == 401
    assert await response.json() == {"ok": False, "error": "invalid_signature"}


@pytest.mark.asyncio
async def test_slo_alert_valid_hmac_dispatches_to_recipients(test_app):
    client, fake_platform = test_app
    payload = {
        "severity": "warn",
        "chip": "dra-clara-des-kailany-v2",
        "metric": "ttfr",
        "value_seconds": 135,
        "threshold_seconds": 60,
        "windows_violated": 3,
        "recipients": ["+55 (51) 99198-7972", "5551984213925"],
    }
    body, headers = _slo_headers(payload)

    response = await client.post("/api/webhook/slo-alert", data=body, headers=headers)

    assert response.status == 200
    assert await response.json() == {"ok": True, "status": "queued", "recipients": 2}
    await asyncio.sleep(0)
    assert [call["chat_id"] for call in fake_platform.calls] == [
        "5551991987972@s.whatsapp.net",
        "5551984213925@s.whatsapp.net",
    ]
    assert fake_platform.calls[0]["content"] == (
        "⚠️ [WARN] chip=dra-clara-des-kailany-v2 "
        "metric=ttfr=135s threshold=60s há 3 janelas"
    )


@pytest.mark.asyncio
async def test_slo_alert_deduplicates_same_metric_for_15min(test_app):
    client, fake_platform = test_app
    payload = {
        "severity": "warn",
        "chip": "dra-clara-des-kailany-v2",
        "metric": "ttfr",
        "value_seconds": 135,
        "threshold_seconds": 60,
        "windows_violated": 3,
        "recipients": ["5551991987972"],
    }
    body, headers = _slo_headers(payload)

    first = await client.post("/api/webhook/slo-alert", data=body, headers=headers)
    second = await client.post("/api/webhook/slo-alert", data=body, headers=headers)

    assert first.status == 200
    assert second.status == 200
    assert await second.json() == {"ok": True, "status": "dedup_skipped"}
    await asyncio.sleep(0)
    assert len(fake_platform.calls) == 1
