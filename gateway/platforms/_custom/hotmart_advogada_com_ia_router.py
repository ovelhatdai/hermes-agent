"""Hotmart webhook for SPEC-151 Advogada com IA.

Mounts POST /api/webhook/hotmart on the Hermes API server.
"""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import html
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

EVENTS_LOG = Path(os.getenv("HOTMART_EVENTS_LOG", "/var/log/hermes/hotmart-events.log"))
FAILURES_LOG = Path(os.getenv("HOTMART_FAILURES_LOG", "/var/log/hermes/hotmart-failures.log"))
STATE_PATH = Path(
    os.getenv(
        "HOTMART_ADVOGADA_STATE_PATH",
        "/var/lib/hermes/hotmart-advogada-com-ia-state.json",
    )
)
DEFAULT_TEMPLATE_PATH = Path(
    os.getenv(
        "ADVOGADA_COM_IA_EMAIL_TEMPLATE",
        "/opt/central-inteligencia/services/hotmart-webhook-advogada-com-ia/email-template-boas-vindas.html",
    )
)
MAX_GROUP_SIZE = int(os.getenv("ADVOGADA_COM_IA_MAX_GROUP_SIZE", "500"))


def _json_error(status: int, error: str, *, detail: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _log_event(kind: str, payload: dict[str, Any]) -> None:
    record = {"ts": _now(), "kind": kind, **payload}
    _append_json(EVENTS_LOG, record)


def _log_failure(kind: str, payload: dict[str, Any]) -> None:
    record = {"ts": _now(), "kind": kind, **payload}
    _append_json(FAILURES_LOG, record)
    _append_json(EVENTS_LOG, record)


def _env(name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        value = os.getenv(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _expected_token() -> str:
    return _env("HOTMART_WEBHOOK_TOKEN", "HOTMART_ADVOGADA_COM_IA_WEBHOOK_TOKEN")


def _expected_product_id() -> str:
    return _env("HOTMART_PRODUCT_ID_ADVOGADA_COM_IA", "ADVOGADA_COM_IA_HOTMART_PRODUCT_ID")


def _gateway_token() -> str:
    return _env("HERMES_GATEWAY_TOKEN")


def _groups() -> list[dict[str, str]]:
    values = [
        ("A", _env("ADVOGADA_COM_IA_GRUPOS_TURMA_A", "GRUPO_TURMA_A")),
        ("B", _env("ADVOGADA_COM_IA_GRUPOS_TURMA_B", "GRUPO_TURMA_B")),
        ("C", _env("ADVOGADA_COM_IA_GRUPOS_TURMA_C", "GRUPO_TURMA_C")),
    ]
    return [{"name": name, "id": jid} for name, jid in values if jid]


def _normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if len(digits) < 10:
        raise ValueError("buyer_phone_invalid")
    return digits if digits.startswith("55") else f"55{digits}"


def _first_name(name: str) -> str:
    cleaned = " ".join(name.split())
    return cleaned.split(" ", 1)[0] if cleaned else "advogada"


def _extract_event(payload: dict[str, Any]) -> str:
    return str(payload.get("event") or payload.get("event_type") or payload.get("eventType") or "").strip()


def _extract_product_id(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    return str(product.get("id") or product.get("ucode") or product.get("product_id") or "").strip()


def _extract_transaction(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    purchase = data.get("purchase") if isinstance(data.get("purchase"), dict) else {}
    candidate = (
        purchase.get("transaction")
        or purchase.get("transaction_id")
        or purchase.get("id")
        or payload.get("id")
        or payload.get("transaction")
    )
    return str(candidate or f"hotmart-{uuid.uuid4()}").strip()


def _extract_buyer(payload: dict[str, Any]) -> dict[str, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    buyer = data.get("buyer") if isinstance(data.get("buyer"), dict) else {}
    name = str(buyer.get("name") or buyer.get("full_name") or "").strip()
    email = str(buyer.get("email") or "").strip().lower()
    phone = _normalize_phone(buyer.get("phone") or buyer.get("phone_number") or buyer.get("cellphone"))
    if not name:
        raise ValueError("buyer_name_missing")
    if "@" not in email:
        raise ValueError("buyer_email_invalid")
    return {"name": name, "email": email, "phone": phone}


def _load_state_unlocked() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"next_index": 0, "transactions": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"next_index": 0, "transactions": {}}
    if not isinstance(data, dict):
        return {"next_index": 0, "transactions": {}}
    data.setdefault("next_index", 0)
    data.setdefault("transactions", {})
    return data


def _state_transaction_status(transaction_id: str) -> str | None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
        except Exception:
            state = {"next_index": 0, "transactions": {}}
        transactions = state.setdefault("transactions", {})
        current = transactions.get(transaction_id)
        if isinstance(current, dict):
            status = str(current.get("status") or "")
            if status in {"queued", "processing", "done"}:
                return status
        transactions[transaction_id] = {"status": "queued", "queued_at": _now()}
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
        fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def _mark_transaction(transaction_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
        except Exception:
            state = {"next_index": 0, "transactions": {}}
        transactions = state.setdefault("transactions", {})
        record = transactions.get(transaction_id)
        if not isinstance(record, dict):
            record = {}
        record.update(extra or {})
        record["status"] = status
        record[f"{status}_at"] = _now()
        transactions[transaction_id] = record
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
        fcntl.flock(handle, fcntl.LOCK_UN)


def _select_group(counts: dict[str, int]) -> dict[str, str]:
    configured = _groups()
    if len(configured) != 3:
        raise RuntimeError("groups_not_configured")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        state = _load_state_unlocked()
        start = int(state.get("next_index") or 0)
        selected = None
        for offset in range(len(configured)):
            candidate = configured[(start + offset) % len(configured)]
            if counts.get(candidate["id"], 0) < MAX_GROUP_SIZE:
                selected = candidate
                state["next_index"] = (start + offset + 1) % len(configured)
                break
        if selected is None:
            raise RuntimeError("all_groups_full")
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
        fcntl.flock(handle, fcntl.LOCK_UN)
    return selected


async def _fetch_group_counts() -> dict[str, int]:
    url = f"{_env('GROUP_BROADCASTER_URL', default='http://127.0.0.1:9120').rstrip('/')}/api/groups"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                payload = await response.json(content_type=None)
    except Exception as exc:
        _log_event("groups_count_unavailable", {"error": str(exc)})
        return {}

    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return {}
    counts: dict[str, int] = {}
    for item in groups:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("jid") or item.get("id") or "").strip()
        try:
            counts[jid] = int(item.get("members_count") or item.get("participants_count") or 0)
        except Exception:
            counts[jid] = 0
    return counts


async def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout_s: int = 12) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers or {}) as response:
            text = await response.text()
            try:
                body = json.loads(text) if text else {}
            except Exception:
                body = {"raw": text[:1000]}
            if response.status >= 400:
                raise RuntimeError(f"http_{response.status}:{body}")
            return body if isinstance(body, dict) else {"body": body}


async def _add_participant(group_id: str, buyer: dict[str, str]) -> dict[str, Any]:
    base_url = _env("GROUP_BROADCASTER_URL", default="http://127.0.0.1:9120").rstrip("/")
    token = _env("GROUP_BROADCASTER_TOKEN") or _gateway_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await _post_json(
        f"{base_url}/api/groups/add-participant",
        {"group_id": group_id, "phone": buyer["phone"], "name": buyer["name"]},
        headers=headers,
        timeout_s=20,
    )


def _fallback_template() -> str:
    return """
<p>Oi, {nome}.</p>
<p>Bem-vinda a Advogada com IA.</p>
<p>O primeiro passo e entrar na area de membros da Hotmart e assistir o Modulo 0. Sem pressa e sem tentar pular etapa: organiza o basico primeiro.</p>
<p>Grupo da tua turma: {grupo_link}</p>
<p>Suporte: {bia_whatsapp}</p>
"""


def _render_email_html(buyer: dict[str, str], group: dict[str, str]) -> str:
    template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8") if DEFAULT_TEMPLATE_PATH.exists() else _fallback_template()
    values = {
        "nome": html.escape(buyer["name"]),
        "primeiro_nome": html.escape(_first_name(buyer["name"])),
        "email": html.escape(buyer["email"]),
        "telefone": html.escape(buyer["phone"]),
        "turma": html.escape(group["name"]),
        "grupo_id": html.escape(group["id"]),
        "grupo_link": html.escape(_env(f"ADVOGADA_COM_IA_GRUPO_{group['name']}_LINK", default="link do grupo sera enviado no WhatsApp")),
        "hotmart_area_membros": html.escape(_env("ADVOGADA_COM_IA_HOTMART_AREA_MEMBROS_URL", default="https://consumer.hotmart.com")),
        "bia_whatsapp": html.escape(_env("ADVOGADA_COM_IA_BIA_WHATSAPP_URL", default="https://wa.me/555181980960")),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return rendered


async def _send_welcome_email(buyer: dict[str, str], group: dict[str, str], transaction_id: str) -> dict[str, Any]:
    base_url = _env("RESEND_PROXY_URL", default="http://127.0.0.1:9133").rstrip("/")
    subject = f"Bem-vinda a Advogada com IA, {_first_name(buyer['name'])}!"
    payload = {
        "from": _env("ADVOGADA_COM_IA_EMAIL_FROM", default="Daiane Elisa <daiane@advogadadaianeelisa.com.br>"),
        "to": buyer["email"],
        "subject": subject,
        "html": _render_email_html(buyer, group),
        "script_origem": "hotmart-webhook-advogada-com-ia",
        "template_id": "advogada-com-ia-welcome",
        "campanha_id": transaction_id,
    }
    return await _post_json(f"{base_url}/send", payload, timeout_s=20)


def _load_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("\"'")
    except Exception:
        return {}
    return result


async def _prime_bia(buyer: dict[str, str], group: dict[str, str], transaction_id: str) -> dict[str, Any]:
    bot_env = _load_env_file("/opt/bot-bridge-25k/.env")
    chatwoot_url = _env("BIA_CHATWOOT_URL", default=bot_env.get("CHATWOOT_URL", "https://chatwoot.advogando100k.com.br")).rstrip("/")
    account_id = _env("BIA_CHATWOOT_ACCOUNT_ID", default=bot_env.get("CHATWOOT_ACCOUNT_ID", "3"))
    inbox_id = int(_env("BIA_CHATWOOT_INBOX_ID", default="3"))
    token = _env("BIA_CHATWOOT_USER_TOKEN", default=bot_env.get("CHATWOOT_USER_TOKEN", ""))
    if not token:
        raise RuntimeError("bia_chatwoot_token_missing")

    headers = {"api_access_token": token, "Content-Type": "application/json"}
    phone_e164 = f"+{buyer['phone']}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=headers) as session:
        contact_payload = {
            "name": buyer["name"],
            "email": buyer["email"],
            "phone_number": phone_e164,
            "inbox_id": inbox_id,
            "additional_attributes": {
                "origem": "hotmart_advogada_com_ia",
                "hotmart_transaction": transaction_id,
                "turma": group["name"],
                "grupo_whatsapp": group["id"],
            },
        }
        async with session.post(f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts", json=contact_payload) as response:
            contact_body = await response.json(content_type=None)
            if response.status >= 400 and response.status != 422:
                raise RuntimeError(f"chatwoot_contact_http_{response.status}:{contact_body}")
        contact = contact_body.get("payload", {}).get("contact") if isinstance(contact_body, dict) else None
        if not isinstance(contact, dict):
            contact = contact_body.get("payload") if isinstance(contact_body, dict) else {}
        contact_id = contact.get("id") if isinstance(contact, dict) else None
        source_id = str(contact.get("source_id") or buyer["phone"]) if isinstance(contact, dict) else buyer["phone"]

        conv_payload = {"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id, "status": "pending"}
        async with session.post(f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations", json=conv_payload) as response:
            conv_body = await response.json(content_type=None)
            if response.status >= 400 and response.status != 422:
                raise RuntimeError(f"chatwoot_conversation_http_{response.status}:{conv_body}")
        conversation = conv_body.get("payload") if isinstance(conv_body, dict) else {}
        conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
        if not conversation_id:
            return {"ok": True, "contact_id": contact_id, "conversation_id": None, "note": "conversation_not_created"}

        note = (
            "[Advogada com IA] Nova aluna aprovada na Hotmart\n"
            f"Nome: {buyer['name']}\n"
            f"Email: {buyer['email']}\n"
            f"Telefone: {phone_e164}\n"
            f"Turma: {group['name']} ({group['id']})\n"
            f"Transacao: {transaction_id}\n"
            "Orientacao: se ela chamar no suporte, tratar como aluna nova e conferir acesso/grupo."
        )
        msg_payload = {"content": note, "message_type": "outgoing", "private": True}
        async with session.post(f"{chatwoot_url}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages", json=msg_payload) as response:
            msg_body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"chatwoot_note_http_{response.status}:{msg_body}")
    return {"ok": True, "contact_id": contact_id, "conversation_id": conversation_id}


async def _alert_vini(message: str) -> dict[str, Any]:
    url = _env("HERMES_BRIDGE_SEND_URL", default=_env("HERMES_BRIDGE_URL", default="http://127.0.0.1:3015").rstrip("/") + "/send")
    chat_id = _env("HOTMART_ALERT_CHAT_ID", "HERMES_ALERT_JID", default="5551991987972")
    try:
        return await _post_json(url, {"chatId": chat_id, "message": message}, timeout_s=12)
    except Exception as exc:
        _log_failure("alert_vini_failed", {"error": str(exc)})
        return {"ok": False, "error": str(exc)}


async def _process_purchase(payload: dict[str, Any], transaction_id: str, buyer: dict[str, str]) -> None:
    _mark_transaction(transaction_id, "processing")
    try:
        counts = await _fetch_group_counts()
        group = _select_group(counts)
        group_result = await _add_participant(group["id"], buyer)
        email_result = await _send_welcome_email(buyer, group, transaction_id)
        bia_result = await _prime_bia(buyer, group, transaction_id)
        _mark_transaction(
            transaction_id,
            "done",
            {
                "buyer_email": buyer["email"],
                "group": group,
                "group_result": group_result,
                "email_result": email_result,
                "bia_result": bia_result,
            },
        )
        _log_event("purchase_processed", {"transaction": transaction_id, "buyer_email": buyer["email"], "group": group})
    except Exception as exc:
        detail = {
            "transaction": transaction_id,
            "buyer": buyer,
            "error": str(exc),
            "event": payload,
        }
        _mark_transaction(transaction_id, "error", {"error": str(exc), "buyer_email": buyer.get("email")})
        _log_failure("purchase_processing_failed", detail)
        await _alert_vini(
            "FALHA WEBHOOK HOTMART Advogada com IA\n"
            f"Transacao: {transaction_id}\n"
            f"Aluna: {buyer.get('name')} ({buyer.get('phone')})\n"
            "Ver log: /var/log/hermes/hotmart-failures.log\n"
            "Fallback: adicionar manualmente no grupo e conferir e-mail de boas-vindas."
        )


async def handle_hotmart_webhook(request: "web.Request") -> "web.Response":
    started_at = time.monotonic()
    token = _expected_token()
    product_id = _expected_product_id()
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _json_error(401, "missing_bearer")
    if not token:
        return _json_error(503, "hotmart_webhook_token_missing")
    if not hmac.compare_digest(auth_header.removeprefix("Bearer ").strip(), token):
        return _json_error(401, "invalid_bearer")

    if request.content_type != "application/json":
        return _json_error(415, "unsupported_media_type")
    try:
        payload = await request.json()
    except Exception:
        return _json_error(400, "invalid_json")
    if not isinstance(payload, dict):
        return _json_error(400, "invalid_json")

    event = _extract_event(payload)
    transaction_id = _extract_transaction(payload)
    _log_event("webhook_received", {"event": event, "transaction": transaction_id, "product_id": _extract_product_id(payload)})

    if event != "PURCHASE_APPROVED":
        return web.json_response({"ok": True, "skip": True, "reason": "event_ignored"}, status=200)

    if not product_id:
        return _json_error(503, "hotmart_product_id_missing")
    received_product_id = _extract_product_id(payload)
    if received_product_id and received_product_id != product_id:
        return web.json_response({"ok": True, "skip": True, "reason": "product_ignored"}, status=200)
    if len(_groups()) != 3:
        return _json_error(503, "groups_not_configured")

    try:
        buyer = _extract_buyer(payload)
    except ValueError as exc:
        _log_failure("invalid_purchase_payload", {"transaction": transaction_id, "error": str(exc)})
        return _json_error(400, str(exc))

    duplicate_status = _state_transaction_status(transaction_id)
    if duplicate_status:
        return web.json_response(
            {"ok": True, "duplicate": True, "status": duplicate_status, "transaction": transaction_id},
            status=200,
        )

    asyncio.create_task(_process_purchase(payload, transaction_id, buyer))
    duration_ms = int((time.monotonic() - started_at) * 1000)
    return web.json_response({"ok": True, "accepted": True, "transaction": transaction_id, "duration_ms": duration_ms}, status=200)


def build_hotmart_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")
    subapp = web.Application()
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("", handle_hotmart_webhook)
    subapp.router.add_post("/", handle_hotmart_webhook)
    return subapp


def mount_hotmart_subapp(parent_app: "web.Application", adapter: Any) -> "web.Application":
    subapp = build_hotmart_subapp(adapter)
    parent_app.add_subapp("/api/webhook/hotmart", subapp)
    logger.info("[custom_extensions] hotmart Advogada com IA route mounted")
    return subapp


__all__ = ["build_hotmart_subapp", "handle_hotmart_webhook", "mount_hotmart_subapp"]
