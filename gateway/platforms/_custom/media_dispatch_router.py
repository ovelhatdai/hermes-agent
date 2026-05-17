"""SPEC-074 — HTTP router for media dispatch over the aiohttp API server."""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from pydantic import ValidationError

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - guarded by api_server requirements
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.platforms._custom.compat import ensure_media_dispatch_pool, get_whatsapp_platform
from gateway.platforms._custom.media_dispatch import (
    DispatchError,
    DispatcherDeps,
    MediaDispatchRequest,
    caller_token_hash,
    dispatch,
    rate_limit_state,
)

logger = logging.getLogger(__name__)


def _json_error(status: int, error: str, *, detail: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def _expected_token() -> str:
    return os.getenv("HERMES_GATEWAY_TOKEN", "").strip()


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def gateway_bearer_middleware(request: "web.Request", handler):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_error(401, "missing_bearer")

        token = auth_header.removeprefix("Bearer ").strip()
        expected = _expected_token()
        if not expected or not hmac.compare_digest(token, expected):
            return _json_error(401, "invalid_bearer")

        request["gateway_bearer"] = token
        return await handler(request)
else:  # pragma: no cover - imported only when aiohttp exists
    gateway_bearer_middleware = None  # type: ignore[assignment]


def _get_adapter(request: "web.Request") -> Any:
    adapter = request.config_dict.get("api_server_adapter")
    if adapter is None:
        raise DispatchError(503, "api_server_adapter_unavailable")
    return adapter


def _require_json_content_type(request: "web.Request") -> None:
    if request.content_type != "application/json":
        raise DispatchError(415, "unsupported_media_type")


async def _get_pool(adapter: Any) -> Any:
    try:
        return await ensure_media_dispatch_pool(adapter)
    except DispatchError:
        raise
    except Exception as exc:
        raise DispatchError(503, f"db_pool_unavailable: {exc}") from exc


async def handle_send_media(request: "web.Request") -> "web.Response":
    try:
        _require_json_content_type(request)
        body = await request.json()
        payload = MediaDispatchRequest.model_validate(body)

        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
        deps = DispatcherDeps(pool=pool, get_whatsapp_platform=lambda: get_whatsapp_platform(adapter))
        result = await dispatch(
            payload,
            deps,
            caller_token_hash(request["gateway_bearer"]),
        )
        return web.json_response(result.model_dump(), status=200)
    except ValidationError as exc:
        return _json_error(400, "invalid_request", detail=exc.errors())
    except DispatchError as exc:
        return _json_error(exc.http_code, exc.message)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("[media_dispatch_router] send-media failed: %s", exc)
        return _json_error(500, "internal_error")


async def handle_send_media_stats(request: "web.Request") -> "web.Response":
    try:
        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  count(*) as total,
                  count(*) filter (where status = 'sent')         as sent,
                  count(*) filter (where status = 'failed')       as failed,
                  count(*) filter (where status = 'deduplicated') as deduplicated,
                  coalesce(sum(size_bytes), 0)                    as bytes_total
                FROM hermes_media_dispatch_log
                WHERE created_at > NOW() - INTERVAL '24 hours'
                """
            )
            by_type = await conn.fetch(
                """
                SELECT media_type, count(*) as c
                FROM hermes_media_dispatch_log
                WHERE created_at > NOW() - INTERVAL '24 hours'
                GROUP BY media_type
                ORDER BY media_type ASC
                """
            )

        row = row or {
            "total": 0,
            "sent": 0,
            "failed": 0,
            "deduplicated": 0,
            "bytes_total": 0,
        }
        payload = {
            "last_24h": {
                "total": int(row["total"] or 0),
                "sent": int(row["sent"] or 0),
                "failed": int(row["failed"] or 0),
                "deduplicated": int(row["deduplicated"] or 0),
                "bytes_total": int(row["bytes_total"] or 0),
                "by_type": {item["media_type"]: int(item["c"] or 0) for item in by_type},
            },
            "rate_limit_state": rate_limit_state(),
        }
        return web.json_response(payload, status=200)
    except DispatchError as exc:
        return _json_error(exc.http_code, exc.message)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("[media_dispatch_router] stats failed: %s", exc)
        return _json_error(500, "internal_error")


def build_media_dispatch_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application(middlewares=[gateway_bearer_middleware])
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("/send-media", handle_send_media)
    subapp.router.add_get("/send-media/stats", handle_send_media_stats)
    return subapp


def mount_media_dispatch_subapp(parent_app: "web.Application", adapter: Any) -> "web.Application":
    subapp = build_media_dispatch_subapp(adapter)
    parent_app.add_subapp("/api/gateway", subapp)
    return subapp


__all__ = [
    "build_media_dispatch_subapp",
    "gateway_bearer_middleware",
    "handle_send_media",
    "handle_send_media_stats",
    "mount_media_dispatch_subapp",
]
