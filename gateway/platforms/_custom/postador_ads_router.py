"""SPEC-127 Fase 2 — Hermes opera postador-ads-100k.

Expoe 5 endpoints HTTP no Hermes que delegam pro backend FastAPI
em https://monitor.advogando100k.com.br/mentoradas/api/agent/*.

Auth interno: whitelist por IP (loopback + bridges Docker).
Auth backend: token decifrado de hermes.secrets ('postador_ads_100k').

Endpoints expostos no Hermes:
    POST /api/postador-ads/listar-mentoradas
    POST /api/postador-ads/listar             {act_id, status?}
    POST /api/postador-ads/criar              {mentorada_nome, page_id, whatsapp_number, niche, daily_budget_brl, schedule_at_iso}
    POST /api/postador-ads/pausar             {ad_id?, adset_id?, campaign_id?, dry_run?}
    POST /api/postador-ads/duplicar           {source_ad_id, schedule_at_isos[]}

Guardrails:
    - REGRA #1: tenant=mentoria_100k (hardcoded — backend sirve so 100K)
    - dry_run=true em pausar quando enviado
    - max 4 clones por chamada de duplicar
    - rate_limit por minuto (5 criações)
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from typing import Any, Optional

import aiohttp
import asyncpg
from aiohttp import web

logger = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "HERMES_DB_URL", "postgresql://postgres@127.0.0.1:5432/hermes"
)
MASTER_KEY_PATH = os.environ.get(
    "HERMES_MASTER_KEY_PATH", "/etc/hermes/master.key"
)
POSTADOR_BASE = os.environ.get(
    "POSTADOR_AGENT_BASE",
    "https://monitor.advogando100k.com.br/mentoradas/api/agent",
).rstrip("/")
TENANT_SCOPE = "mentoria_100k"
MAX_CLONES_PER_CALL = 4
RATE_LIMIT_CRIAR_PER_MIN = 5

_ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.20.0.0/16"),
    ipaddress.ip_network("172.17.0.0/16"),
]

_pool: asyncpg.Pool | None = None
_master_key: str | None = None
_token_cache: tuple[str, float] | None = None  # (token, expires_at)
_TOKEN_TTL = 60.0  # cache 60s
_rate_window: list[float] = []  # timestamps das ultimas criações


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_URL, min_size=1, max_size=3, command_timeout=10
        )
    return _pool


def _load_master_key() -> str:
    global _master_key
    if _master_key is None:
        with open(MASTER_KEY_PATH, "r", encoding="utf-8") as f:
            _master_key = f.read().strip()
    return _master_key


async def _get_postador_token() -> str:
    """Decifra token from hermes.secrets, cacheado 60s."""
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache[1] > now:
        return _token_cache[0]
    pool = await _get_pool()
    mk = _load_master_key()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            SELECT pgp_sym_decrypt(secret_encrypted, $1) AS token
            FROM hermes.secrets
            WHERE name = 'postador_ads_100k'
              AND scope = $2
              AND active = TRUE
            ORDER BY rotated_at DESC
            LIMIT 1
            """,
            mk, TENANT_SCOPE,
        )
    if not row or not row["token"]:
        raise RuntimeError("postador_ads_100k token not found in hermes.secrets")
    _token_cache = (row["token"], now + _TOKEN_TTL)
    return row["token"]


def _ip_allowed(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED_NETWORKS)


def _check_ip(request: web.Request) -> Optional[web.Response]:
    src_ip = request.remote
    if not src_ip:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        src_ip = peer[0] if peer else ""
    if not _ip_allowed(src_ip):
        return web.json_response(
            {"error": "forbidden", "ip": src_ip}, status=403
        )
    return None


async def _backend_get(path: str, params: dict | None = None) -> tuple[int, Any]:
    token = await _get_postador_token()
    url = f"{POSTADOR_BASE}{path}"
    headers = {"X-Agent-Token": token}
    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            try:
                body = await r.json()
            except Exception:
                body = {"raw": await r.text()}
            return r.status, body


async def _backend_post(path: str, json_body: dict) -> tuple[int, Any]:
    token = await _get_postador_token()
    url = f"{POSTADOR_BASE}{path}"
    headers = {"X-Agent-Token": token, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url, json=json_body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            try:
                body = await r.json()
            except Exception:
                body = {"raw": await r.text()}
            return r.status, body


# ---------- handlers ----------
async def _listar_mentoradas(request: web.Request) -> web.Response:
    if (rsp := _check_ip(request)) is not None:
        return rsp
    status, body = await _backend_get("/mentoradas")
    return web.json_response(body, status=status)


async def _listar_ads(request: web.Request) -> web.Response:
    if (rsp := _check_ip(request)) is not None:
        return rsp
    try:
        body_in = await request.json()
    except Exception:
        body_in = {}
    act_id = body_in.get("act_id")
    if not act_id:
        return web.json_response({"error": "act_id required"}, status=400)
    status_filter = body_in.get("status", "ACTIVE")
    status, body = await _backend_get(
        f"/ads/list/{act_id}", params={"status": status_filter}
    )
    return web.json_response(body, status=status)


async def _criar_ad(request: web.Request) -> web.Response:
    global _rate_window
    if (rsp := _check_ip(request)) is not None:
        return rsp
    try:
        body_in = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    # rate limit (5/min)
    now = time.time()
    _rate_window = [t for t in _rate_window if t > now - 60]
    if len(_rate_window) >= RATE_LIMIT_CRIAR_PER_MIN:
        return web.json_response(
            {"error": "rate_limited", "limit_per_min": RATE_LIMIT_CRIAR_PER_MIN},
            status=429,
        )

    # Hermes exige só act_id+niche; backend resolve page_id/whatsapp via mentorada.default_page_id e mentorada.whatsapp
    required = ("act_id", "niche")
    missing = [k for k in required if not body_in.get(k)]
    if missing:
        return web.json_response(
            {"error": "missing_fields", "fields": missing}, status=400
        )

    payload = {
        "act_id": body_in["act_id"],
        "niche": body_in["niche"],
        "daily_budget_brl": int(body_in.get("daily_budget_brl", 50)),
        "name_prefix": body_in.get("name_prefix", body_in.get("mentorada_nome", "")),
    }
    if body_in.get("page_id"):
        payload["page_id"] = body_in["page_id"]
    if body_in.get("whatsapp_number"):
        payload["whatsapp_number"] = body_in["whatsapp_number"]
    if body_in.get("schedule_at_iso"):
        payload["schedule_at_iso"] = body_in["schedule_at_iso"]
    if body_in.get("welcome_message"):
        payload["welcome_message"] = body_in["welcome_message"]

    status, body = await _backend_post("/ads/create", payload)
    if 200 <= status < 300:
        _rate_window.append(now)
        logger.info(
            "[postador-ads] criar OK ad_id=%s mentorada=%s niche=%s",
            (body or {}).get("ad_id"),
            payload.get("name_prefix"),
            payload.get("niche"),
        )
    else:
        logger.warning(
            "[postador-ads] criar FAIL status=%s body=%s",
            status, str(body)[:200],
        )
    return web.json_response(body, status=status)


async def _pausar(request: web.Request) -> web.Response:
    if (rsp := _check_ip(request)) is not None:
        return rsp
    try:
        body_in = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    has_id = any(body_in.get(k) for k in ("ad_id", "adset_id", "campaign_id"))
    if not has_id:
        return web.json_response(
            {"error": "missing_fields", "needs_one_of": ["ad_id", "adset_id", "campaign_id"]},
            status=400,
        )

    if body_in.get("dry_run"):
        return web.json_response(
            {"dry_run": True, "would_pause": {
                "ad_id": body_in.get("ad_id"),
                "adset_id": body_in.get("adset_id"),
                "campaign_id": body_in.get("campaign_id"),
            }},
            status=200,
        )

    payload = {k: body_in[k] for k in ("ad_id", "adset_id", "campaign_id") if body_in.get(k)}
    status, body = await _backend_post("/ads/pause", payload)
    return web.json_response(body, status=status)


async def _duplicar(request: web.Request) -> web.Response:
    if (rsp := _check_ip(request)) is not None:
        return rsp
    try:
        body_in = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    src = body_in.get("source_ad_id")
    schedules = body_in.get("schedule_at_isos") or []
    if not src or not isinstance(schedules, list) or not schedules:
        return web.json_response(
            {"error": "missing_fields", "needs": ["source_ad_id", "schedule_at_isos"]},
            status=400,
        )
    if len(schedules) > MAX_CLONES_PER_CALL:
        return web.json_response(
            {"error": "too_many_clones", "max": MAX_CLONES_PER_CALL, "got": len(schedules)},
            status=400,
        )

    status, body = await _backend_post(
        "/ads/duplicate-scheduled",
        {"source_ad_id": src, "schedule_at_isos": schedules},
    )
    return web.json_response(body, status=status)


async def _health(request: web.Request) -> web.Response:
    if (rsp := _check_ip(request)) is not None:
        return rsp
    try:
        token = await _get_postador_token()
        status, body = await _backend_get("/health")
        return web.json_response({
            "ok": True,
            "tenant": TENANT_SCOPE,
            "backend_status": status,
            "backend": body,
            "token_cached": _token_cache is not None and _token_cache[1] > time.time(),
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:300]}, status=500)


# ---------- mount ----------
def mount_postador_ads_subapp(parent_app: web.Application, adapter: Any) -> None:
    p = parent_app
    p.router.add_get("/api/postador-ads/health", _health)
    p.router.add_post("/api/postador-ads/listar-mentoradas", _listar_mentoradas)
    p.router.add_post("/api/postador-ads/listar", _listar_ads)
    p.router.add_post("/api/postador-ads/criar", _criar_ad)
    p.router.add_post("/api/postador-ads/pausar", _pausar)
    p.router.add_post("/api/postador-ads/duplicar", _duplicar)
    logger.info("[custom_extensions] postador-ads routes mounted under /api/postador-ads/*")
