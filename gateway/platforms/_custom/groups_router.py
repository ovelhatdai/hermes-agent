"""SPEC-081 task 06 — groups broadcast router for Hermes gateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
from gateway.platforms._custom.compat import ensure_media_dispatch_pool
from gateway.platforms._custom.evolution_groups import EvolutionGroupsClient

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger(__name__)

MAX_GROUP_JIDS = 100
MAX_REMOVE_GROUP_JIDS = 10
ALLOWED_MEDIA_TYPES = {"image", "video", "audio", "document"}
HITL_TOKEN_TTL_SECONDS = max(60, int(os.getenv("HITL_TOKEN_TTL_SEC", "300")))
REMOVE_SPACING_SECONDS = max(2, int(os.getenv("HERMES_GROUP_REMOVE_SPACING_S", "2")))
REMOVE_RETRY_ATTEMPTS = max(1, int(os.getenv("HERMES_GROUP_REMOVE_RETRY_ATTEMPTS", "3")))
REMOVE_RETRY_BASE_SECONDS = max(1, int(os.getenv("HERMES_GROUP_REMOVE_RETRY_BASE_S", "1")))


def _json_error(status: int, error: str, *, detail: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def _expected_gateway_token() -> str:
    return os.getenv("HERMES_GATEWAY_TOKEN", "").strip()


def _group_broadcaster_url() -> str:
    return (os.getenv("GROUP_BROADCASTER_URL") or "http://127.0.0.1:9120").rstrip("/")


def _group_broadcaster_token() -> str:
    token = os.getenv("GROUP_BROADCASTER_TOKEN", "").strip()
    if token:
        return token
    # Fallback keeps same trust boundary when dedicated token is not provided yet.
    return _expected_gateway_token()


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def gateway_bearer_middleware(request: "web.Request", handler):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_error(401, "missing_bearer")

        token = auth_header.removeprefix("Bearer ").strip()
        expected = _expected_gateway_token()
        if not expected or not hmac.compare_digest(token, expected):
            return _json_error(401, "invalid_bearer")

        request["gateway_bearer"] = token
        return await handler(request)
else:  # pragma: no cover
    gateway_bearer_middleware = None  # type: ignore[assignment]


def _get_adapter(request: "web.Request") -> Any:
    adapter = request.config_dict.get("api_server_adapter")
    if adapter is None:
        raise RuntimeError("api_server_adapter_unavailable")
    return adapter


async def _get_pool(adapter: Any) -> Any:
    try:
        return await ensure_media_dispatch_pool(adapter)
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"db_pool_unavailable: {exc}") from exc


def _require_json_content_type(request: "web.Request") -> None:
    if request.content_type != "application/json":
        raise ValueError("unsupported_media_type")


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except Exception:
        return False
    return parsed.version == 4


def _validate_group_jids(payload: dict[str, Any]) -> list[str]:
    if payload.get("all") is True:
        raise ValueError("all_wildcard_not_allowed")

    group_jids = payload.get("groupJids")
    if not isinstance(group_jids, list):
        raise ValueError("groupJids_must_be_list")

    cleaned: list[str] = []
    for item in group_jids:
        if not isinstance(item, str):
            raise ValueError("groupJids_item_must_be_string")
        jid = item.strip()
        if not jid or jid == "*" or jid.lower() == "all":
            raise ValueError("wildcard_not_allowed")
        cleaned.append(jid)

    if len(cleaned) == 0:
        raise ValueError("groupJids_must_not_be_empty")
    if len(cleaned) > MAX_GROUP_JIDS:
        raise ValueError("groupJids_exceeds_max_100")
    return cleaned


def _validate_message(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    return message.strip()


def _validate_media(payload: dict[str, Any]) -> dict[str, Any]:
    media = payload.get("media")
    if not isinstance(media, dict):
        raise ValueError("media_required")

    url = media.get("url")
    media_type = media.get("type")
    caption = media.get("caption")

    if not isinstance(url, str) or not url.strip():
        raise ValueError("media_url_required")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("media_url_must_be_http")

    if not isinstance(media_type, str) or media_type.strip().lower() not in ALLOWED_MEDIA_TYPES:
        raise ValueError("media_type_invalid")

    result: dict[str, Any] = {
        "url": url.strip(),
        "type": media_type.strip().lower(),
    }
    if isinstance(caption, str) and caption.strip():
        result["caption"] = caption.strip()
    return result


def _normalize_phone(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("phone_required")
    digits = re.sub(r"\D+", "", raw)
    if len(digits) < 10:
        raise ValueError("phone_invalid")
    if not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


def _normalize_jid_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw)
    if len(digits) >= 11 and not digits.startswith("55"):
        digits = f"55{digits}"
    return digits


def _extract_groups_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = payload.get("groups")
    if isinstance(groups, list):
        return [g for g in groups if isinstance(g, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [g for g in data if isinstance(g, dict)]
    return []


def _extract_participants(group: dict[str, Any]) -> list[dict[str, Any]]:
    raw = group.get("participants")
    if not isinstance(raw, list):
        return []

    participants: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            participants.append({"id": item})
            continue
        if isinstance(item, dict):
            participants.append(item)
    return participants


def _participant_matches_phone(participant: dict[str, Any], phone: str) -> tuple[bool, bool]:
    participant_id = str(
        participant.get("id")
        or participant.get("jid")
        or participant.get("number")
        or "",
    ).strip()
    if not participant_id:
        return False, False

    normalized = _normalize_jid_phone(participant_id)
    is_match = normalized == phone
    admin_value = str(participant.get("admin") or participant.get("role") or "").strip().lower()
    is_admin = admin_value in {"admin", "superadmin", "super_admin", "owner", "true", "1"}
    if not is_admin and isinstance(participant.get("isAdmin"), bool):
        is_admin = bool(participant.get("isAdmin"))
    return is_match, is_admin


def _validate_hitl_token(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("hitl_token_required")
    token = raw.strip().lower()
    if not _is_uuid4(token):
        raise ValueError("hitl_token_invalid")
    return token


def _validate_reason(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("reason_required")
    return raw.strip()


def _validate_remove_group_jids(payload: dict[str, Any]) -> list[str]:
    if payload.get("all") is True:
        raise ValueError("all_wildcard_not_allowed")
    raw = payload.get("groupJids")
    if not isinstance(raw, list):
        raise ValueError("groupJids_must_be_list")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("groupJids_item_must_be_string")
        jid = item.strip()
        if not jid or jid == "*" or jid.lower() == "all":
            raise ValueError("wildcard_not_allowed")
        if jid not in seen:
            cleaned.append(jid)
            seen.add(jid)
    if not cleaned:
        raise ValueError("groupJids_must_not_be_empty")
    if len(cleaned) > MAX_REMOVE_GROUP_JIDS:
        raise ValueError("groupJids_exceeds_max_10")
    return cleaned


async def _find_existing_idempotency(
    conn: Any,
    *,
    op_type: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT id, status, payload_out, error
        FROM hermes_group_ops_log
        WHERE op_type = $1 AND idempotency_key = $2::uuid
        ORDER BY id DESC
        LIMIT 1
        """,
        op_type,
        idempotency_key,
    )
    if row is None:
        return None

    payload_out = row["payload_out"]
    if isinstance(payload_out, str):
        try:
            payload_out = json.loads(payload_out)
        except Exception:
            payload_out = {"status": row["status"]}
    elif not isinstance(payload_out, dict):
        payload_out = {"status": row["status"]}

    response = dict(payload_out)
    response.setdefault("ok", row["status"] in {"queued", "ok", "partial"})
    response["idempotent_replay"] = True
    response["audit_id"] = int(row["id"])
    if row["error"]:
        response.setdefault("error", row["error"])
    return response


async def _insert_audit_row(
    conn: Any,
    *,
    op_type: str,
    idempotency_key: str,
    payload_in: dict[str, Any],
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO hermes_group_ops_log (op_type, requestor, idempotency_key, payload_in, status)
        VALUES ($1, 'hermes', $2::uuid, $3::jsonb, 'queued')
        RETURNING id
        """,
        op_type,
        idempotency_key,
        json.dumps(payload_in),
    )
    return int(row["id"])


async def _finish_audit_row(
    conn: Any,
    *,
    audit_id: int,
    status: str,
    payload_out: dict[str, Any] | None,
    error: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE hermes_group_ops_log
        SET status = $2,
            payload_out = $3::jsonb,
            error = $4,
            finished_at = NOW()
        WHERE id = $1
        """,
        audit_id,
        status,
        json.dumps(payload_out or {}),
        error,
    )


async def _dispatch_group_broadcast(
    *,
    path: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any], str | None]:
    url = f"{_group_broadcaster_url()}{path}"
    headers = {"Content-Type": "application/json"}
    token = _group_broadcaster_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                try:
                    parsed = json.loads(text) if text else {}
                except Exception:
                    parsed = {"raw": text[:1000]}
                return resp.status, parsed if isinstance(parsed, dict) else {"raw": parsed}, None
    except Exception as exc:
        return 503, {}, str(exc)


async def _fetch_groups_from_group_broadcaster() -> tuple[int, dict[str, Any], str | None]:
    url = f"{_group_broadcaster_url()}/api/groups"
    headers = {}
    token = _group_broadcaster_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                try:
                    parsed = json.loads(text) if text else {}
                except Exception:
                    parsed = {"raw": text[:1000]}
                return response.status, parsed if isinstance(parsed, dict) else {"raw": parsed}, None
    except Exception as exc:
        return 503, {}, str(exc)


async def _fetch_group_broadcaster_json(path: str) -> tuple[int, dict[str, Any], str | None]:
    url = f"{_group_broadcaster_url()}{path}"
    headers = {}
    token = _group_broadcaster_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                try:
                    parsed = json.loads(text) if text else {}
                except Exception:
                    parsed = {"raw": text[:1000]}
                return response.status, parsed if isinstance(parsed, dict) else {"raw": parsed}, None
    except Exception as exc:
        return 503, {}, str(exc)


async def _fetch_group_tags_by_jid() -> dict[str, list[str]]:
    status, tags_payload, error = await _fetch_group_broadcaster_json("/api/tags")
    if error is not None or status >= 400:
        logger.warning("Group tags unavailable from broadcaster: status=%s error=%s", status, error)
        return {}

    raw_tags = tags_payload.get("tags") if isinstance(tags_payload, dict) else None
    if not isinstance(raw_tags, list):
        return {}

    tags_by_jid: dict[str, list[str]] = {}
    for tag_row in raw_tags:
        if not isinstance(tag_row, dict):
            continue
        tag_id = tag_row.get("id")
        tag_name_raw = tag_row.get("name")
        if tag_id is None or not isinstance(tag_name_raw, str) or not tag_name_raw.strip():
            continue
        tag_name = tag_name_raw.strip().lower()
        groups_status, groups_payload, groups_error = await _fetch_group_broadcaster_json(f"/api/tags/{tag_id}/groups")
        if groups_error is not None or groups_status >= 400:
            logger.warning(
                "Group tag assignments unavailable: tag_id=%s status=%s error=%s",
                tag_id,
                groups_status,
                groups_error,
            )
            continue
        raw_groups = groups_payload.get("groups") if isinstance(groups_payload, dict) else None
        if not isinstance(raw_groups, list):
            continue
        for group_row in raw_groups:
            if not isinstance(group_row, dict):
                continue
            jid = str(group_row.get("group_jid") or group_row.get("jid") or "").strip()
            if not jid:
                continue
            tags_by_jid.setdefault(jid, [])
            if tag_name not in tags_by_jid[jid]:
                tags_by_jid[jid].append(tag_name)
    return tags_by_jid


async def _fetch_groups_with_participants() -> tuple[int, list[dict[str, Any]], str | None]:
    status, payload, error = await _fetch_groups_from_group_broadcaster()
    if error is not None:
        return status, [], error
    if status >= 400:
        message = str(payload.get("error") or f"group_broadcaster_http_{status}")
        return status, [], message

    groups = _extract_groups_from_payload(payload)
    has_participants = any(_extract_participants(group) for group in groups)
    if has_participants:
        return 200, groups, None

    # Fallback: fetch from Evolution API with participants.
    evo_client = EvolutionGroupsClient()
    evo_status, evo_payload = await evo_client.fetch_all_groups(get_participants=True)
    if evo_status >= 400:
        message = str(evo_payload.get("error") or f"evolution_http_{evo_status}")
        return evo_status, [], message
    return 200, _extract_groups_from_payload(evo_payload), None


async def _insert_find_audit_row(
    conn: Any,
    *,
    phone: str,
    hitl_token: str,
    group_jids: list[str],
) -> int:
    payload_in = {"phone": phone}
    payload_out = {"phone": phone, "groupJids": group_jids, "total": len(group_jids)}
    row = await conn.fetchrow(
        """
        INSERT INTO hermes_group_ops_log (
            op_type,
            requestor,
            payload_in,
            payload_out,
            status,
            hitl_token,
            finished_at
        )
        VALUES (
            'find',
            'hermes',
            $1::jsonb,
            $2::jsonb,
            'ok',
            $3::uuid,
            NOW()
        )
        RETURNING id
        """,
        json.dumps(payload_in),
        json.dumps(payload_out),
        hitl_token,
    )
    return int(row["id"])


async def _insert_list_audit_row(
    conn: Any,
    *,
    payload_in: dict[str, Any],
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO hermes_group_ops_log (op_type, requestor, payload_in, status)
        VALUES ('list', 'hermes', $1::jsonb, 'queued')
        RETURNING id
        """,
        json.dumps(payload_in),
    )
    return int(row["id"])


async def _load_hitl_record(
    conn: Any,
    *,
    hitl_token: str,
    phone: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT id, payload_out, created_at
        FROM hermes_group_ops_log
        WHERE op_type = 'find'
          AND hitl_token = $1::uuid
          AND created_at >= (NOW() - ($2 || ' seconds')::interval)
        ORDER BY id DESC
        LIMIT 1
        """,
        hitl_token,
        str(HITL_TOKEN_TTL_SECONDS),
    )
    if row is None:
        return None

    payload_out = row["payload_out"] or {}
    if isinstance(payload_out, str):
        try:
            payload_out = json.loads(payload_out)
        except Exception:
            payload_out = {}
    if not isinstance(payload_out, dict):
        payload_out = {}

    stored_phone = str(payload_out.get("phone") or "").strip()
    if stored_phone != phone:
        return None

    original_jids_raw = payload_out.get("groupJids") or []
    if not isinstance(original_jids_raw, list):
        return None
    original_jids = {str(jid).strip() for jid in original_jids_raw if isinstance(jid, str) and str(jid).strip()}
    return {
        "id": int(row["id"]),
        "group_jids": original_jids,
    }


async def _insert_remove_audit_row(
    conn: Any,
    *,
    phone: str,
    group_jid: str,
    reason: str,
    hitl_token: str,
) -> int:
    payload_in = {
        "phone": phone,
        "groupJid": group_jid,
        "reason": reason,
    }
    row = await conn.fetchrow(
        """
        INSERT INTO hermes_group_ops_log (
            op_type,
            requestor,
            payload_in,
            status,
            hitl_token
        )
        VALUES (
            'remove',
            'hermes',
            $1::jsonb,
            'queued',
            $2::uuid
        )
        RETURNING id
        """,
        json.dumps(payload_in),
        hitl_token,
    )
    return int(row["id"])


async def _call_evolution_remove_with_retry(
    *,
    client: EvolutionGroupsClient,
    group_jid: str,
    participant_phone: str,
) -> tuple[int, dict[str, Any], int]:
    attempts = 0
    while True:
        attempts += 1
        status, payload = await client.remove_participant(
            group_jid=group_jid,
            participant_phone=participant_phone,
        )
        if status < 500 or attempts >= REMOVE_RETRY_ATTEMPTS:
            return status, payload, attempts
        delay = REMOVE_RETRY_BASE_SECONDS * (2 ** (attempts - 1))
        await asyncio.sleep(delay)


def _infer_final_status(http_status: int, result: dict[str, Any]) -> str:
    if http_status >= 500:
        return "error"
    if http_status >= 400:
        return "error"

    failed = result.get("failed")
    rejected = result.get("rejected")
    if isinstance(failed, int) and failed > 0:
        return "partial"
    if isinstance(rejected, int) and rejected > 0:
        return "partial"
    return "ok"


def _normalize_payload_out(
    *,
    result: dict[str, Any],
    audit_id: int,
    op_type: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = dict(result)
    payload.setdefault("ok", True)
    payload.setdefault("op_type", op_type)
    payload.setdefault("idempotency_key", idempotency_key)
    payload.setdefault("audit_id", audit_id)
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return payload


async def _handle_broadcast_common(request: "web.Request", *, op_type: str) -> "web.Response":
    try:
        _require_json_content_type(request)
        body = await request.json()
    except ValueError as exc:
        if str(exc) == "unsupported_media_type":
            return _json_error(415, "unsupported_media_type")
        return _json_error(400, "invalid_request")
    except Exception:
        return _json_error(400, "invalid_json")

    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    try:
        group_jids = _validate_group_jids(body)
    except ValueError as exc:
        return _json_error(400, str(exc))

    idempotency_key_raw = body.get("idempotency_key")
    if not isinstance(idempotency_key_raw, str) or not _is_uuid4(idempotency_key_raw.strip()):
        return _json_error(400, "idempotency_key_uuid4_required")
    idempotency_key = idempotency_key_raw.strip().lower()

    downstream_body: dict[str, Any]
    payload_in: dict[str, Any]
    endpoint_path: str

    if op_type == "broadcast":
        try:
            message = _validate_message(body)
        except ValueError as exc:
            return _json_error(400, str(exc))

        downstream_body = {
            "groupJids": group_jids,
            "message": message,
            "idempotency_key": idempotency_key,
        }
        payload_in = {
            "groupJids": group_jids,
            "message": message,
            "idempotency_key": idempotency_key,
        }
        endpoint_path = "/api/hermes/broadcast"
    else:
        try:
            media = _validate_media(body)
        except ValueError as exc:
            return _json_error(400, str(exc))

        downstream_body = {
            "groupJids": group_jids,
            "media": media,
            "idempotency_key": idempotency_key,
        }
        payload_in = {
            "groupJids": group_jids,
            "media": media,
            "idempotency_key": idempotency_key,
        }
        endpoint_path = "/api/hermes/broadcast-media"

    try:
        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
    except Exception as exc:
        return _json_error(503, "db_pool_unavailable", detail=str(exc))

    async with pool.acquire() as conn:
        existing = await _find_existing_idempotency(
            conn,
            op_type=op_type,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return web.json_response(existing, status=200)

        audit_id = await _insert_audit_row(
            conn,
            op_type=op_type,
            idempotency_key=idempotency_key,
            payload_in=payload_in,
        )

    http_status, downstream_result, dispatch_error = await _dispatch_group_broadcast(
        path=endpoint_path,
        body=downstream_body,
    )

    if dispatch_error is not None:
        payload_out = {
            "ok": False,
            "audit_id": audit_id,
            "error": "group_broadcaster_unavailable",
            "detail": dispatch_error,
        }
        async with pool.acquire() as conn:
            await _finish_audit_row(
                conn,
                audit_id=audit_id,
                status="error",
                payload_out=payload_out,
                error=dispatch_error,
            )
        return web.json_response(payload_out, status=503)

    final_status = _infer_final_status(http_status, downstream_result)
    payload_out = _normalize_payload_out(
        result=downstream_result,
        audit_id=audit_id,
        op_type=op_type,
        idempotency_key=idempotency_key,
    )
    payload_out["ok"] = final_status in {"ok", "partial"}

    error_text = None
    if final_status == "error":
        error_text = str(downstream_result.get("error") or f"group_broadcaster_http_{http_status}")
        payload_out.setdefault("error", error_text)

    async with pool.acquire() as conn:
        await _finish_audit_row(
            conn,
            audit_id=audit_id,
            status=final_status,
            payload_out=payload_out,
            error=error_text,
        )

    status_code = 200 if final_status in {"ok", "partial"} else max(400, http_status)
    return web.json_response(payload_out, status=status_code)


async def handle_broadcast(request: "web.Request") -> "web.Response":
    return await _handle_broadcast_common(request, op_type="broadcast")


async def handle_broadcast_media(request: "web.Request") -> "web.Response":
    return await _handle_broadcast_common(request, op_type="broadcast_media")


async def handle_find_participant(request: "web.Request") -> "web.Response":
    try:
        _require_json_content_type(request)
        body = await request.json()
    except ValueError as exc:
        if str(exc) == "unsupported_media_type":
            return _json_error(415, "unsupported_media_type")
        return _json_error(400, "invalid_request")
    except Exception:
        return _json_error(400, "invalid_json")

    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    try:
        phone = _normalize_phone(body.get("phone"))
    except ValueError as exc:
        return _json_error(400, str(exc))

    status, groups, fetch_error = await _fetch_groups_with_participants()
    if fetch_error is not None:
        return _json_error(max(500, status), "groups_fetch_failed", detail=fetch_error)

    matched: list[dict[str, Any]] = []
    matched_jids: list[str] = []
    for group in groups:
        jid = str(group.get("jid") or group.get("id") or "").strip()
        if not jid:
            continue
        name = str(group.get("name") or group.get("subject") or jid).strip()
        participants = _extract_participants(group)
        is_member = False
        is_admin = False
        for participant in participants:
            matches, participant_admin = _participant_matches_phone(participant, phone)
            if matches:
                is_member = True
                is_admin = participant_admin
                break
        if is_member:
            matched_jids.append(jid)
            matched.append({"jid": jid, "name": name, "is_admin": is_admin})

    hitl_token = str(uuid.uuid4())

    try:
        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
    except Exception as exc:
        return _json_error(503, "db_pool_unavailable", detail=str(exc))

    async with pool.acquire() as conn:
        audit_id = await _insert_find_audit_row(
            conn,
            phone=phone,
            hitl_token=hitl_token,
            group_jids=matched_jids,
        )

    payload = {
        "ok": True,
        "phone": phone,
        "groups": matched,
        "total": len(matched),
        "hitl_token": hitl_token,
        "audit_id": audit_id,
        "ttl_seconds": HITL_TOKEN_TTL_SECONDS,
    }
    return web.json_response(payload, status=200)


async def handle_remove_participant(request: "web.Request") -> "web.Response":
    try:
        _require_json_content_type(request)
        body = await request.json()
    except ValueError as exc:
        if str(exc) == "unsupported_media_type":
            return _json_error(415, "unsupported_media_type")
        return _json_error(400, "invalid_request")
    except Exception:
        return _json_error(400, "invalid_json")

    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    try:
        phone = _normalize_phone(body.get("phone"))
        group_jids = _validate_remove_group_jids(body)
        hitl_token = _validate_hitl_token(body.get("hitl_token"))
        reason = _validate_reason(body.get("reason"))
    except ValueError as exc:
        error_code = str(exc)
        if error_code.startswith("hitl_token"):
            return _json_error(403, error_code)
        return _json_error(400, error_code)

    try:
        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
    except Exception as exc:
        return _json_error(503, "db_pool_unavailable", detail=str(exc))

    async with pool.acquire() as conn:
        hitl_record = await _load_hitl_record(
            conn,
            hitl_token=hitl_token,
            phone=phone,
        )
    if hitl_record is None:
        return _json_error(403, "hitl_token_invalid_or_expired")

    token_group_jids = hitl_record["group_jids"]
    if not set(group_jids).issubset(token_group_jids):
        return _json_error(403, "hitl_token_group_scope_mismatch")

    evo_client = EvolutionGroupsClient()
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    first_audit_id: int | None = None

    for index, group_jid in enumerate(group_jids):
        async with pool.acquire() as conn:
            audit_id = await _insert_remove_audit_row(
                conn,
                phone=phone,
                group_jid=group_jid,
                reason=reason,
                hitl_token=hitl_token,
            )
        if first_audit_id is None:
            first_audit_id = audit_id

        status, result, attempts = await _call_evolution_remove_with_retry(
            client=evo_client,
            group_jid=group_jid,
            participant_phone=phone,
        )
        ok = status < 400
        error = None if ok else str(result.get("error") or f"evolution_http_{status}")
        payload_out = {
            "ok": ok,
            "jid": group_jid,
            "status_code": status,
            "attempts": attempts,
            "result": result,
        }

        async with pool.acquire() as conn:
            await _finish_audit_row(
                conn,
                audit_id=audit_id,
                status="ok" if ok else "error",
                payload_out=payload_out,
                error=error,
            )

        if ok:
            removed.append({"jid": group_jid, "ok": True})
        else:
            failed.append({"jid": group_jid, "error": error})

        if index < len(group_jids) - 1:
            await asyncio.sleep(REMOVE_SPACING_SECONDS)

    return web.json_response(
        {
            "ok": len(failed) == 0,
            "removed": removed,
            "failed": failed,
            "audit_id": first_audit_id,
        },
        status=200,
    )




async def handle_list_groups(request: "web.Request") -> "web.Response":
    """
    POST /api/gateway/groups/list
    Body: {"filter": {"tag": "...", "name_contains": "..."}}
    Returns: {"ok": true, "groups": [...], "total": N, "source": "group_broadcaster"}
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    filters = payload.get("filter") if isinstance(payload, dict) else None
    if not isinstance(filters, dict):
        filters = {}

    name_contains_raw = filters.get("name_contains")
    name_contains = name_contains_raw.strip().lower() if isinstance(name_contains_raw, str) else ""

    tag_raw = filters.get("tag")
    tag = tag_raw.strip().lower() if isinstance(tag_raw, str) else ""

    limit_raw = payload.get("limit") if isinstance(payload, dict) else None
    try:
        limit = int(limit_raw) if limit_raw is not None else None
    except Exception:
        return _json_error(400, "limit_invalid")
    if limit is not None and limit < 1:
        return _json_error(400, "limit_invalid")

    payload_in = {
        "filter": {"name_contains": name_contains or None, "tag": tag or None},
        "limit": limit,
    }

    try:
        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
    except Exception as exc:
        return _json_error(503, "db_pool_unavailable", detail=str(exc))

    async with pool.acquire() as conn:
        audit_id = await _insert_list_audit_row(conn, payload_in=payload_in)

    status, groups_payload, error = await _fetch_groups_from_group_broadcaster()
    if error is not None:
        payload_out = {"ok": False, "error": "group_broadcaster_unreachable", "detail": error, "audit_id": audit_id}
        async with pool.acquire() as conn:
            await _finish_audit_row(conn, audit_id=audit_id, status="error", payload_out=payload_out, error=error)
        return web.json_response(payload_out, status=503)
    if status >= 400:
        msg = str(groups_payload.get("error") or f"group_broadcaster_http_{status}") if isinstance(groups_payload, dict) else f"http_{status}"
        payload_out = {"ok": False, "error": msg, "audit_id": audit_id}
        async with pool.acquire() as conn:
            await _finish_audit_row(conn, audit_id=audit_id, status="error", payload_out=payload_out, error=msg)
        return web.json_response(payload_out, status=status)

    groups = _extract_groups_from_payload(groups_payload) if isinstance(groups_payload, dict) else []
    tags_by_jid = await _fetch_group_tags_by_jid()

    normalized: list[dict[str, Any]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        jid = str(g.get("id") or g.get("jid") or g.get("group_jid") or "").strip()
        name = str(g.get("subject") or g.get("name") or g.get("title") or "").strip()
        if not jid:
            continue
        size = g.get("size") or g.get("participants_count") or g.get("participantsCount")
        try:
            size_int = int(size) if size is not None else None
        except Exception:
            size_int = None
        tags_raw = g.get("tags")
        tags = [str(t).strip().lower() for t in tags_raw if isinstance(t, (str, int))] if isinstance(tags_raw, list) else []
        for tag_name in tags_by_jid.get(jid, []):
            if tag_name not in tags:
                tags.append(tag_name)
        normalized.append({
            "jid": jid,
            "name": name,
            "participants_count": size_int,
            "tags": tags,
            "announce": bool(g.get("announce")) if "announce" in g else None,
            "created_at": g.get("creation") or g.get("created_at"),
        })

    filtered = normalized
    if name_contains:
        filtered = [g for g in filtered if name_contains in (g.get("name") or "").lower()]
    if tag:
        filtered = [g for g in filtered if tag in (g.get("tags") or [])]
    if limit is not None:
        filtered = filtered[:limit]

    payload_out = {
        "ok": True,
        "source": "group_broadcaster",
        "total": len(filtered),
        "total_raw": len(normalized),
        "audit_id": audit_id,
        "filter": {"name_contains": name_contains or None, "tag": tag or None},
        "groups": filtered,
    }
    async with pool.acquire() as conn:
        await _finish_audit_row(conn, audit_id=audit_id, status="ok", payload_out=payload_out, error=None)

    return web.json_response(payload_out)


def build_groups_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application(middlewares=[gateway_bearer_middleware])
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("/broadcast", handle_broadcast)
    subapp.router.add_post("/broadcast-media", handle_broadcast_media)
    subapp.router.add_post("/find-participant", handle_find_participant)
    subapp.router.add_post("/remove-participant", handle_remove_participant)
    subapp.router.add_post("/list", handle_list_groups)
    return subapp


def mount_groups_subapp(parent_app: "web.Application", adapter: Any) -> "web.Application":
    subapp = build_groups_subapp(adapter)
    parent_app.add_subapp("/api/gateway/groups", subapp)
    return subapp


__all__ = [
    "build_groups_subapp",
    "gateway_bearer_middleware",
    "handle_broadcast",
    "handle_broadcast_media",
    "handle_find_participant",
    "handle_list_groups",
    "handle_remove_participant",
    "mount_groups_subapp",
]
