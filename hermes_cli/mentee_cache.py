"""Redis cache helpers for SPEC-144 mentee snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - production dependency issue
    aioredis = None  # type: ignore[assignment]

log = logging.getLogger("uvicorn.error")

SNAPSHOT_VERSION = "v1"
CACHE_KEY_PREFIX = f"mentee:snapshot:{SNAPSHOT_VERSION}"
DEFAULT_CACHE_TTL_SECONDS = 60
DEFAULT_CACHE_TIMEOUT_SECONDS = 0.08

_redis_client: Any | None = None


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def snapshot_cache_ttl() -> int:
    raw_ttl = os.environ.get("SNAPSHOT_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))
    try:
        ttl = int(raw_ttl)
    except ValueError:
        log.warning("[CACHE-TTL-ERR] value=%s fallback=%s", raw_ttl, DEFAULT_CACHE_TTL_SECONDS)
        return DEFAULT_CACHE_TTL_SECONDS
    return max(1, ttl)


def _cache_timeout() -> float:
    raw_timeout = os.environ.get("SNAPSHOT_CACHE_TIMEOUT_SECONDS", str(DEFAULT_CACHE_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError:
        return DEFAULT_CACHE_TIMEOUT_SECONDS
    return max(0.01, timeout)


def cache_key(identifier: str) -> str:
    return f"{CACHE_KEY_PREFIX}:{identifier}"


async def get_redis() -> Any | None:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if aioredis is None:
        log.warning("[CACHE-REDIS-MISSING]")
        return None
    _redis_client = aioredis.from_url(_redis_url(), decode_responses=True)
    return _redis_client


async def cache_get(identifier: str) -> dict[str, Any] | None:
    try:
        redis_client = await get_redis()
        if redis_client is None:
            return None
        raw = await asyncio.wait_for(
            redis_client.get(cache_key(identifier)),
            timeout=_cache_timeout(),
        )
        if not raw:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception as exc:
        log.warning("[CACHE-GET-ERR] identifier=%s err=%s", identifier, type(exc).__name__)
        return None


async def cache_set(identifier: str, value: dict[str, Any], ttl: int | None = None) -> None:
    try:
        redis_client = await get_redis()
        if redis_client is None:
            return
        ttl_seconds = snapshot_cache_ttl() if ttl is None else max(1, int(ttl))
        payload = json.dumps(value, default=str, ensure_ascii=False)
        await asyncio.wait_for(
            redis_client.setex(cache_key(identifier), ttl_seconds, payload),
            timeout=_cache_timeout(),
        )
    except Exception as exc:
        log.warning("[CACHE-SET-ERR] identifier=%s err=%s", identifier, type(exc).__name__)


def cache_set_background(identifier: str, value: dict[str, Any], ttl: int | None = None) -> None:
    asyncio.create_task(cache_set(identifier, value, ttl=ttl))
