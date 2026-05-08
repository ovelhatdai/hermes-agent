"""SPEC-146 task 06: feedback hash injection and usage tracking."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.request
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

COVERED_JIDS = {
    "5551991987972",
    "5551991987972@s.whatsapp.net",
    "+5551991987972",
    "143658066157619@lid",
    "5551984213925",
    "5551984213925@s.whatsapp.net",
    "+5551984213925",
}
HASH_RE = re.compile(r"\[#([A-Za-z0-9_-]{4,8})\]")
POSITIVE = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"}
NEGATIVE = {"👎", "👎🏻", "👎🏼", "👎🏽", "👎🏾", "👎🏿"}
DEFAULT_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/hermes"


def _normalize_jid(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip()
    if ":" in value and "@" in value:
        value = value.replace(":", "@", 1)
    return value


def _is_covered_dm(chat_id: str | None) -> bool:
    chat_id = _normalize_jid(chat_id)
    if not chat_id or chat_id.endswith("@g.us"):
        return False
    bare = chat_id.split("@")[0]
    return chat_id in COVERED_JIDS or bare in COVERED_JIDS


def _db_url() -> str:
    return os.getenv("HERMES_FEEDBACK_DATABASE_URL") or os.getenv("HERMES_DB_URL") or DEFAULT_DATABASE_URL


def gen_hash(prompt: str, response: str, user_jid: str = "") -> str:
    raw = f"{user_jid}::{prompt}::{response}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:4]


def inject_feedback_hash(response: str, *, chat_id: str, prompt: str = "") -> tuple[str, str | None]:
    """Append a compact feedback hash only to Vini/Daiane DMs."""
    if not _is_covered_dm(chat_id):
        return response, None
    if HASH_RE.search(response or ""):
        match = HASH_RE.search(response or "")
        return response, match.group(1) if match else None
    hash_id = gen_hash(prompt, response, chat_id)
    return f"{response}\n\n[#{hash_id}]", hash_id


async def track_usage(
    feature: str,
    name: str,
    user_jid: str | None = None,
    success: bool | None = None,
    latency_ms: int | None = None,
    error_msg: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        conn = await asyncpg.connect(_db_url(), timeout=2)
        try:
            await conn.execute(
                """
                INSERT INTO hermes.usage_tracking
                    (feature, name, user_jid, success, latency_ms, error_msg, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                feature,
                name,
                user_jid,
                success,
                latency_ms,
                error_msg,
                json.dumps(metadata or {}),
            )
        finally:
            await conn.close()
    except Exception as exc:  # never break the user response path
        log.warning("[feedback_middleware] track_usage failed: %s", exc)


async def detect_emoji_feedback(message_text: str | None, sender_jid: str | None, quoted_text: str | None) -> dict[str, Any] | None:
    sender_jid = _normalize_jid(sender_jid)
    if not _is_covered_dm(sender_jid):
        return None
    text = message_text or ""
    match = HASH_RE.search(quoted_text or "")
    if not match:
        inline_match = HASH_RE.search(text)
        if not inline_match:
            inline_match = re.search(r"(?:#|jogo da velha\s+)([A-Za-z0-9_-]{4,8})", text, re.IGNORECASE)
        if not inline_match:
            return None
        match = inline_match
    rating = None
    if any(mark in text for mark in POSITIVE):
        rating = "positive"
    elif any(mark in text for mark in NEGATIVE):
        rating = "negative"
    if not rating:
        return None
    return {
        "hash": match.group(1),
        "rating": rating,
        "user_jid": sender_jid,
        "context": {"quoted_excerpt": (quoted_text or text)[:200]},
    }


async def post_feedback(payload: dict[str, Any]) -> bool:
    token = os.getenv("HERMES_FEEDBACK_TOKEN", "").strip()
    base_url = (os.getenv("HERMES_BRIDGE_URL") or os.getenv("HERMES_GATEWAY_URL") or "http://127.0.0.1:8642").rstrip("/")
    if not token:
        log.warning("[feedback_middleware] HERMES_FEEDBACK_TOKEN missing")
        return False

    def _post() -> bool:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/feedback",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300

    try:
        return await asyncio.to_thread(_post)
    except Exception as exc:
        log.warning("[feedback_middleware] post_feedback failed: %s", exc)
        return False
