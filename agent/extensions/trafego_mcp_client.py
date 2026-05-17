"""SPEC-117 task 10: cliente MCP isolado para o trafego_router.

Este modulo chama MCPs externos diretamente, sem loopback para o Hermes
Gateway. O objetivo e evitar o deadlock observado quando o webhook tenta
invocar /v1/chat/completions no mesmo processo que esta tratando o evento.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

CENTRAL_URL = os.environ.get(
    "HERMES_CENTRAL_MCP_URL", "https://central.advogando100k.com.br/mcp"
)
ADVDASH_URL = os.environ.get(
    "HERMES_ADVDASH_MCP_URL", "https://mcp.advogandodash.com.br/mcp"
)
ADVDASH_TOKEN = os.environ.get("ADVOGANDODASH_MCP_TOKEN", "").strip()

TOOL_TIMEOUT_SECONDS = float(os.environ.get("HERMES_TRAFEGO_TOOL_TIMEOUT", "10"))
BATCH_TIMEOUT_SECONDS = float(os.environ.get("HERMES_TRAFEGO_BATCH_TIMEOUT", "45"))

CENTRAL_TOOL_ALLOWLIST = {
    "get_meta_ads_data",
    "get_meta_ads_summary",
    "get_lead_facts_by_phone",
    "search_transcriptions",
}

ADVDASH_TOOL_ALLOWLIST = {
    "listar_clientes",
    "list_clients",
    "query_entities",
}


def _normalize_tool_name(tool_name: str, server: str) -> str:
    """Aceita nomes nativos do MCP ou nomes prefixados pelo wrapper Hermes."""
    prefixes = {
        "central": "mcp_central_inteligencia_",
        "advdash": "mcp_advogando_dash_",
    }
    prefix = prefixes[server]
    if tool_name.startswith(prefix):
        return tool_name[len(prefix) :]
    return tool_name


def _content_to_jsonable(content: Any) -> Any:
    """Converte CallToolResult em estrutura simples para log/prompt."""
    structured = getattr(content, "structuredContent", None)
    if structured is not None:
        return structured

    items = getattr(content, "content", None)
    if items is None:
        return content

    out: list[Any] = []
    for item in items:
        text = getattr(item, "text", None)
        if text is None:
            out.append(str(item))
            continue
        try:
            out.append(json.loads(text))
        except Exception:
            out.append(text)
    if len(out) == 1:
        return out[0]
    return out


async def _call_tool(
    *,
    url: str,
    tool_name: str,
    args: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=TOOL_TIMEOUT_SECONDS,
            sse_read_timeout=TOOL_TIMEOUT_SECONDS,
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, args),
                    timeout=TOOL_TIMEOUT_SECONDS,
                )
                is_error = bool(getattr(result, "isError", False))
                return {
                    "ok": not is_error,
                    "tool": tool_name,
                    "data": _content_to_jsonable(result),
                    "error": "tool_returned_error" if is_error else None,
                }
    except asyncio.TimeoutError:
        return {"ok": False, "tool": tool_name, "data": None, "error": "timeout"}
    except Exception as exc:
        logger.warning("MCP tool call failed tool=%s error=%s", tool_name, exc)
        return {"ok": False, "tool": tool_name, "data": None, "error": str(exc)}


async def call_central_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    native = _normalize_tool_name(tool_name, "central")
    if native not in CENTRAL_TOOL_ALLOWLIST:
        return {
            "ok": False,
            "tool": native,
            "data": None,
            "error": "tool_not_allowed",
        }
    args = _filter_central_args(native, args)
    return await _call_tool(url=CENTRAL_URL, tool_name=native, args=args)


async def call_advdash_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    native = _normalize_tool_name(tool_name, "advdash")
    if native not in ADVDASH_TOOL_ALLOWLIST:
        return {
            "ok": False,
            "tool": native,
            "data": None,
            "error": "tool_not_allowed",
        }

    headers = {"Authorization": f"Bearer {ADVDASH_TOKEN}"} if ADVDASH_TOKEN else None
    return await _call_tool(url=ADVDASH_URL, tool_name=native, args=args, headers=headers)


async def call_trafego_batch(
    *,
    phone: str | None = None,
    mentorada_name: str | None = None,
    meta_ads_account_id: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Busca dados de apoio para diagnostico de trafego em chamadas paralelas."""
    calls: list[asyncio.Task[dict[str, Any]]] = []

    if meta_ads_account_id:
        # O MCP central atual nao aceita account_id direto; usa segment como
        # filtro melhor-esforco ate a task 09 popular uma fonte oficial.
        calls.append(
            asyncio.create_task(
                call_central_tool(
                    "get_meta_ads_data",
                    {"segment": meta_ads_account_id},
                )
            )
        )

    if phone:
        calls.append(
            asyncio.create_task(
                call_central_tool(
                    "get_lead_facts_by_phone",
                    {"phone": phone, "limit": 30},
                )
            )
        )

    if mentorada_name:
        calls.append(
            asyncio.create_task(
                call_central_tool(
                    "search_transcriptions",
                    {
                        "query": f"{mentorada_name} trafego",
                        "source": "all",
                        "max_results": 5,
                    },
                )
            )
        )

    if not calls:
        return {"ok": True, "results": [], "error": None}

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*calls),
            timeout=BATCH_TIMEOUT_SECONDS,
        )
        return {"ok": True, "results": results, "error": None}
    except asyncio.TimeoutError:
        for task in calls:
            task.cancel()
        return {"ok": False, "results": [], "error": "batch_timeout"}


def _filter_central_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Remove chaves extras porque o MCP central usa additionalProperties=false."""
    allowed: dict[str, set[str]] = {
        "get_meta_ads_data": {"segment", "gestor"},
        "get_meta_ads_summary": set(),
        "get_lead_facts_by_phone": {"phone", "limit"},
        "search_transcriptions": {"query", "source", "max_results"},
    }
    keys = allowed.get(tool_name)
    if keys is None:
        return args
    return {k: v for k, v in args.items() if k in keys and v is not None}
