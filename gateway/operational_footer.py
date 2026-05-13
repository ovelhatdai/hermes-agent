"""SPEC-159 deterministic operational footer fallback with real Kanban card creation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import asyncpg

_TRIGGER_RE = re.compile(
    r"\b(spec|projeto|pend[eê]ncia|pendencia|tarefa|decis[aã]o|atualizei|atualiza|"
    r"confirmei|fechado|acordado|pr[oó]ximo passo|vou abrir|baixar? a spec|operacional)\b",
    re.IGNORECASE,
)
_FOOTER_MARKERS = ("📋 Pendências", "🗂️ Kanban", "▶️ Próximo passo")
_CASUAL_RE = re.compile(r"\b(que horas|almo[cç]o|fam[ií]lia|beleza|^ok$|bom dia|boa noite)\b", re.IGNORECASE)
_SPEC_RE = re.compile(r"\bSPEC-\d+\b", re.IGNORECASE)
DEFAULT_DATABASE_URL = ""


def _db_url() -> str:
    db_url = os.getenv("KANBAN_DATABASE_URL") or os.getenv("HERMES_DB_URL") or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    if not db_url:
        raise RuntimeError("KANBAN_DATABASE_URL/HERMES_DB_URL/DATABASE_URL not configured")
    return db_url


def _deadline_brt() -> str:
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    return (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M BRT")


def _source_hash(user_message: str | None, response: str | None) -> str:
    raw = f"{user_message or ''}\n---\n{response or ''}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:24]


def _extract_spec_id(text: str) -> str | None:
    match = _SPEC_RE.search(text or "")
    return match.group(0).upper() if match else None


def needs_operational_footer(user_message: str | None, response: str | None) -> bool:
    response = response or ""
    user_message = user_message or ""
    if not response.strip():
        return False
    if any(marker in response for marker in _FOOTER_MARKERS):
        return False
    text = f"{user_message}\n{response}"
    if _CASUAL_RE.search(user_message.strip()) and not _TRIGGER_RE.search(text):
        return False
    return bool(_TRIGGER_RE.search(text))


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE SCHEMA IF NOT EXISTS kanban;
        CREATE TABLE IF NOT EXISTS kanban.cards (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          parent_request_id UUID NOT NULL,
          agent_alvo VARCHAR(40) NOT NULL,
          skill VARCHAR(80) NOT NULL,
          payload JSONB NOT NULL,
          status VARCHAR(20) NOT NULL DEFAULT 'pending',
          result JSONB,
          error TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          started_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ,
          duration_ms INTEGER,
          sla_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kanban_parent ON kanban.cards (parent_request_id);
        CREATE INDEX IF NOT EXISTS idx_kanban_agent_status ON kanban.cards (agent_alvo, status);
        CREATE INDEX IF NOT EXISTS idx_kanban_open ON kanban.cards (status, created_at);
        """
    )


async def _create_operational_card(user_message: str | None, response: str | None, deadline: str) -> dict[str, Any]:
    source_hash = _source_hash(user_message, response)
    full_text = f"{user_message or ''}\n{response or ''}"
    spec_id = _extract_spec_id(full_text)
    payload = {
        "kind": "spec159_operational_footer",
        "source_hash": source_hash,
        "spec_id": spec_id,
        "dri": "Vinicius",
        "deadline_brt": deadline,
        "user_message_excerpt": (user_message or "")[:1200],
        "response_excerpt": (response or "")[:1200],
    }
    conn = await asyncpg.connect(_db_url())
    try:
        await _ensure_schema(conn)
        existing = await conn.fetchrow(
            """
            SELECT id::text, status
            FROM kanban.cards
            WHERE agent_alvo='vinicius'
              AND skill='operational-followup'
              AND payload->>'source_hash'=$1
              AND created_at >= NOW() - INTERVAL '6 hours'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            source_hash,
        )
        if existing:
            return {"created": False, "card_id": existing["id"], "status": existing["status"]}
        card_id = str(uuid.uuid4())
        parent_request_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO kanban.cards (id, parent_request_id, agent_alvo, skill, payload, status, sla_ms)
            VALUES ($1::uuid, $2::uuid, 'vinicius', 'operational-followup', $3::jsonb, 'pending', 86400000)
            """,
            card_id,
            parent_request_id,
            json.dumps(payload, ensure_ascii=False),
        )
        return {"created": True, "card_id": card_id, "status": "pending"}
    finally:
        await conn.close()


def _kanban_line(card: dict[str, Any] | None) -> str:
    if not card:
        return "Kanban indisponivel: falha ao criar card real. Nao considerar como card aberto."
    prefix = "Card criado" if card.get("created") else "Card existente"
    return f"{prefix}: {card.get('card_id')} — status: {card.get('status')} — tabela: kanban.cards."


async def ensure_operational_footer_async(response: str, user_message: str | None = None) -> str:
    if not needs_operational_footer(user_message, response):
        return response
    deadline = _deadline_brt()
    card = None
    try:
        card = await _create_operational_card(user_message, response, deadline)
    except Exception:
        card = None
    footer = (
        "📋 Pendências\n"
        f"1. Confirmar/acompanhar o item operacional desta conversa — DRI: Vinicius [DRI tentativa, confirma?] — Prazo: {deadline} — aguardando-ack\n\n"
        "▶️ Próximo passo\n"
        f"Vinicius confirma o encaminhamento ou ajusta o DRI até {deadline}.\n\n"
        "🗂️ Kanban\n"
        f"{_kanban_line(card)}"
    )
    return response.rstrip() + "\n\n" + footer


def ensure_operational_footer(response: str, user_message: str | None = None) -> str:
    if not needs_operational_footer(user_message, response):
        return response
    deadline = _deadline_brt()
    footer = (
        "📋 Pendências\n"
        f"1. Confirmar/acompanhar o item operacional desta conversa — DRI: Vinicius [DRI tentativa, confirma?] — Prazo: {deadline} — aguardando-ack\n\n"
        "▶️ Próximo passo\n"
        f"Vinicius confirma o encaminhamento ou ajusta o DRI até {deadline}.\n\n"
        "🗂️ Kanban\n"
        "Kanban indisponivel no caminho sincronico; este fallback nao criou card real."
    )
    return response.rstrip() + "\n\n" + footer
