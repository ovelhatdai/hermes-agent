"""SPEC-073 — HTTP router for DES SAC alert dispatch over the aiohttp API server."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - guarded by api_server requirements
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.platforms._custom.compat import get_whatsapp_platform
from gateway.platforms._custom.media_dispatch_router import gateway_bearer_middleware

logger = logging.getLogger(__name__)

_DEDUP_CACHE: dict[str, float] = {}
_DEDUP_TTL_S = 24 * 60 * 60
_SLO_DEDUP_CACHE: dict[str, float] = {}
_SLO_DEDUP_TTL_S = 15 * 60
_PHONE_DIGITS_RE = re.compile(r"\D+")


class DesSacAlert(BaseModel):
    cliente_nome: str = Field(min_length=1, max_length=200)
    telefone: str = Field(min_length=8, max_length=32)
    chatwoot_conv_url: str = Field(min_length=1, max_length=500)
    ultimo_atendimento_base44: str | None = Field(default=None, max_length=500)
    detectado_em: str = Field(min_length=1, max_length=64)
    chip_origem: str | None = Field(default="clara-des", max_length=120)


class SloAlertPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["warn", "crit"]
    chip: str = Field(min_length=1, max_length=160)
    metric: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_:-]+$")
    value_seconds: float = Field(ge=0)
    threshold_seconds: float = Field(ge=0)
    windows_violated: int = Field(ge=1, le=10_000)
    recipients: list[str] = Field(min_length=1, max_length=10)

    @field_validator("chip", "metric", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("recipients", mode="before")
    @classmethod
    def _normalize_recipients(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return value

        normalized: list[str] = []
        for item in value:
            if item is None:
                continue
            digits = _PHONE_DIGITS_RE.sub("", str(item))
            if digits:
                normalized.append(digits)
        return normalized


class Spec107SmokePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: str = Field(default="SPEC-107", max_length=40)
    status: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_:-]+$")
    message: str | None = Field(default=None, max_length=1000)
    valor_observado: float | None = None
    baseline: float | None = None
    tolerance_pct: float | None = Field(default=None, ge=0, le=100)
    duracao_ms: int | None = Field(default=None, ge=0)
    dry_run: bool = False

    @field_validator("spec", "status", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


def _json_error(status: int, error: str, *, detail: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def _normalize_phone_key(phone: str) -> str:
    normalized = _PHONE_DIGITS_RE.sub("", phone)
    return normalized or phone.strip()


def _hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:12]


def _prune_expired(now: float) -> None:
    expired = [phone for phone, ts in _DEDUP_CACHE.items() if (now - ts) >= _DEDUP_TTL_S]
    for phone in expired:
        _DEDUP_CACHE.pop(phone, None)


def _recently_alerted(phone: str) -> bool:
    now = time.time()
    _prune_expired(now)
    normalized = _normalize_phone_key(phone)
    ts = _DEDUP_CACHE.get(normalized)
    return bool(ts and (now - ts) < _DEDUP_TTL_S)


def _mark_alerted(phone: str) -> None:
    now = time.time()
    _prune_expired(now)
    _DEDUP_CACHE[_normalize_phone_key(phone)] = now


def _prune_slo_expired(now: float) -> None:
    expired = [key for key, ts in _SLO_DEDUP_CACHE.items() if (now - ts) >= _SLO_DEDUP_TTL_S]
    for key in expired:
        _SLO_DEDUP_CACHE.pop(key, None)


def _slo_dedup_key(payload: SloAlertPayload) -> str:
    return f"{payload.severity}:{payload.chip}:{payload.metric}:{payload.threshold_seconds}"


def _slo_recently_alerted(payload: SloAlertPayload) -> bool:
    now = time.time()
    _prune_slo_expired(now)
    ts = _SLO_DEDUP_CACHE.get(_slo_dedup_key(payload))
    return bool(ts and (now - ts) < _SLO_DEDUP_TTL_S)


def _mark_slo_alerted(payload: SloAlertPayload) -> None:
    now = time.time()
    _prune_slo_expired(now)
    _SLO_DEDUP_CACHE[_slo_dedup_key(payload)] = now


def _format_alert_text(payload: DesSacAlert) -> str:
    ultimo_atendimento = payload.ultimo_atendimento_base44 or "n/a"
    return (
        "ja-cliente detectado no DES\n\n"
        f"Cliente: {payload.cliente_nome}\n"
        f"Telefone: {payload.telefone}\n"
        f"Detectado: {payload.detectado_em}\n"
        f"Ultimo atendimento Base44: {ultimo_atendimento}\n"
        f"Chip origem: {payload.chip_origem or 'clara-des'}\n\n"
        f"Conv Chatwoot: {payload.chatwoot_conv_url}\n\n"
        "Clara DES nao respondeu porque o contato ja e cliente ativo. "
        "Favor assumir se for atendimento novo."
    )


def _format_slo_alert_text(payload: SloAlertPayload) -> str:
    severity = payload.severity.upper()
    return (
        f"⚠️ [{severity}] chip={payload.chip} "
        f"metric={payload.metric}={payload.value_seconds:g}s "
        f"threshold={payload.threshold_seconds:g}s "
        f"há {payload.windows_violated} janelas"
    )


def _format_spec107_value(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_spec107_smoke_text(payload: Spec107SmokePayload) -> str:
    return (
        "🚨 SPEC-107 smoke FAIL — "
        f"status={payload.status} "
        f"valor={_format_spec107_value(payload.valor_observado)} "
        f"baseline={_format_spec107_value(payload.baseline)}. "
        "Codex precisa investigar layout-change JFRS."
    )


def _expected_slo_secret() -> str:
    return (
        os.getenv("HERMES_SLO_ALERT_HMAC_SECRET", "")
        or os.getenv("HERMES_GATEWAY_TOKEN", "")
    ).strip()


def _valid_slo_signature(raw_body: bytes, received_signature: str | None) -> bool:
    secret = _expected_slo_secret()
    if not secret or not received_signature:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(received_signature.strip(), expected)


async def _dispatch_alert(adapter: Any, ashley_phone: str, payload: DesSacAlert) -> None:
    try:
        whatsapp = get_whatsapp_platform(adapter)
        if whatsapp is None:
            logger.error(
                "alerts/des-sac dispatch unavailable lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=missing_adapter",
                payload.telefone,
                _hash_phone(ashley_phone),
            )
            return

        chat_id = f"{ashley_phone}@s.whatsapp.net"
        result = await whatsapp.send(chat_id, _format_alert_text(payload))
        if getattr(result, "success", False):
            logger.info(
                "alerts/des-sac delivered lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=sent message_id=%s",
                payload.telefone,
                _hash_phone(ashley_phone),
                getattr(result, "message_id", None),
            )
            return

        logger.error(
            "alerts/des-sac dispatch_failed lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=failed error=%s",
            payload.telefone,
            _hash_phone(ashley_phone),
            getattr(result, "error", "unknown"),
        )
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception(
            "alerts/des-sac dispatch exception lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=exception err=%s",
            payload.telefone,
            _hash_phone(ashley_phone),
            exc,
        )


async def _dispatch_slo_alert(adapter: Any, payload: SloAlertPayload) -> None:
    try:
        whatsapp = get_whatsapp_platform(adapter)
        if whatsapp is None:
            logger.error(
                "alerts/slo dispatch unavailable chip=%s metric=%s resultado=missing_adapter",
                payload.chip,
                payload.metric,
            )
            return

        content = _format_slo_alert_text(payload)
        for recipient in payload.recipients:
            chat_id = f"{recipient}@s.whatsapp.net"
            result = await whatsapp.send(chat_id, content)
            if getattr(result, "success", False):
                logger.info(
                    "alerts/slo delivered chip=%s metric=%s severity=%s recipient_hash=%s resultado=sent message_id=%s",
                    payload.chip,
                    payload.metric,
                    payload.severity,
                    _hash_phone(recipient),
                    getattr(result, "message_id", None),
                )
                continue

            logger.error(
                "alerts/slo dispatch_failed chip=%s metric=%s severity=%s recipient_hash=%s resultado=failed error=%s",
                payload.chip,
                payload.metric,
                payload.severity,
                _hash_phone(recipient),
                getattr(result, "error", "unknown"),
            )
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception(
            "alerts/slo dispatch exception chip=%s metric=%s severity=%s resultado=exception err=%s",
            payload.chip,
            payload.metric,
            payload.severity,
            exc,
        )


async def _dispatch_spec107_smoke_alert(adapter: Any, payload: Spec107SmokePayload) -> None:
    recipients = ["5551991987972", "5551984213925"]

    try:
        whatsapp = get_whatsapp_platform(adapter)
        if whatsapp is None:
            logger.error(
                "alerts/spec107-smoke dispatch unavailable status=%s resultado=missing_adapter",
                payload.status,
            )
            return

        content = _format_spec107_smoke_text(payload)
        for recipient in recipients:
            chat_id = f"{recipient}@s.whatsapp.net"
            result = await whatsapp.send(chat_id, content)
            if getattr(result, "success", False):
                logger.info(
                    "alerts/spec107-smoke delivered status=%s recipient_hash=%s resultado=sent message_id=%s",
                    payload.status,
                    _hash_phone(recipient),
                    getattr(result, "message_id", None),
                )
                continue

            logger.error(
                "alerts/spec107-smoke dispatch_failed status=%s recipient_hash=%s resultado=failed error=%s",
                payload.status,
                _hash_phone(recipient),
                getattr(result, "error", "unknown"),
            )
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception(
            "alerts/spec107-smoke dispatch exception status=%s resultado=exception err=%s",
            payload.status,
            exc,
        )


def _get_adapter(request: "web.Request") -> Any:
    adapter = request.config_dict.get("api_server_adapter")
    if adapter is None:
        raise RuntimeError("api_server_adapter_unavailable")
    return adapter


async def handle_des_sac_alert(request: "web.Request") -> "web.Response":
    try:
        if request.content_type != "application/json":
            return _json_error(415, "unsupported_media_type")

        ashley_phone = os.getenv("ASHLEY_PHONE_E164", "").strip()
        if not ashley_phone:
            logger.error(
                "alerts/des-sac config_missing lead_phone=unknown ashley_phone_hash=missing canal=whatsapp-hermes resultado=config_missing"
            )
            return web.json_response({"ok": False, "status": "config_missing"}, status=200)

        payload = DesSacAlert.model_validate(await request.json())
        adapter = _get_adapter(request)

        if _recently_alerted(payload.telefone):
            logger.info(
                "alerts/des-sac dedup lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=dedup_skipped",
                payload.telefone,
                _hash_phone(ashley_phone),
            )
            return web.json_response({"ok": True, "status": "dedup_skipped"}, status=200)

        _mark_alerted(payload.telefone)
        logger.info(
            "alerts/des-sac queued lead_phone=%s ashley_phone_hash=%s canal=whatsapp-hermes resultado=queued",
            payload.telefone,
            _hash_phone(ashley_phone),
        )
        asyncio.create_task(_dispatch_alert(adapter, ashley_phone, payload))
        return web.json_response(
            {
                "ok": True,
                "status": "queued",
                "canal": "whatsapp-hermes",
                "destino": ashley_phone,
            },
            status=200,
        )
    except ValidationError as exc:
        return _json_error(400, "invalid_request", detail=exc.errors())
    except RuntimeError as exc:
        return _json_error(503, str(exc))
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("alerts/des-sac handler failed: %s", exc)
        return _json_error(500, "internal_error")


async def handle_slo_alert(request: "web.Request") -> "web.Response":
    try:
        if request.content_type != "application/json":
            return _json_error(415, "unsupported_media_type")

        raw_body = await request.read()
        if not _expected_slo_secret():
            logger.error("alerts/slo config_missing hmac_secret")
            return _json_error(503, "hmac_secret_missing")

        received_signature = request.headers.get("X-Signature")
        if not received_signature:
            return _json_error(401, "missing_signature")
        if not _valid_slo_signature(raw_body, received_signature):
            return _json_error(401, "invalid_signature")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error(400, "invalid_json")
        if not isinstance(body, dict):
            return _json_error(400, "invalid_request")

        payload = SloAlertPayload.model_validate(body)
        adapter = _get_adapter(request)

        if _slo_recently_alerted(payload):
            logger.info(
                "alerts/slo dedup chip=%s metric=%s severity=%s resultado=dedup_skipped",
                payload.chip,
                payload.metric,
                payload.severity,
            )
            return web.json_response({"ok": True, "status": "dedup_skipped"}, status=200)

        _mark_slo_alerted(payload)
        logger.info(
            "alerts/slo queued chip=%s metric=%s severity=%s recipients=%s resultado=queued",
            payload.chip,
            payload.metric,
            payload.severity,
            len(payload.recipients),
        )
        asyncio.create_task(_dispatch_slo_alert(adapter, payload))
        return web.json_response(
            {
                "ok": True,
                "status": "queued",
                "recipients": len(payload.recipients),
            },
            status=200,
        )
    except ValidationError as exc:
        return _json_error(400, "invalid_request", detail=exc.errors())
    except RuntimeError as exc:
        return _json_error(503, str(exc))
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("alerts/slo handler failed: %s", exc)
        return _json_error(500, "internal_error")


async def handle_spec107_smoke_alert(request: "web.Request") -> "web.Response":
    try:
        if request.content_type != "application/json":
            return _json_error(415, "unsupported_media_type")

        payload = Spec107SmokePayload.model_validate(await request.json())
        adapter = _get_adapter(request)

        if payload.dry_run:
            logger.info(
                "alerts/spec107-smoke dry_run status=%s valor=%s baseline=%s resultado=dry_run",
                payload.status,
                payload.valor_observado,
                payload.baseline,
            )
            return web.json_response({"ok": True, "status": "dry_run"}, status=200)

        logger.info(
            "alerts/spec107-smoke queued status=%s valor=%s baseline=%s resultado=queued",
            payload.status,
            payload.valor_observado,
            payload.baseline,
        )
        asyncio.create_task(_dispatch_spec107_smoke_alert(adapter, payload))
        return web.json_response(
            {
                "ok": True,
                "status": "queued",
                "recipients": 2,
            },
            status=200,
        )
    except ValidationError as exc:
        return _json_error(400, "invalid_request", detail=exc.errors())
    except RuntimeError as exc:
        return _json_error(503, str(exc))
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("alerts/spec107-smoke handler failed: %s", exc)
        return _json_error(500, "internal_error")


def build_alerts_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application(middlewares=[gateway_bearer_middleware])
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("/des-sac", handle_des_sac_alert)
    return subapp


def build_slo_alert_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application()
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("/slo-alert", handle_slo_alert)
    subapp.router.add_post("/spec107-smoke", handle_spec107_smoke_alert)
    return subapp


def mount_alerts_subapp(parent_app: "web.Application", adapter: Any) -> "web.Application":
    subapp = build_alerts_subapp(adapter)
    slo_subapp = build_slo_alert_subapp(adapter)
    parent_app.add_subapp("/api/gateway/alerts", subapp)
    parent_app.add_subapp("/api/webhook", slo_subapp)
    return subapp


__all__ = [
    "DesSacAlert",
    "SloAlertPayload",
    "Spec107SmokePayload",
    "_DEDUP_CACHE",
    "_SLO_DEDUP_CACHE",
    "build_alerts_subapp",
    "build_slo_alert_subapp",
    "handle_des_sac_alert",
    "handle_slo_alert",
    "handle_spec107_smoke_alert",
    "mount_alerts_subapp",
]
