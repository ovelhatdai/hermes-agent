"""SPEC-120 Kanban delegation tools for Hermes.

These tools create rows in the production ``kanban.cards`` table, call an
internal child-agent endpoint, and persist the outcome for audit/smoke use.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

import asyncpg
import httpx

from tools.registry import registry, tool_error

DEFAULT_DATABASE_URL = "postgresql://postgres:EvDb_Adv100k_2026!Migr@127.0.0.1:5432/hermes"
AGENT_ENDPOINTS = {
    "larissinha": "http://127.0.0.1:9123/api/internal/task",
    "helena": "http://127.0.0.1:9126/api/internal/task",
    "bia": "http://127.0.0.1:9121/api/internal/task",
    "clara-sdr": "http://127.0.0.1:9116/api/internal/task",
    "clara-des": "http://127.0.0.1:9119/api/internal/task",
    "daiane-content": "http://127.0.0.1:9183/api/internal/task",
}
ALLOWED_SKILLS = {
    "larissinha": {"analise-juridica-rapida", "consulta-datajud", "extrai-tese-juridica"},
    "helena": {"status-cliente-completo", "historico-atendimentos"},
    "bia": {"engajamento-aluno"},
    "clara-sdr": {"lead-context"},
    "clara-des": {"pre-vendas-cliente"},
    "daiane-content": {"roteiro-reel", "copy-anuncio"},
}


def _db_url() -> str:
    return os.getenv("KANBAN_DATABASE_URL") or os.getenv("HERMES_DB_URL") or DEFAULT_DATABASE_URL


def _token() -> str:
    return os.getenv("KANBAN_INTERNAL_TOKEN", "").strip()


def _normalize_agent(agent: str) -> str:
    value = (agent or "").strip().lower()
    aliases = {"larissa": "larissinha", "dona-helena": "helena", "clara": "clara-sdr"}
    return aliases.get(value, value)


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


async def _delegate(agent: str, skill: str, payload: dict[str, Any], sla_ms: int = 30000, await_result: bool = True) -> dict[str, Any]:
    agent = _normalize_agent(agent)
    skill = (skill or "").strip()
    if agent not in AGENT_ENDPOINTS:
        raise ValueError(f"agente desconhecido: {agent}")
    if skill not in ALLOWED_SKILLS.get(agent, set()):
        raise ValueError(f"skill {skill!r} nao permitida para {agent}")
    if not isinstance(payload, dict):
        raise ValueError("payload precisa ser objeto JSON")
    token = _token()
    if not token:
        raise RuntimeError("KANBAN_INTERNAL_TOKEN ausente no ambiente do Hermes")

    card_id = str(uuid.uuid4())
    parent_request_id = str(uuid.uuid4())
    started = time.monotonic()
    conn = await asyncpg.connect(_db_url())
    try:
        await _ensure_schema(conn)
        await conn.execute(
            """
            INSERT INTO kanban.cards (id, parent_request_id, agent_alvo, skill, payload, status, started_at, sla_ms)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, 'in_progress', NOW(), $6)
            """,
            card_id,
            parent_request_id,
            agent,
            skill,
            json.dumps(payload, ensure_ascii=False),
            int(sla_ms),
        )
        if not await_result:
            return {"card_id": card_id, "parent_request_id": parent_request_id, "status": "in_progress"}

        timeout = max(1.0, int(sla_ms) / 1000.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    AGENT_ENDPOINTS[agent],
                    headers={"Authorization": f"Bearer {token}"},
                    json={"card_id": card_id, "skill": skill, "payload": payload},
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
            if resp.status_code >= 400 or data.get("error"):
                error = data.get("error") or f"HTTP {resp.status_code}: {resp.text[:300]}"
                await conn.execute(
                    "UPDATE kanban.cards SET status='failed', error=$2, completed_at=NOW(), duration_ms=$3 WHERE id=$1::uuid",
                    card_id,
                    str(error),
                    duration_ms,
                )
                return {"card_id": card_id, "parent_request_id": parent_request_id, "status": "failed", "error": str(error)}
            result = data.get("result", data)
            await conn.execute(
                "UPDATE kanban.cards SET status='done', result=$2::jsonb, completed_at=NOW(), duration_ms=$3 WHERE id=$1::uuid",
                card_id,
                json.dumps(result, ensure_ascii=False),
                duration_ms,
            )
            return {"card_id": card_id, "parent_request_id": parent_request_id, "status": "done", "result": result, "duration_ms": duration_ms}
        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - started) * 1000)
            await conn.execute(
                "UPDATE kanban.cards SET status='timeout', error='timeout', completed_at=NOW(), duration_ms=$2 WHERE id=$1::uuid",
                card_id,
                duration_ms,
            )
            return {"card_id": card_id, "parent_request_id": parent_request_id, "status": "timeout", "error": "timeout", "duration_ms": duration_ms}
    finally:
        await conn.close()


async def _fetch_card(card_id: str) -> dict[str, Any] | None:
    conn = await asyncpg.connect(_db_url())
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text, parent_request_id::text, agent_alvo, skill, payload::text, status,
                   result::text, error, created_at::text, started_at::text, completed_at::text,
                   duration_ms, sla_ms
            FROM kanban.cards WHERE id=$1::uuid
            """,
            card_id,
        )
        if not row:
            return None
        out = dict(row)
        for key in ("payload", "result"):
            if out.get(key):
                out[key] = json.loads(out[key])
        return out
    finally:
        await conn.close()


async def _handle_delegate(args: dict, **kw) -> str:
    try:
        out = await _delegate(
            args.get("agent"),
            args.get("skill"),
            args.get("payload") or {},
            int(args.get("sla_ms") or 30000),
            bool(args.get("await_result", True)),
        )
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"kanban_delegate: {exc}")


async def _handle_await(args: dict, **kw) -> str:
    card_id = str(args.get("card_id") or "").strip()
    timeout_ms = int(args.get("timeout_ms") or 30000)
    if not card_id:
        return tool_error("card_id e obrigatorio")
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
    while True:
        card = await _fetch_card(card_id)
        if not card:
            return tool_error(f"card {card_id} nao encontrado")
        if card["status"] in {"done", "failed", "timeout"}:
            return json.dumps(card, ensure_ascii=False)
        if time.monotonic() >= deadline:
            return json.dumps(card | {"await_status": "timeout"}, ensure_ascii=False)
        await asyncio.sleep(0.5)


async def _handle_batch(args: dict, **kw) -> str:
    tasks = args.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return tool_error("tasks precisa ser lista nao vazia")
    coros = [
        _delegate(t.get("agent"), t.get("skill"), t.get("payload") or {}, int(t.get("sla_ms") or 30000), True)
        for t in tasks
        if isinstance(t, dict)
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    normalized = []
    for item in results:
        if isinstance(item, Exception):
            normalized.append({"status": "failed", "error": str(item)})
        else:
            normalized.append(item)
    return json.dumps({"results": normalized}, ensure_ascii=False)


DELEGATE_SCHEMA = {
    "description": "Cria um card kanban, chama um agente filho interno e grava status/result em kanban.cards.",
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": sorted(AGENT_ENDPOINTS.keys())},
            "skill": {"type": "string"},
            "payload": {"type": "object"},
            "sla_ms": {"type": "integer", "default": 30000},
            "await_result": {"type": "boolean", "default": True},
        },
        "required": ["agent", "skill", "payload"],
    },
}

AWAIT_SCHEMA = {
    "description": "Consulta/aguarda um card kanban ate status terminal.",
    "parameters": {
        "type": "object",
        "properties": {"card_id": {"type": "string"}, "timeout_ms": {"type": "integer", "default": 30000}},
        "required": ["card_id"],
    },
}

BATCH_SCHEMA = {
    "description": "Delega varias subtasks kanban em paralelo e retorna os resultados.",
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string"},
                        "skill": {"type": "string"},
                        "payload": {"type": "object"},
                        "sla_ms": {"type": "integer"},
                    },
                    "required": ["agent", "skill", "payload"],
                },
            }
        },
        "required": ["tasks"],
    },
}

registry.register(name="kanban_delegate", toolset="kanban_delegate", schema=DELEGATE_SCHEMA, handler=_handle_delegate, is_async=True, emoji="K")
registry.register(name="kanban_await", toolset="kanban_delegate", schema=AWAIT_SCHEMA, handler=_handle_await, is_async=True, emoji="K")
registry.register(name="kanban_delegate_batch", toolset="kanban_delegate", schema=BATCH_SCHEMA, handler=_handle_batch, is_async=True, emoji="K")
