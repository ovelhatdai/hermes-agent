"""Aggregator for SPEC-144 MentorHub mentee snapshots."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg

log = logging.getLogger("uvicorn.error")

_NOT_LIVE_ERRORS = (
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedColumnError,
    asyncpg.InvalidSchemaNameError,
)


async def aggregate_snapshot(mentee: dict[str, Any], pg_pool: asyncpg.Pool) -> dict[str, Any]:
    """Return the complete mentee snapshot with four independent sections."""

    oab = mentee.get("oab")
    mentee_id = mentee.get("id")
    started = time.perf_counter()

    results = await asyncio.gather(
        _fetch_trafego_cards(oab, pg_pool),
        _fetch_sla_alerts(oab, pg_pool),
        _fetch_kanban_tasks(oab, mentee_id, pg_pool),
        _fetch_latest_briefing(oab, pg_pool),
        return_exceptions=True,
    )

    section_names = ("trafego", "sla", "kanban", "briefing")
    for name, result in zip(section_names, results, strict=True):
        if isinstance(result, Exception):
            log.warning("[AGG-FAIL] section=%s oab=%s err=%s", name, oab, result)

    trafego_cards = [] if isinstance(results[0], Exception) else results[0]
    sla_alerts = [] if isinstance(results[1], Exception) else results[1]
    kanban_tasks = [] if isinstance(results[2], Exception) else results[2]
    latest_briefing = None if isinstance(results[3], Exception) else results[3]

    latency_ms = max(1, int((time.perf_counter() - started) * 1000))
    return {
        "mentee": mentee,
        "trafego_cards": trafego_cards,
        "sla_alerts": sla_alerts,
        "kanban_tasks": kanban_tasks,
        "latest_briefing": latest_briefing,
        "meta": {
            "cache_hit": False,
            "latency_ms": latency_ms,
            "fetched_at": datetime.now(UTC).isoformat(),
        },
    }


async def _fetch_trafego_cards(oab: str | None, pg_pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """SPEC-117 cards open for this mentee."""

    if not oab:
        return []

    sql = """
        SELECT id::text AS id,
               COALESCE(classificacao->>'categoria', origem, 'trafego_pago') AS categoria,
               prioridade AS severity,
               COALESCE(NULLIF(brief, ''), NULLIF(motivo, ''), 'Card de trafego') AS title,
               created_at,
               last_ack_at AS ack_at,
               owner AS responsavel
          FROM trafego.cards
         WHERE mentee_oab = $1
           AND last_ack_at IS NULL
           AND COALESCE(estado, '') NOT IN ('no_ar', 'pausado', 'resolved', 'archived', 'done')
      ORDER BY created_at DESC
         LIMIT 20
    """

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, oab)
    except _NOT_LIVE_ERRORS as exc:
        log.warning("[AGG-NOT-LIVE] section=trafego table=trafego.cards err=%s", type(exc).__name__)
        return []
    return [dict(row) for row in rows]


async def _fetch_sla_alerts(oab: str | None, pg_pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """SPEC-119 SLA alerts for this mentee when the schema supports mentee_oab."""

    if not oab:
        return []

    sql = """
        SELECT id::text AS id,
               COALESCE(item_type, area) AS type,
               sla_hours,
               age_hours AS elapsed_hours,
               COALESCE(message_sent, item_id, area) AS title,
               responsavel,
               detected_at AS created_at
          FROM supervisor.event_log
         WHERE mentee_oab = $1
           AND resolved_at IS NULL
      ORDER BY detected_at DESC
         LIMIT 20
    """

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, oab)
    except _NOT_LIVE_ERRORS as exc:
        log.warning("[AGG-NOT-LIVE] section=sla table=supervisor.event_log err=%s", type(exc).__name__)
        return []
    return [dict(row) for row in rows]


async def _fetch_kanban_tasks(
    oab: str | None,
    mentee_id: str | None,
    pg_pool: asyncpg.Pool,
) -> list[dict[str, Any]]:
    """SPEC-120 kanban cards linked to this mentee."""

    if not (oab or mentee_id):
        return []

    sql = """
        SELECT id::text AS id,
               COALESCE(payload->>'title', payload->>'task', payload->>'summary', skill) AS title,
               skill,
               agent_alvo AS agent,
               status,
               created_at
          FROM kanban.cards
         WHERE (
               ($1::text IS NOT NULL AND payload::text ILIKE '%' || $1::text || '%')
            OR ($2::text IS NOT NULL AND payload::text ILIKE '%' || $2::text || '%')
         )
           AND COALESCE(status, '') NOT IN ('archived', 'completed')
      ORDER BY created_at DESC
         LIMIT 20
    """

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, oab, mentee_id)
    except _NOT_LIVE_ERRORS as exc:
        log.warning("[AGG-NOT-LIVE] section=kanban table=kanban.cards err=%s", type(exc).__name__)
        return []
    return [dict(row) for row in rows]


async def _fetch_latest_briefing(oab: str | None, pg_pool: asyncpg.Pool) -> dict[str, Any] | None:
    """SPEC-123 latest generated briefing/insight for this mentee."""

    if not oab:
        return None

    sql = """
        SELECT id::text AS id, summary, generated_at
          FROM briefing.outputs
         WHERE mentee_oab = $1
      ORDER BY generated_at DESC
         LIMIT 1
    """

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(sql, oab)
    except _NOT_LIVE_ERRORS as exc:
        log.warning("[AGG-NOT-LIVE] section=briefing table=briefing.outputs err=%s", type(exc).__name__)
        return None
    return dict(row) if row else None
