"""SPEC-117 task 11: prefetch real de trafego para alertas do Hermes.

Camada leve e isolada: puxa dados ja materializados no Meta Automation
(:8080/dashboard/insights) e formata um bloco curto para o DeepSeek. Nao usa
loopback no Hermes e nao chama Graph API diretamente.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import unicodedata
from decimal import Decimal
from typing import Any

import aiohttp

META_AUTOMATION_URL = os.environ.get(
    "HERMES_META_AUTOMATION_URL", "http://127.0.0.1:8080"
).rstrip("/")
META_DASHBOARD_USER = (
    os.environ.get("HERMES_META_DASHBOARD_USER")
    or os.environ.get("HERMES_DASHBOARD_USER")
    or ""
).strip()
META_DASHBOARD_PASS = (
    os.environ.get("HERMES_META_DASHBOARD_PASS")
    or os.environ.get("HERMES_DASHBOARD_PASS")
    or ""
).strip()
PREFETCH_TIMEOUT_SECONDS = float(os.environ.get("HERMES_TRAFEGO_PREFETCH_TIMEOUT", "8"))
PREFETCH_TOTAL_TIMEOUT_SECONDS = float(
    os.environ.get("HERMES_TRAFEGO_PREFETCH_TOTAL_TIMEOUT", "30")
)

CATEGORY_TOOL_HINTS = {
    "trafego_pago": [
        "meta_ads",
        "lead_facts",
        "transcricoes",
        "clientes_des",
    ],
    "comercial_des": [
        "lead_facts",
        "transcricoes",
        "clientes_des",
    ],
    "suporte": [
        "lead_facts",
        "transcricoes",
    ],
    "juridico": [
        "transcricoes",
        "clientes_des",
    ],
    "outro": [
        "transcricoes",
    ],
}


def _norm(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(adv100k|advogando\s*100k|adv\s*100k|100k|adv)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _to_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def _fmt_money(value: Any) -> str:
    number = _to_number(value)
    if number is None:
        return "sem dado"
    return f"R${number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(value: Any) -> str:
    number = _to_number(value)
    if number is None:
        return "sem dado"
    return f"{number:.2f}%".replace(".", ",")


async def _fetch_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=PREFETCH_TIMEOUT_SECONDS)
    headers = {}
    if META_DASHBOARD_USER and META_DASHBOARD_PASS:
        raw = f"{META_DASHBOARD_USER}:{META_DASHBOARD_PASS}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{META_AUTOMATION_URL}{path}", params=params, headers=headers
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"meta_automation_http_{resp.status}: {text[:160]}")
            return await resp.json()


async def _safe_mcp_call(label: str, call: Any) -> tuple[str, Any, str | None]:
    try:
        result = await asyncio.wait_for(call, timeout=10)
    except Exception as exc:
        return label, None, str(exc)

    if isinstance(result, dict) and result.get("ok"):
        return label, result.get("data"), None
    if isinstance(result, dict):
        return label, None, str(result.get("error") or "tool_returned_empty")
    return label, result, None


def _score_candidate(row: dict[str, Any], *, mentorada_name: str, account_id: str | None) -> int:
    row_act = str(row.get("act_id") or "")
    if account_id and row_act.replace("act_", "") == account_id.replace("act_", ""):
        return 1000

    target = _norm(mentorada_name)
    candidate = _norm(row.get("mentorada") or row.get("name"))
    if not target or not candidate:
        return 0
    if target == candidate:
        return 900
    target_parts = set(target.split())
    cand_parts = set(candidate.split())
    if not target_parts:
        return 0
    overlap = len(target_parts & cand_parts)
    return overlap * 100 - abs(len(target_parts) - len(cand_parts)) * 10


def _pick_account(
    accounts: list[dict[str, Any]], *, mentorada_name: str, account_id: str | None = None
) -> dict[str, Any] | None:
    scored = [
        (_score_candidate(row, mentorada_name=mentorada_name, account_id=account_id), row)
        for row in accounts
    ]
    scored = [(score, row) for score, row in scored if score >= 100]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


async def _fetch_meta_ads_context(
    *, mentorada_name: str, account_id: str | None
) -> tuple[str, Any, str | None]:
    try:
        last_7d, last_30d = await asyncio.gather(
            _fetch_json("/dashboard/insights", {"period": "last_7d"}),
            _fetch_json("/dashboard/insights", {"period": "last_30d"}),
        )
    except Exception as exc:
        return "meta_ads", None, f"meta_automation: {exc}"

    acc7 = _pick_account(
        list(last_7d.get("accounts") or []),
        mentorada_name=mentorada_name,
        account_id=str(account_id) if account_id else None,
    )
    acc30 = _pick_account(
        list(last_30d.get("accounts") or []),
        mentorada_name=mentorada_name,
        account_id=str(account_id) if account_id else None,
    )
    if not acc7:
        return (
            "meta_ads",
            None,
            "meta_ads: conta da mentorada nao encontrada no Meta Automation",
        )

    conversations = _to_number(acc7.get("conversations")) or 0
    spend = _to_number(acc7.get("spend")) or 0
    cpl = _to_number(acc7.get("cpl"))
    cpl_calc = spend / conversations if conversations else None
    return (
        "meta_ads",
        {
            "period": "last_7d",
            "act_id": acc7.get("act_id"),
            "mentorada": acc7.get("mentorada"),
            "spend": spend,
            "conversations": conversations,
            "leads": _to_number(acc7.get("leads")) or 0,
            "cpl": cpl if cpl not in (None, 0) else cpl_calc,
            "ctr": _to_number(acc7.get("ctr")),
            "cpc": _to_number(acc7.get("cpc")),
            "account_status": acc7.get("account_status"),
            "captured_at": acc7.get("captured_at"),
            "last_30d": {
                "spend": _to_number((acc30 or {}).get("spend")),
                "conversations": _to_number((acc30 or {}).get("conversations")),
                "leads": _to_number((acc30 or {}).get("leads")),
                "cpl": _to_number((acc30 or {}).get("cpl")),
            }
            if acc30
            else None,
        },
        None,
    )


async def pre_fetch_mentorada_context(body: dict[str, Any]) -> dict[str, Any]:
    """Busca dados reais em paralelo para enriquecer o alerta.

    Retorna sempre um dict seguro para log, mesmo se algum endpoint falhar.
    """
    category = str(body.get("category") or "trafego_pago").strip()
    mentorada_name = str(body.get("mentorada_name") or "").strip()
    mentorada_phone = (
        body.get("mentorada_phone")
        or body.get("phone")
        or body.get("sender_phone")
        or body.get("sender_jid")
    )
    mentorada_slug = body.get("mentorada_slug") or body.get("slug")
    account_id = (
        body.get("meta_ads_account_id")
        or body.get("ad_account_id")
        or body.get("account_id")
    )
    enabled = set(CATEGORY_TOOL_HINTS.get(category) or CATEGORY_TOOL_HINTS["outro"])
    calls: list[Any] = []

    if "meta_ads" in enabled:
        calls.append(
            _fetch_meta_ads_context(
                mentorada_name=mentorada_name,
                account_id=str(account_id) if account_id else None,
            )
        )

    try:
        from agent.extensions.trafego_mcp_client import (
            call_advdash_tool,
            call_central_tool,
        )
    except Exception:
        call_advdash_tool = None
        call_central_tool = None

    if call_central_tool and "lead_facts" in enabled and mentorada_phone:
        phone = str(mentorada_phone).replace("@s.whatsapp.net", "")
        calls.append(
            _safe_mcp_call(
                "lead_facts",
                call_central_tool("get_lead_facts_by_phone", {"phone": phone, "limit": 30}),
            )
        )

    if call_central_tool and "transcricoes" in enabled and mentorada_name:
        calls.append(
            _safe_mcp_call(
                "transcricoes",
                call_central_tool(
                    "search_transcriptions",
                    {
                        "query": f"{mentorada_name} {category}",
                        "source": "all",
                        "max_results": 5,
                    },
                ),
            )
        )

    if call_advdash_tool and "clientes_des" in enabled:
        args = {}
        if mentorada_slug:
            args["mentorada_slug"] = mentorada_slug
        elif mentorada_name:
            args["query"] = mentorada_name
        calls.append(_safe_mcp_call("clientes_des", call_advdash_tool("listar_clientes", args)))

    errors: list[str] = []
    context: dict[str, Any] = {
        "ok": False,
        "category": category,
        "tools_attempted": sorted(enabled),
        "meta_ads": None,
        "lead_facts": None,
        "transcricoes": None,
        "clientes_des": None,
        "errors": errors,
    }

    if not calls:
        return {
            **context,
            "errors": ["prefetch: nenhuma fonte disponivel para esta categoria"],
        }

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*calls, return_exceptions=True),
            timeout=PREFETCH_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {
            **context,
            "errors": ["prefetch: timeout total de 30s"],
        }

    for result in results:
        if isinstance(result, Exception):
            errors.append(f"prefetch: {result}")
            continue
        label, data, error = result
        if error:
            errors.append(f"{label}: {error}")
        context[label] = data

    context["ok"] = any(
        context.get(key) for key in ("meta_ads", "lead_facts", "transcricoes", "clientes_des")
    )
    return context


def format_context_for_prompt(ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    meta = ctx.get("meta_ads") or {}
    if meta:
        lines = [
            "Meta Ads ultimos 7 dias:",
            f"- Conta: {meta.get('mentorada') or meta.get('act_id')}",
            f"- Gasto: {_fmt_money(meta.get('spend'))}",
            f"- Conversas/leads Meta: {int(meta.get('conversations') or 0)} conversas, {int(meta.get('leads') or 0)} leads",
            f"- Custo por conversa/lead: {_fmt_money(meta.get('cpl'))}",
            f"- CTR: {_fmt_pct(meta.get('ctr'))}; CPC: {_fmt_money(meta.get('cpc'))}",
        ]
        last_30d = meta.get("last_30d") or {}
        if last_30d:
            lines.append(
                "Comparativo 30 dias: "
                f"{_fmt_money(last_30d.get('spend'))} gastos, "
                f"{int(last_30d.get('conversations') or 0)} conversas, "
                f"custo medio {_fmt_money(last_30d.get('cpl'))}."
            )
        lines.append("CAC real ainda depende de contrato/fechamento conectado ao card.")
        parts.append("\n".join(lines))

    lead_facts = ctx.get("lead_facts")
    if lead_facts:
        parts.append(f"Lead facts / conversas: {str(lead_facts)[:900]}")

    transcricoes = ctx.get("transcricoes")
    if transcricoes:
        parts.append(f"Transcricoes recentes: {str(transcricoes)[:900]}")

    clientes_des = ctx.get("clientes_des")
    if clientes_des:
        parts.append(f"Clientes DES / AdvDash: {str(clientes_des)[:700]}")

    if ctx.get("errors"):
        parts.append(
            "Dados indisponiveis agora: " + "; ".join(str(x) for x in ctx["errors"][:4])
        )

    return "\n\n".join(parts) if parts else "Sem dados disponiveis das tools."
