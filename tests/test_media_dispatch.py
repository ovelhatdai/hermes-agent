import base64
from pathlib import Path

import pytest

from gateway.platforms.base import SendResult
from gateway.platforms._custom import media_dispatch as md


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, *, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.content = _FakeStream([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._body.decode("utf-8", errors="replace")


class _FakeSession:
    def __init__(self, *, head_response=None, get_response=None):
        self._head_response = head_response or _FakeResponse(headers={})
        self._get_response = get_response or _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def head(self, *_args, **_kwargs):
        return self._head_response

    def get(self, *_args, **_kwargs):
        return self._get_response


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, fetchrow_results=None):
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetchrow_calls = []
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return None

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakePlatform:
    def __init__(self, result=None):
        self.result = result or SendResult(success=True, message_id="msg-123")
        self.calls = []

    async def send_document(self, **kwargs):
        self.calls.append(("document", kwargs))
        return self.result

    async def send_video(self, **kwargs):
        self.calls.append(("video", kwargs))
        return self.result

    async def send_image_file(self, **kwargs):
        self.calls.append(("image", kwargs))
        return self.result

    async def send_voice(self, **kwargs):
        self.calls.append(("voice", kwargs))
        return self.result

    async def send_animation(self, **kwargs):
        self.calls.append(("animation", kwargs))
        return self.result

    async def _send_media_to_bridge(self, **kwargs):
        self.calls.append(("bridge", kwargs))
        return self.result


@pytest.fixture(autouse=True)
def _reset_bucket(monkeypatch):
    monkeypatch.setattr(md, "_RATE_BUCKET", md.TokenBucket(md.RATE_LIMIT_PER_MIN))


@pytest.fixture
def sandbox_paths(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp-media"
    upload_dir = tmp_path / "uploads"
    tmp_dir.mkdir()
    upload_dir.mkdir()
    monkeypatch.setattr(md, "TMP_DIR", tmp_dir)
    monkeypatch.setattr(md, "UPLOAD_DIR", upload_dir)
    return tmp_dir, upload_dir


@pytest.mark.asyncio
async def test_base64_source_decodes(sandbox_paths, monkeypatch):
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "application/pdf")
    payload = b"spec-074-base64"

    result = await md.resolve_source(
        md.Base64Source(kind="base64", data=base64.b64encode(payload).decode("ascii")),
        "document",
        "application/pdf",
    )

    assert result.exists()
    assert result.read_bytes() == payload
    assert result.parent == sandbox_paths[0]


@pytest.mark.asyncio
async def test_url_source_fetches(sandbox_paths, monkeypatch):
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "application/pdf")
    body = b"x" * 10_240
    session = _FakeSession(
        head_response=_FakeResponse(headers={"content-length": str(len(body))}),
        get_response=_FakeResponse(body=body),
    )
    monkeypatch.setattr(md.aiohttp, "ClientSession", lambda *args, **kwargs: session)

    result = await md.resolve_source(
        md.UrlSource(kind="url", url="https://example.com/file.pdf"),
        "document",
        "application/pdf",
    )

    assert result.read_bytes() == body


@pytest.mark.asyncio
async def test_url_source_rejects_over_25mb(sandbox_paths, monkeypatch):
    monkeypatch.setattr(md, "MAX_BYTES", 25)
    session = _FakeSession(
        head_response=_FakeResponse(headers={"content-length": "26"}),
        get_response=_FakeResponse(body=b"x" * 26),
    )
    monkeypatch.setattr(md.aiohttp, "ClientSession", lambda *args, **kwargs: session)

    with pytest.raises(md.DispatchError) as excinfo:
        await md.resolve_source(
            md.UrlSource(kind="url", url="https://example.com/file.pdf"),
            "document",
            "application/pdf",
        )

    assert excinfo.value.http_code == 413


@pytest.mark.asyncio
async def test_file_path_whitelist_rejects_traversal(sandbox_paths):
    with pytest.raises(md.DispatchError) as excinfo:
        await md.resolve_source(
            md.FilePathSource(kind="file_path", path="../../etc/passwd"),
            "document",
            "application/pdf",
        )

    assert excinfo.value.http_code == 403


@pytest.mark.asyncio
async def test_file_path_whitelist_rejects_unsafe_root(sandbox_paths):
    with pytest.raises(md.DispatchError) as excinfo:
        await md.resolve_source(
            md.FilePathSource(kind="file_path", path="/root/secret.txt"),
            "document",
            "application/pdf",
        )

    assert excinfo.value.http_code == 403


@pytest.mark.asyncio
async def test_file_path_accepts_whitelisted(sandbox_paths, monkeypatch):
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "application/pdf")
    upload_file = sandbox_paths[1] / "foo.pdf"
    upload_file.write_bytes(b"pdf-data")

    result = await md.resolve_source(
        md.FilePathSource(kind="file_path", path=str(upload_file)),
        "document",
        "application/pdf",
    )

    assert result.read_bytes() == b"pdf-data"
    assert result.parent == sandbox_paths[0]


@pytest.mark.asyncio
async def test_mime_whitelist_rejects_exe(sandbox_paths, monkeypatch):
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "application/x-msdownload")
    payload = base64.b64encode(b"MZ").decode("ascii")

    with pytest.raises(md.DispatchError) as excinfo:
        await md.resolve_source(
            md.Base64Source(kind="base64", data=payload),
            "document",
            "application/pdf",
        )

    assert excinfo.value.http_code == 400


def test_filename_sanitization():
    with pytest.raises(md.DispatchError) as excinfo:
        md._safe_filename("../../evil.exe")

    assert excinfo.value.http_code == 400


@pytest.mark.asyncio
async def test_rate_limit_60_per_min():
    bucket = md.TokenBucket(60)

    for _ in range(60):
        assert await bucket.allow("5551991987972@s.whatsapp.net") is True

    assert await bucket.allow("5551991987972@s.whatsapp.net") is False


@pytest.mark.asyncio
async def test_idempotency_dedup_within_10min(monkeypatch):
    monkeypatch.setenv("HERMES_MEDIA_DISPATCH_ENABLED", "true")
    conn = _FakeConnection(
        fetchrow_results=[
            {
                "id": "dedup-log-id",
                "platform_msg_id": "wamid-1",
                "status": "sent",
                "created_at": object(),
            }
        ]
    )
    pool = _FakePool(conn)
    platform = _FakePlatform()
    deps = md.DispatcherDeps(pool=pool, get_whatsapp_platform=lambda: platform)

    result = await md.dispatch(
        md.MediaDispatchRequest(
            platform="whatsapp",
            chat_id="5551991987972",
            type="document",
            source=md.Base64Source(kind="base64", data=base64.b64encode(b"ignored").decode("ascii")),
            mimetype="application/pdf",
            filename="doc.pdf",
            idempotency_key="same-key",
        ),
        deps,
        caller_token_hash_value="caller-1",
    )

    assert result.status == "deduplicated"
    assert result.platform_msg_id == "wamid-1"
    assert platform.calls == []
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_idempotency_expires_after_10min(sandbox_paths, monkeypatch):
    monkeypatch.setenv("HERMES_MEDIA_DISPATCH_ENABLED", "true")
    monkeypatch.setattr(md.magic, "from_file", lambda *_args, **_kwargs: "audio/ogg")

    voice_file = sandbox_paths[1] / "note.ogg"
    voice_file.write_bytes(b"OggSvoice")

    conn = _FakeConnection(fetchrow_results=[None])
    pool = _FakePool(conn)
    platform = _FakePlatform(result=SendResult(success=True, message_id="voice-123"))
    deps = md.DispatcherDeps(pool=pool, get_whatsapp_platform=lambda: platform)

    result = await md.dispatch(
        md.MediaDispatchRequest(
            platform="whatsapp",
            chat_id="5551991987972",
            type="voice",
            source=md.FilePathSource(kind="file_path", path=str(voice_file)),
            mimetype="audio/ogg",
            filename="note.ogg",
            idempotency_key="expired-key",
        ),
        deps,
        caller_token_hash_value="caller-1",
    )

    assert result.status == "sent"
    assert result.platform_msg_id == "voice-123"
    assert len(conn.execute_calls) == 2
    assert platform.calls[0][0] == "bridge"
    assert platform.calls[0][1]["chat_id"] == "5551991987972@s.whatsapp.net"
    assert platform.calls[0][1]["media_type"] == "audio"


@pytest.mark.asyncio
async def test_feature_flag_disabled_raises_503(monkeypatch):
    monkeypatch.setenv("HERMES_MEDIA_DISPATCH_ENABLED", "false")
    conn = _FakeConnection(fetchrow_results=[None])
    pool = _FakePool(conn)
    deps = md.DispatcherDeps(pool=pool, get_whatsapp_platform=lambda: _FakePlatform())

    with pytest.raises(md.DispatchError) as excinfo:
        await md.dispatch(
            md.MediaDispatchRequest(
                platform="whatsapp",
                chat_id="5551991987972",
                type="document",
                source=md.Base64Source(kind="base64", data=base64.b64encode(b"payload").decode("ascii")),
                mimetype="application/pdf",
                filename="doc.pdf",
            ),
            deps,
            caller_token_hash_value="caller-1",
        )

    assert excinfo.value.http_code == 503
