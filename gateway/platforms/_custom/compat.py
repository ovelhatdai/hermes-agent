"""Compatibility helpers for Hermes v0.12 custom routers."""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _database_dsn() -> str:
    explicit = (
        os.getenv("HERMES_MEDIA_DISPATCH_DATABASE_URL", "")
        or os.getenv("DATABASE_URL", "")
    ).strip()
    if explicit:
        return explicit

    host = (os.getenv("HERMES_MEDIA_DISPATCH_DB_HOST") or os.getenv("PGHOST") or "127.0.0.1").strip()
    port = (os.getenv("HERMES_MEDIA_DISPATCH_DB_PORT") or os.getenv("PGPORT") or "5432").strip()
    database = (os.getenv("HERMES_MEDIA_DISPATCH_DB_NAME") or os.getenv("PGDATABASE") or "hermes").strip()
    user = (os.getenv("HERMES_MEDIA_DISPATCH_DB_USER") or os.getenv("PGUSER") or "evolution").strip()
    password = (os.getenv("HERMES_MEDIA_DISPATCH_DB_PASSWORD") or os.getenv("PGPASSWORD") or "").strip()

    if not host or not port or not database or not user:
        return ""
    if password:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return f"postgresql://{user}@{host}:{port}/{database}"


async def ensure_media_dispatch_pool(adapter: Any) -> Any:
    legacy = getattr(adapter, "_ensure_media_dispatch_pool", None)
    if callable(legacy):
        return await legacy()

    cached = getattr(adapter, "_custom_media_dispatch_pool", None)
    if cached is not None:
        return cached

    if asyncpg is None:
        raise RuntimeError("asyncpg not installed")
    dsn = _database_dsn()
    if not dsn:
        raise RuntimeError("database dsn not configured")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4, command_timeout=30)
    setattr(adapter, "_custom_media_dispatch_pool", pool)
    return pool


def get_whatsapp_platform(adapter: Any) -> Any:
    legacy = getattr(adapter, "_get_whatsapp_platform", None)
    if callable(legacy):
        return legacy()

    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        try:
            from gateway import run as gateway_run
            ref = getattr(gateway_run, "_gateway_runner_ref", lambda: None)
            runner = ref()
        except Exception as exc:  # pragma: no cover
            logger.debug("could not resolve gateway runner: %s", exc)
            runner = None

    adapters = getattr(runner, "adapters", {}) if runner is not None else {}
    try:
        from gateway.config import Platform
        whatsapp = adapters.get(Platform.WHATSAPP)
        if whatsapp is not None:
            return whatsapp
    except Exception:
        pass

    return adapters.get("whatsapp") or adapters.get("WHATSAPP")
