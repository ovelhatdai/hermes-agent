import base64
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms._custom import media_dispatch as md
from gateway.platforms.api_server import (
    APIServerAdapter,
    body_limit_middleware,
    cors_middleware,
    security_headers_middleware,
)
from gateway.platforms.base import SendResult
from gateway.platforms._custom.media_dispatch_router import mount_media_dispatch_subapp


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, *, fetchrow_results=None, fetch_results=None):
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])
        self.fetchrow_calls = []
        self.fetch_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return None

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeWhatsAppPlatform:
    def __init__(self):
        self.calls = []

    async def _send_media_to_bridge(self, **kwargs):
        self.calls.append(("bridge", kwargs))
        return SendResult(success=True, message_id="msg-bridge-123")

    async def send_document(self, **kwargs):
        self.calls.append(("document", kwargs))
        return SendResult(success=True, message_id="msg-doc-123")

    async def send_image_file(self, **kwargs):
        self.calls.append(("image", kwargs))
        return SendResult(success=True, message_id="msg-image-123")

    async def send_video(self, **kwargs):
        self.calls.append(("video", kwargs))
        return SendResult(success=True, message_id="msg-video-123")

    async def send_voice(self, **kwargs):
        self.calls.append(("voice", kwargs))
        return SendResult(success=True, message_id="msg-voice-123")

    async def send_animation(self, **kwargs):
        self.calls.append(("animation", kwargs))
        return SendResult(success=True, message_id="msg-animation-123")


@pytest.fixture(autouse=True)
def _reset_media_dispatch_state(monkeypatch, tmp_path):
    tmp_dir = tmp_path / "tmp-media"
    upload_dir = tmp_path / "uploads"
    tmp_dir.mkdir()
    upload_dir.mkdir()
    monkeypatch.setattr(md, "TMP_DIR", tmp_dir)
    monkeypatch.setattr(md, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(md, "_RATE_BUCKET", md.TokenBucket(md.RATE_LIMIT_PER_MIN))
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "devtoken")
    monkeypatch.delenv("HERMES_MEDIA_DISPATCH_ENABLED", raising=False)


def _build_test_app(adapter: APIServerAdapter) -> web.Application:
    middlewares = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=middlewares)
    app["api_server_adapter"] = adapter
    mount_media_dispatch_subapp(app, adapter)
    return app


@pytest_asyncio.fixture
async def test_app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = SimpleNamespace(adapters={})
    adapter._media_dispatch_pool = _FakePool(_FakeConnection())
    async with TestClient(TestServer(_build_test_app(adapter))) as client:
        yield client


@pytest_asyncio.fixture
async def test_app_with_mock_bridge(monkeypatch):
    monkeypatch.setenv("HERMES_MEDIA_DISPATCH_ENABLED", "true")
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "audio/ogg")
    conn = _FakeConnection(fetchrow_results=[None])
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    fake_platform = _FakeWhatsAppPlatform()
    adapter.gateway_runner = SimpleNamespace(adapters={Platform.WHATSAPP: fake_platform})
    adapter._media_dispatch_pool = _FakePool(conn)
    async with TestClient(TestServer(_build_test_app(adapter))) as client:
        yield client, fake_platform, conn


@pytest_asyncio.fixture
async def test_app_with_seeded_db():
    conn = _FakeConnection(
        fetchrow_results=[
            {
                "total": 4,
                "sent": 2,
                "failed": 1,
                "deduplicated": 1,
                "bytes_total": 4096,
            }
        ],
        fetch_results=[
            [
                {"media_type": "document", "c": 3},
                {"media_type": "voice", "c": 1},
            ]
        ],
    )
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.gateway_runner = SimpleNamespace(adapters={})
    adapter._media_dispatch_pool = _FakePool(conn)
    async with TestClient(TestServer(_build_test_app(adapter))) as client:
        yield client, conn


@pytest.mark.asyncio
async def test_missing_bearer_401(test_app):
    response = await test_app.post("/api/gateway/send-media", json={})
    assert response.status == 401


@pytest.mark.asyncio
async def test_invalid_bearer_401(test_app):
    response = await test_app.post(
        "/api/gateway/send-media",
        json={},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status == 401


@pytest.mark.asyncio
async def test_valid_dispatch_with_mock_bridge(test_app_with_mock_bridge):
    client, fake_platform, conn = test_app_with_mock_bridge
    payload = {
        "platform": "whatsapp",
        "chat_id": "5551991987972",
        "type": "voice",
        "source": {
            "kind": "base64",
            "data": base64.b64encode(b"fake-ogg-payload").decode("ascii"),
        },
        "mimetype": "audio/ogg",
        "filename": "briefing.ogg",
        "caption": "briefing",
        "idempotency_key": "spec-074-router-happy-path",
    }

    response = await client.post(
        "/api/gateway/send-media",
        json=payload,
        headers={"Authorization": "Bearer devtoken"},
    )

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["status"] == "sent"
    assert body["platform_msg_id"] == "msg-bridge-123"
    assert fake_platform.calls[0][0] == "bridge"
    assert conn.execute_calls[0][0].strip().startswith("INSERT INTO hermes_media_dispatch_log")
    assert conn.execute_calls[1][0].strip().startswith("UPDATE hermes_media_dispatch_log")


@pytest.mark.asyncio
async def test_stats_endpoint_returns_shape(test_app_with_seeded_db):
    client, conn = test_app_with_seeded_db
    response = await client.get(
        "/api/gateway/send-media/stats",
        headers={"Authorization": "Bearer devtoken"},
    )

    assert response.status == 200
    body = await response.json()
    assert body == {
        "last_24h": {
            "total": 4,
            "sent": 2,
            "failed": 1,
            "deduplicated": 1,
            "bytes_total": 4096,
            "by_type": {"document": 3, "voice": 1},
        },
        "rate_limit_state": {"tracked_chats": 0},
    }
    assert len(conn.fetchrow_calls) == 1
    assert len(conn.fetch_calls) == 1


@pytest.mark.asyncio
async def test_send_media_requires_application_json(test_app):
    response = await test_app.post(
        "/api/gateway/send-media",
        data="{}",
        headers={"Authorization": "Bearer devtoken", "Content-Type": "text/plain"},
    )
    assert response.status == 415
