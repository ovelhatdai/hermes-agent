"""SPEC-127 Fase 1 — Hermes ouve postador-ads-100k.

Handler genérico para POST /api/eventos. Recebe eventos de sub-sistemas
internos (postador-ads-100k principalmente; outros podem ser adicionados
via match em ``body.source``).

Auth: whitelist por IP (loopback + meta-automation_internal subnet 172.20.0.0/16).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import asyncpg
from aiohttp import web

from gateway.platforms._custom.compat import get_whatsapp_platform

logger = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "HERMES_DB_URL", "postgresql://postgres@127.0.0.1:5432/hermes"
)

_ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.20.0.0/16"),
    ipaddress.ip_network("172.17.0.0/16"),
]

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_URL, min_size=1, max_size=3, command_timeout=10
        )
    return _pool


def _target_jids() -> list[str]:
    raw = os.getenv(
        "HERMES_SENSOR_TARGETS",
        "143658066157619@lid,233465882640526@lid",
    )
    targets: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if "@" not in value:
            value = f"{''.join(ch for ch in value if ch.isdigit())}@s.whatsapp.net"
        if value not in targets:
            targets.append(value)
    return targets


def _format_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "valor n/a"
    return f"R$ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _sensor_message(source: str, event: str, payload: dict[str, Any]) -> str | None:
    event_upper = event.upper()
    if source == "asaas":
        if event_upper not in {
            "PAYMENT_RECEIVED",
            "CONTRACT_SIGNED",
            "SALE_CLOSED",
            "PAYMENT_CONFIRMED",
        }:
            return None
        lead = payload.get("lead_name") or payload.get("customer_name") or payload.get("customerName") or "Cliente sem nome"
        sku = payload.get("sku") or payload.get("product") or "produto n/a"
        amount = payload.get("amount") or payload.get("value")
        action = {
            "PAYMENT_RECEIVED": "pagamento recebido",
            "PAYMENT_CONFIRMED": "pagamento confirmado",
            "CONTRACT_SIGNED": "contrato assinado",
            "SALE_CLOSED": "venda fechada",
        }.get(event_upper, event)
        return "\n".join([
            "Hermes Sensor - Asaas",
            f"{action}: {lead}",
            f"Produto: {sku}",
            f"Valor: {_format_money(amount)}",
            f"Payment ID: {payload.get('payment_id') or payload.get('paymentId') or 'n/a'}",
        ])

    if source == "bebela":
        count = int(payload.get("count") or payload.get("events_count") or 0)
        severity = str(payload.get("severity") or "").lower()
        if event != "churn_alert" or (severity not in {"high", "critical"} and count < 1):
            return None
        groups = payload.get("groups") or []
        if isinstance(groups, list):
            group_lines = [f"- {g}" for g in groups[:6]]
        else:
            group_lines = [str(groups)]
        return "\n".join([
            "Hermes Sensor - Bebela",
            f"{count or len(group_lines)} grupo(s) cairam para cold/frozen.",
            *group_lines,
            "Sugestao: avaliar reengajamento sem jogar alerta bruto no grupo.",
        ])

    if source == "calendar":
        if event != "upcoming_event":
            return None
        title = payload.get("summary") or payload.get("title") or "Evento sem titulo"
        starts_at = payload.get("starts_at") or payload.get("start") or "horario n/a"
        calendar = payload.get("calendar") or payload.get("calendar_source") or "agenda n/a"
        meet = payload.get("meet_url") or payload.get("google_meet_url") or payload.get("meet_link")
        lines = [
            "Hermes Sensor - Calendar",
            f"Em ~30 min: {title}",
            f"Agenda: {calendar}",
            f"Inicio: {starts_at}",
        ]
        if meet:
            lines.append(f"Meet: {meet}")
        return "\n".join(lines)

    return None


async def _notify_sensor(adapter: Any, source: str, event: str, payload: dict[str, Any]) -> bool:
    message = _sensor_message(source, event, payload)
    if not message:
        return False
    whatsapp = get_whatsapp_platform(adapter)
    if whatsapp is None:
        logger.warning("[eventos] whatsapp platform unavailable source=%s event=%s", source, event)
        return False

    delivered = False
    for target in _target_jids():
        result = await whatsapp.send(target, message)
        if getattr(result, "success", False):
            delivered = True
        else:
            logger.warning(
                "[eventos] sensor notify failed source=%s event=%s target=%s error=%s",
                source,
                event,
                target,
                getattr(result, "error", "unknown"),
            )
    return delivered


async def emit_sensor_event(
    adapter: Any,
    source: str,
    event: str,
    payload: dict[str, Any],
    *,
    timestamp: datetime | None = None,
    src_ip: str = "127.0.0.1",
) -> dict[str, Any]:
    ts = timestamp or datetime.now(UTC)
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    pool = await _get_pool()

    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO hermes_postador_eventos (
                source, event, payload, ts, src_ip, received_at
            ) VALUES ($1, $2, $3::jsonb, $4, $5::inet, NOW())
            """,
            source,
            event,
            payload_json,
            ts,
            src_ip,
        )

        if source == "postador-ads-100k":
            await _on_postador_ads_event(con, event, payload)

    notified = await _notify_sensor(adapter, source, event, payload)
    logger.info("[eventos] received source=%s event=%s ip=%s notified=%s", source, event, src_ip, notified)
    return {"ok": True, "source": source, "event": event, "notified": notified}


def _ip_allowed(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED_NETWORKS)


async def handle_evento_generic(request: web.Request) -> web.Response:
    src_ip = request.remote
    if not src_ip:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        src_ip = peer[0] if peer else ""
    if not _ip_allowed(src_ip):
        return web.json_response(
            {"error": "forbidden", "reason": "ip_not_allowed", "ip": src_ip},
            status=403,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    required = ("source", "event", "payload", "timestamp")
    missing = [k for k in required if k not in body]
    if missing:
        return web.json_response(
            {"error": "missing_fields", "fields": missing}, status=400
        )

    source = str(body["source"])
    event = str(body["event"])
    payload = body.get("payload") or {}
    ts_str = str(body["timestamp"])
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.now(UTC)

    adapter = request.app.get("api_server_adapter")
    if adapter is None:
        return web.json_response({"error": "api_server_adapter_unavailable"}, status=500)
    result = await emit_sensor_event(adapter, source, event, payload, timestamp=ts, src_ip=src_ip)
    return web.json_response(result)


async def _on_postador_ads_event(
    con: asyncpg.Connection, event: str, payload: dict
) -> None:
    ad_id = payload.get("ad_id")
    if not ad_id:
        logger.warning("[eventos] postador-ads sem ad_id payload=%s", payload)
        return

    if event == "ad_created":
        await con.execute(
            """
            INSERT INTO hermes_ads_eventos (
                ad_id, campaign_id, adset_id, mentorada, act_id,
                niche, schedule_at_iso, status, last_event_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'created', NOW())
            ON CONFLICT (ad_id) DO UPDATE SET
                status = 'created',
                last_event_at = NOW()
            """,
            str(ad_id),
            payload.get("campaign_id"),
            payload.get("adset_id"),
            payload.get("mentorada"),
            payload.get("act_id"),
            payload.get("niche"),
            _parse_ts(payload.get("schedule_at_iso")),
        )
    elif event == "ads_paused":
        await con.execute(
            "UPDATE hermes_ads_eventos SET status='paused', last_event_at=NOW() WHERE ad_id=$1",
            str(ad_id),
        )
    elif event == "ads_duplicated":
        await con.execute(
            "UPDATE hermes_ads_eventos SET status='duplicated', last_event_at=NOW() WHERE ad_id=$1",
            str(ad_id),
        )


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def mount_eventos_subapp(parent_app, adapter):
    parent_app["api_server_adapter"] = adapter
    parent_app.router.add_post("/api/eventos", handle_evento_generic)
    logger.info("[custom_extensions] eventos route mounted: POST /api/eventos")
