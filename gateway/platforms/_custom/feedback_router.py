"""SPEC-146 task 05: Hermes feedback API routes."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import asyncpg
from aiohttp import web

logger = logging.getLogger(__name__)
VALID_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{4,8}$")
VALID_RATINGS = {"positive", "negative", "neutral"}
DEFAULT_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/hermes"


def _db_url() -> str:
    return os.getenv("HERMES_FEEDBACK_DATABASE_URL") or os.getenv("HERMES_DB_URL") or DEFAULT_DATABASE_URL


def _expected_token() -> str:
    return (os.getenv("HERMES_FEEDBACK_TOKEN") or "").strip()


def _authorized(request: web.Request) -> bool:
    token = _expected_token()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:] == token


def _validate_payload(body: dict[str, Any]) -> tuple[bool, str]:
    value_hash = str(body.get("hash") or "")
    rating = str(body.get("rating") or "")
    user_jid = str(body.get("user_jid") or "")
    if not VALID_HASH_RE.match(value_hash):
        return False, "invalid_hash"
    if rating not in VALID_RATINGS:
        return False, "invalid_rating"
    if not user_jid:
        return False, "missing_user_jid"
    context = body.get("context", {})
    if context is not None and not isinstance(context, dict):
        return False, "invalid_context"
    return True, ""


async def handle_feedback(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)
    valid, error = _validate_payload(body)
    if not valid:
        return web.json_response({"ok": False, "error": error}, status=422)

    context = body.get("context") or {}
    conn = await asyncpg.connect(_db_url())
    try:
        row_id = await conn.fetchval(
            """
            INSERT INTO hermes.feedback
                (hash, rating, user_jid, prompt_excerpt, response_excerpt,
                 skills_used, tools_used, latency_ms, model, raw_payload)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10::jsonb)
            ON CONFLICT (hash, user_jid) DO UPDATE
                SET rating = EXCLUDED.rating,
                    prompt_excerpt = COALESCE(EXCLUDED.prompt_excerpt, hermes.feedback.prompt_excerpt),
                    response_excerpt = COALESCE(EXCLUDED.response_excerpt, hermes.feedback.response_excerpt),
                    skills_used = COALESCE(EXCLUDED.skills_used, hermes.feedback.skills_used),
                    tools_used = COALESCE(EXCLUDED.tools_used, hermes.feedback.tools_used),
                    latency_ms = COALESCE(EXCLUDED.latency_ms, hermes.feedback.latency_ms),
                    model = COALESCE(EXCLUDED.model, hermes.feedback.model),
                    raw_payload = EXCLUDED.raw_payload,
                    created_at = now()
            RETURNING id
            """,
            str(body["hash"]),
            str(body["rating"]),
            str(body["user_jid"]),
            context.get("prompt_excerpt"),
            context.get("response_excerpt"),
            json.dumps(context.get("skills_used")) if context.get("skills_used") is not None else None,
            json.dumps(context.get("tools_used")) if context.get("tools_used") is not None else None,
            context.get("latency_ms"),
            context.get("model"),
            json.dumps(body),
        )
    except Exception as exc:
        logger.exception("[feedback] persist failed: %s", exc)
        return web.json_response({"ok": False, "error": "persist_failed"}, status=500)
    finally:
        await conn.close()
    return web.json_response({"ok": True, "feedback_id": row_id})


async def handle_feedback_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "schema": "hermes.feedback"})


def mount_feedback_subapp(parent_app: web.Application, adapter: Any) -> None:
    parent_app.router.add_post("/api/feedback", handle_feedback)
    parent_app.router.add_get("/api/feedback/health", handle_feedback_health)
    logger.info("[custom_extensions] feedback routes mounted")
