"""SPEC-159 deterministic operational footer fallback."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TRIGGER_RE = re.compile(
    r"\b(spec|projeto|pend[eê]ncia|pendencia|tarefa|decis[aã]o|atualizei|atualiza|"
    r"confirmei|fechado|acordado|pr[oó]ximo passo|vou abrir|baixar? a spec|operacional)\b",
    re.IGNORECASE,
)
_FOOTER_MARKERS = ("📋 Pendências", "🗂️ Kanban", "▶️ Próximo passo")
_CASUAL_RE = re.compile(r"\b(que horas|almo[cç]o|fam[ií]lia|beleza|^ok$|bom dia|boa noite)\b", re.IGNORECASE)


def _deadline_brt() -> str:
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    return (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M BRT")


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
        "Card não criado automaticamente por este fallback; manter rastreio textual e só citar card quando houver ID real."
    )
    return response.rstrip() + "\n\n" + footer
