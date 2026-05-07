"""SPEC-141 Mentor Hub card routes.

Mounts POST /api/mentor-hub/contract-update on the Hermes API server and
proxies the heavy extraction/render/Base44 pipeline to the isolated
mentee-card-system service on 127.0.0.1:9178.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import aiohttp
from aiohttp import web


logger = logging.getLogger(__name__)

MENTEE_CARD_API_URL = os.getenv("MENTEE_CARD_API_URL", "http://127.0.0.1:9178").rstrip("/")
MENTOR_HUB_TOKEN = os.getenv("HERMES_MENTOR_HUB_TOKEN", "").strip()
DEDUP_WINDOW_SECONDS = int(os.getenv("MENTOR_HUB_DEDUP_SECONDS", "300"))
_DEDUP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _cleanup_dedup(now: float) -> None:
    expired = [key for key, (seen_at, _) in _DEDUP_CACHE.items() if now - seen_at > DEDUP_WINDOW_SECONDS]
    for key in expired:
        _DEDUP_CACHE.pop(key, None)


def _dedup_key(body: dict[str, Any]) -> str:
    raw = f"{body.get('mentee_id', '')}:{body.get('raw_message', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _authorized(request: web.Request) -> bool:
    if not MENTOR_HUB_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:] == MENTOR_HUB_TOKEN


async def handle_contract_update(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    missing = [field for field in ("mentee_id", "raw_message") if not body.get(field)]
    if missing:
        return web.json_response({"ok": False, "error": "missing_fields", "fields": missing}, status=400)

    now = time.time()
    _cleanup_dedup(now)
    key = _dedup_key(body)
    cached = _DEDUP_CACHE.get(key)
    if cached and now - cached[0] <= DEDUP_WINDOW_SECONDS:
        payload = dict(cached[1])
        payload["cached"] = True
        return web.json_response(payload, status=200)

    started = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
            async with session.post(f"{MENTEE_CARD_API_URL}/contract-update", json=body) as resp:
                payload = await resp.json(content_type=None)
                status = resp.status
    except Exception as exc:
        logger.exception("[mentor_hub] contract-update pipeline failed: %s", exc)
        return web.json_response({"ok": False, "error": "pipeline_unavailable"}, status=503)

    latency_ms = round((time.perf_counter() - started) * 1000)
    if status < 400:
        payload["latency_ms"] = latency_ms
        _DEDUP_CACHE[key] = (now, payload)
        logger.info(
            "[mentor_hub] contract-update ok mentee_id=%s period=%s latency_ms=%s",
            body.get("mentee_id"),
            payload.get("requested_period_yyyy_mm"),
            latency_ms,
        )
    else:
        logger.warning(
            "[mentor_hub] contract-update error status=%s mentee_id=%s payload=%s",
            status,
            body.get("mentee_id"),
            payload,
        )

    return web.json_response(payload, status=status)


def mount_mentor_hub_subapp(parent_app: web.Application, adapter: Any) -> None:
    parent_app.router.add_post("/api/mentor-hub/contract-update", handle_contract_update)
    logger.info("[custom_extensions] mentor-hub route mounted")
