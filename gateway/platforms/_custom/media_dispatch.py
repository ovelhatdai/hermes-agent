"""
SPEC-074 — Media dispatch module.
Schema + source resolver + rate limit + dedup + dispatcher.
Called by api_server.py. Does not expose HTTP directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
import pathlib
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal, Optional, Union

import aiohttp
import magic
from pydantic import BaseModel, Field, HttpUrl

from gateway.platforms.base import SendResult

MAX_BYTES = int(os.getenv("HERMES_MEDIA_MAX_BYTES", 26_214_400))
RATE_LIMIT_PER_MIN = int(os.getenv("HERMES_MEDIA_RATE_LIMIT_PER_MIN", 60))
DEDUP_WINDOW_S = int(os.getenv("HERMES_MEDIA_DEDUP_WINDOW_SECONDS", 600))
FETCH_TIMEOUT_S = int(os.getenv("HERMES_MEDIA_FETCH_TIMEOUT_S", 30))
TMP_DIR = pathlib.Path(os.getenv("HERMES_MEDIA_TMP_DIR", "/tmp/hermes-media"))
UPLOAD_DIR = pathlib.Path(os.getenv("HERMES_MEDIA_UPLOAD_DIR", "/opt/hermes-uploads"))
BRIDGE_RETRY = int(os.getenv("HERMES_MEDIA_BRIDGE_RETRY", 2))

MIME_WHITELIST = {
    "document": {
        "application/json",
        "application/pdf",
        "application/zip",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    },
    "audio": {
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/mp4",
        "audio/aac",
        "audio/x-m4a",
    },
    "voice": {"audio/ogg"},
    "video": {"video/mp4", "video/webm"},
    "image": {"image/jpeg", "image/png", "image/webp"},
    "animation": {"image/gif", "video/mp4"},
}

MIME_EQUIVALENTS = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"application/zip"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {"application/zip"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {"application/zip"},
    "text/csv": {"text/plain"},
    "audio/ogg": {"application/ogg"},
    "audio/wav": {"audio/x-wav", "audio/wave"},
    "audio/x-m4a": {"audio/mp4"},
}

EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

FILENAME_SAFE_RE = re.compile(r"^[A-Za-z0-9._\- ]+$")
JID_SUFFIX = "@s.whatsapp.net"


class Base64Source(BaseModel):
    kind: Literal["base64"]
    data: str


class UrlSource(BaseModel):
    kind: Literal["url"]
    url: HttpUrl


class FilePathSource(BaseModel):
    kind: Literal["file_path"]
    path: str


Source = Annotated[
    Union[Base64Source, UrlSource, FilePathSource],
    Field(discriminator="kind"),
]


class MediaDispatchRequest(BaseModel):
    platform: Literal["whatsapp"]
    chat_id: str
    type: Literal["document", "audio", "voice", "video", "image", "animation"]
    source: Source
    mimetype: str
    filename: str
    caption: Optional[str] = Field(None, max_length=1024)
    idempotency_key: Optional[str] = Field(None, max_length=128)


class DispatchResult(BaseModel):
    ok: bool
    log_id: str
    platform_msg_id: Optional[str]
    status: Literal["sent", "failed", "deduplicated"]
    duration_ms: int
    error: Optional[str] = None


class DispatchError(Exception):
    def __init__(self, http_code: int, message: str):
        self.http_code = http_code
        self.message = message
        super().__init__(message)


class TokenBucket:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, chat_id: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            bucket = self._hits[chat_id]
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= self.per_min:
                return False
            bucket.append(now)
            return True

    def clear(self) -> None:
        self._hits.clear()

    def tracked_count(self) -> int:
        return len(self._hits)


_RATE_BUCKET = TokenBucket(RATE_LIMIT_PER_MIN)


@dataclass(slots=True)
class DispatcherDeps:
    pool: Any
    get_whatsapp_platform: Callable[[], Any]


def caller_token_hash(bearer: str) -> str:
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:16]


def rate_limit_state() -> dict[str, int]:
    return {"tracked_chats": _RATE_BUCKET.tracked_count()}


def _normalize_mimetype(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def _normalize_chat_id(chat_id: str) -> str:
    value = chat_id.strip()
    if not value:
        raise DispatchError(400, "chat_id vazio")
    if "@" in value:
        return value
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise DispatchError(400, f"chat_id invalido: {chat_id}")
    return f"{digits}{JID_SUFFIX}"


def _safe_filename(name: str) -> str:
    value = name.strip()
    if not value:
        raise DispatchError(400, "filename vazio")
    if "/" in value or "\\" in value:
        raise DispatchError(400, f"filename inseguro: {value}")
    base = pathlib.PurePath(value).name
    if base != value or not FILENAME_SAFE_RE.match(base):
        raise DispatchError(400, f"filename inseguro: {value}")
    return base


def _allowed_path_prefixes() -> tuple[pathlib.Path, ...]:
    return (TMP_DIR, UPLOAD_DIR)


def _is_within(path: pathlib.Path, base: pathlib.Path) -> bool:
    try:
        path.relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _ext_from_mime(mime: str) -> str:
    normalized = _normalize_mimetype(mime)
    return EXTENSION_BY_MIME.get(normalized) or mimetypes.guess_extension(normalized) or ""


def _mime_allowed(media_type: str, expected_mime: str, sniffed_mime: str) -> bool:
    normalized_expected = _normalize_mimetype(expected_mime)
    normalized_sniffed = _normalize_mimetype(sniffed_mime)
    allowed_for_type = MIME_WHITELIST.get(media_type, set())
    if normalized_expected not in allowed_for_type:
        return False
    if normalized_sniffed in allowed_for_type:
        return True
    return normalized_sniffed in MIME_EQUIVALENTS.get(normalized_expected, set())


async def resolve_source(
    src: Source,
    expected_type: str,
    expected_mime: str,
) -> pathlib.Path:
    """Convert a source payload into a real file in TMP_DIR."""
    normalized_mime = _normalize_mimetype(expected_mime)
    TMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = TMP_DIR / f"{uuid.uuid4().hex}{_ext_from_mime(normalized_mime)}"

    try:
        if src.kind == "base64":
            try:
                raw = base64.b64decode(src.data, validate=True)
            except Exception as exc:
                raise DispatchError(400, f"base64 invalido: {exc}") from exc
            if len(raw) > MAX_BYTES:
                raise DispatchError(413, f"payload {len(raw)} > {MAX_BYTES}")
            target.write_bytes(raw)

        elif src.kind == "url":
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    async with session.head(str(src.url), allow_redirects=True) as response:
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > MAX_BYTES:
                            raise DispatchError(413, f"url content-length {content_length} > {MAX_BYTES}")
                except DispatchError:
                    raise
                except Exception:
                    pass

                async with session.get(str(src.url), allow_redirects=True) as response:
                    if response.status >= 400:
                        raise DispatchError(400, f"url fetch falhou: status={response.status}")
                    total = 0
                    with target.open("wb") as file_descriptor:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > MAX_BYTES:
                                raise DispatchError(413, f"url body > {MAX_BYTES}")
                            file_descriptor.write(chunk)

        elif src.kind == "file_path":
            source_path = pathlib.Path(src.path).expanduser().resolve()
            if not any(_is_within(source_path, base) for base in _allowed_path_prefixes()):
                raise DispatchError(403, "file_path fora da whitelist")
            if not source_path.exists() or not source_path.is_file():
                raise DispatchError(404, "file_path inexistente")
            size_bytes = source_path.stat().st_size
            if size_bytes > MAX_BYTES:
                raise DispatchError(413, f"file {size_bytes} > {MAX_BYTES}")
            shutil.copyfile(source_path, target)

        sniffed = magic.from_file(str(target), mime=True)
        if not _mime_allowed(expected_type, normalized_mime, sniffed):
            raise DispatchError(400, f"mime rejeitado expected={normalized_mime} sniffed={sniffed}")

        return target

    except DispatchError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise DispatchError(400, f"source_resolution_failed: {exc}") from exc


async def lookup_dedup(pool: Any, idempotency_key: str, chat_id: str) -> Optional[dict[str, Any]]:
    key = idempotency_key.strip()
    if not key:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, platform_msg_id, status, created_at
            FROM hermes_media_dispatch_log
            WHERE idempotency_key = $1 AND chat_id = $2
              AND created_at > NOW() - ($3 * INTERVAL '1 second')
              AND status IN ('sent', 'pending')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            key,
            chat_id,
            DEDUP_WINDOW_S,
        )
        return dict(row) if row else None


async def _send_audio_via_bridge(
    adapter: Any,
    chat_id: str,
    file_path: pathlib.Path,
    caption: Optional[str],
    file_name: str,
) -> Any:
    bridge_send = getattr(adapter, "_send_media_to_bridge", None)
    if callable(bridge_send):
        return await bridge_send(
            chat_id=chat_id,
            file_path=str(file_path),
            media_type="audio",
            caption=caption,
            file_name=file_name,
        )
    return await adapter.send_voice(chat_id=chat_id, audio_path=str(file_path), caption=caption)


async def _send_animation_via_bridge(
    adapter: Any,
    chat_id: str,
    file_path: pathlib.Path,
    caption: Optional[str],
    file_name: str,
    mimetype: str,
) -> Any:
    bridge_send = getattr(adapter, "_send_media_to_bridge", None)
    if callable(bridge_send):
        media_type = "video" if _normalize_mimetype(mimetype).startswith("video/") else "image"
        return await bridge_send(
            chat_id=chat_id,
            file_path=str(file_path),
            media_type=media_type,
            caption=caption,
            file_name=file_name,
        )
    return await adapter.send_animation(chat_id=chat_id, animation_url=str(file_path), caption=caption)


def _resolve_sender(
    adapter: Any,
    req: MediaDispatchRequest,
    chat_id: str,
    file_path: pathlib.Path,
    file_name: str,
) -> Callable[[], Any]:
    method_map = {
        "document": lambda: adapter.send_document(
            chat_id=chat_id,
            file_path=str(file_path),
            file_name=file_name,
            caption=req.caption,
        ),
        "video": lambda: adapter.send_video(
            chat_id=chat_id,
            video_path=str(file_path),
            caption=req.caption,
        ),
        "image": lambda: adapter.send_image_file(
            chat_id=chat_id,
            image_path=str(file_path),
            caption=req.caption,
        ),
        "audio": lambda: _send_audio_via_bridge(adapter, chat_id, file_path, req.caption, file_name),
        "voice": lambda: _send_audio_via_bridge(adapter, chat_id, file_path, req.caption, file_name),
        "animation": lambda: _send_animation_via_bridge(
            adapter,
            chat_id,
            file_path,
            req.caption,
            file_name,
            req.mimetype,
        ),
    }
    return method_map[req.type]


def _coerce_send_result(result: Any) -> tuple[bool, Optional[str], Optional[str]]:
    if isinstance(result, SendResult):
        return result.success, result.message_id, result.error
    if isinstance(result, str):
        return True, result, None
    if isinstance(result, dict):
        success = bool(result.get("success", True))
        message_id = result.get("message_id") or result.get("messageId")
        error = result.get("error")
        return success, message_id, error
    raise DispatchError(502, f"unexpected_send_result: {type(result).__name__}")


async def dispatch(
    req: MediaDispatchRequest,
    deps: DispatcherDeps,
    caller_token_hash_value: str,
) -> DispatchResult:
    if os.getenv("HERMES_MEDIA_DISPATCH_ENABLED", "false").lower() != "true":
        raise DispatchError(503, "feature_disabled")

    safe_name = _safe_filename(req.filename)
    normalized_chat_id = _normalize_chat_id(req.chat_id)
    normalized_mime = _normalize_mimetype(req.mimetype)

    if not await _RATE_BUCKET.allow(normalized_chat_id):
        raise DispatchError(429, "rate_limit_exceeded")

    dup = await lookup_dedup(deps.pool, req.idempotency_key or "", normalized_chat_id)
    if dup:
        return DispatchResult(
            ok=True,
            log_id=str(dup["id"]),
            platform_msg_id=dup.get("platform_msg_id"),
            status="deduplicated",
            duration_ms=0,
        )

    started = time.monotonic()
    tmp_path = await resolve_source(req.source, req.type, normalized_mime)
    size_bytes = tmp_path.stat().st_size
    log_id = uuid.uuid4()

    async with deps.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO hermes_media_dispatch_log (
                id,
                platform,
                chat_id,
                media_type,
                filename,
                mimetype,
                size_bytes,
                source_kind,
                status,
                idempotency_key,
                caller_token_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, $10)
            """,
            log_id,
            req.platform,
            normalized_chat_id,
            req.type,
            safe_name,
            normalized_mime,
            size_bytes,
            req.source.kind,
            req.idempotency_key,
            caller_token_hash_value,
        )

    adapter = deps.get_whatsapp_platform()
    if adapter is None:
        raise DispatchError(503, "whatsapp_platform_unavailable")

    send_once = _resolve_sender(adapter, req, normalized_chat_id, tmp_path, safe_name)
    attempts = BRIDGE_RETRY + 1
    last_err: Optional[str] = None
    message_id: Optional[str] = None
    success = False

    for attempt in range(1, attempts + 1):
        try:
            send_result = await send_once()
            success, message_id, error = _coerce_send_result(send_result)
        except Exception as exc:
            success = False
            error = str(exc)
            message_id = None
        if success:
            break
        last_err = error or "unknown_error"
        if attempt < attempts:
            await asyncio.sleep(1.5 * attempt)

    duration_ms = int((time.monotonic() - started) * 1000)

    async with deps.pool.acquire() as conn:
        if success:
            await conn.execute(
                """
                UPDATE hermes_media_dispatch_log
                SET status = 'sent',
                    platform_msg_id = $2,
                    bridge_attempts = $3,
                    duration_ms = $4
                WHERE id = $1
                """,
                log_id,
                message_id,
                attempt,
                duration_ms,
            )
        else:
            await conn.execute(
                """
                UPDATE hermes_media_dispatch_log
                SET status = 'failed',
                    error = $2,
                    bridge_attempts = $3,
                    duration_ms = $4
                WHERE id = $1
                """,
                log_id,
                last_err or "unknown",
                attempts,
                duration_ms,
            )

    if not success:
        raise DispatchError(502, f"bridge_unreachable: {last_err}")

    return DispatchResult(
        ok=True,
        log_id=str(log_id),
        platform_msg_id=message_id,
        status="sent",
        duration_ms=duration_ms,
    )


__all__ = [
    "Base64Source",
    "DispatchError",
    "DispatchResult",
    "DispatcherDeps",
    "FilePathSource",
    "MAX_BYTES",
    "MediaDispatchRequest",
    "RATE_LIMIT_PER_MIN",
    "Source",
    "TMP_DIR",
    "TokenBucket",
    "UPLOAD_DIR",
    "UrlSource",
    "caller_token_hash",
    "dispatch",
    "lookup_dedup",
    "rate_limit_state",
    "resolve_source",
]
