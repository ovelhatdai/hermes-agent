"""SPEC-121 R7: constructive pushback gate for spec/runtime changes."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

TECHNICAL_TARGET_PATTERNS = [
    r"\bspec[- ]?\d+", r"\bspec\b", r"\bcompozy\b", r"\bruntime\b",
    r"\bdeploy\b", r"\bprodução\b", r"\bproducao\b", r"\baceite\b",
    r"\bprioridade\b", r"\btask\b", r"\bservi[cç]o\b", r"\bsystemd\b",
    r"\btimer\b", r"\bworker\b", r"\bendpoint\b", r"\bwebhook\b",
    r"\bporta\b", r"\bpostgres\b", r"\bbanco\b", r"\bgit\b", r"\brebase\b",
    r"\brollback\b", r"\bauto-update\b", r"\bbranch\b", r"\bfork\b",
]
ACTION_PATTERNS = [
    r"\brodar\b", r"\bexecutar\b", r"\bimplementar\b", r"\bativar\b",
    r"\bdesativar\b", r"\bmudar\b", r"\bmuda\b", r"\balterar\b",
    r"\bcorrigir\b", r"\bcorrija\b", r"\baplicar\b", r"\bdeployar\b",
    r"\bsubir\b", r"\breiniciar\b", r"\bbaixar\b", r"\bmarcar done\b",
    r"\bmover pra done\b", r"\bcommitar\b", r"\bmergear\b", r"\bdeletar\b",
]
COMPLAINT_OR_CLARIFICATION_PATTERNS = [
    r"\bn[aã]o entendi\b", r"\bme explica\b", r"\bexplica\b", r"\bentender\b",
    r"\bo que aconteceu\b", r"\bo que esta acontecendo\b", r"\bo que está acontecendo\b",
    r"\bn[aã]o me serve\b", r"\bsem sentido\b", r"\bruim\b", r"\bbobagem\b",
    r"\bn[aã]o funciona\b", r"\bnao funciona\b", r"\bestou bravo\b", r"\bestou confuso\b",
    r"\bcliente\b", r"\bme ajuda entender\b",
]
SIMPLE_READ_PATTERNS = [
    r"\bo que é\b", r"\bo que e\b", r"\bexplica\b", r"\bentender\b", r"\bstatus\b",
    r"\blista\b", r"\bmostra\b", r"\baudita\b", r"\bvalida\b", r"\bverifica\b",
]
OVERRIDE_PATTERNS = [
    r"\bexecuta mesmo assim\b", r"\broda mesmo assim\b", r"\bfaz mesmo assim\b",
    r"\bsegue mesmo assim\b",
]
GOOD_SCOPE_PATTERNS = [
    r"\bsimples\b", r"\bmvp\b", r"\bpequen[ao]\b", r"\brevers[ií]vel\b",
    r"\btest[aá]vel\b", r"\bsmoke\b", r"\b2-3 perguntas\b", r"\bduas\b", r"\btr[eê]s\b",
]
RISKY_SCOPE_PATTERNS = [
    r"\bsozinh[oa]\b", r"\bautomaticamente\b", r"\bsem aprova[cç][aã]o\b",
    r"\bqualquer\b", r"\btudo\b", r"\bsempre\b", r"\bmexer no runtime\b",
    r"\bdecidir sozinho\b",
]
QUESTION_BANK = {
    "root": "Isso resolve o problema raiz ou só adiciona uma camada bonita?",
    "breakage": "Qual comportamento atual pode quebrar se essa mudança entrar?",
    "small": "Existe um caminho menor, reversível e testável antes de mexer no runtime?",
    "scope": "Essa mudança pertence a esta SPEC ou merece SPEC filha?",
    "proof": "Qual prova mínima mostra que deu certo?",
}
LOG_PATH = Path(os.getenv("HERMES_SPEC_PUSHBACK_LOG", "/root/.hermes/logs/spec_change_pushback.jsonl"))


@dataclass(frozen=True)
class PushbackDecision:
    should_gate: bool
    decision: str
    questions: list[str]
    recommendation: str
    requires_override: bool
    override_detected: bool
    change_type: str = "unknown"
    target_surface: str = "whatsapp"
    created_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        if not data["created_at"]:
            data["created_at"] = datetime.now(timezone.utc).isoformat()
        return data


def _any(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_request(text: str) -> tuple[bool, bool, bool, bool]:
    normalized = (text or "").strip().lower()
    override = _any(OVERRIDE_PATTERNS, normalized)
    risky = _any(RISKY_SCOPE_PATTERNS, normalized)
    scoped = _any(GOOD_SCOPE_PATTERNS, normalized)

    # R7 is a safety gate, not a customer-support answer. It should only
    # interrupt when Vinicius is asking Hermes to change a technical surface.
    # Complaints, confusion and diagnosis requests must go to the normal LLM.
    complaint_or_clarification = _any(COMPLAINT_OR_CLARIFICATION_PATTERNS, normalized)
    action = _any(ACTION_PATTERNS, normalized)
    technical_target = _any(TECHNICAL_TARGET_PATTERNS, normalized)
    read_only = _any(SIMPLE_READ_PATTERNS, normalized) and not action

    should_gate = action and technical_target and not read_only and not complaint_or_clarification
    return should_gate, override, risky, scoped

def evaluate_pushback(request_text: str, *, change_type: str = "unknown", target_surface: str = "whatsapp") -> PushbackDecision:
    should_gate, override, risky, scoped = classify_request(request_text)
    now = datetime.now(timezone.utc).isoformat()
    if not should_gate:
        return PushbackDecision(False, "proceed", [], "Fluxo normal: pedido não parece mudança relevante de spec/runtime.", False, override, change_type, target_surface, now)
    if override:
        return PushbackDecision(True, "proceed", [QUESTION_BANK["proof"], QUESTION_BANK["breakage"]], "Override explícito detectado. Seguir e registrar que Vinicius assumiu a decisão.", False, True, change_type, target_surface, now)
    if risky and not scoped:
        return PushbackDecision(True, "block", [QUESTION_BANK["root"], QUESTION_BANK["breakage"], QUESTION_BANK["proof"]], "Não executar ainda. O pedido dá autonomia ampla demais sem prova mínima e sem limite de reversão.", True, False, change_type, target_surface, now)
    if scoped:
        return PushbackDecision(True, "proceed", [QUESTION_BANK["root"], QUESTION_BANK["breakage"], QUESTION_BANK["small"]], "Seguir com MVP pequeno, reversível e com smoke controlado.", False, False, change_type, target_surface, now)
    return PushbackDecision(True, "revise", [QUESTION_BANK["root"], QUESTION_BANK["scope"], QUESTION_BANK["proof"]], "Ajustar escopo antes de executar: falta dizer prova mínima e limite da mudança.", True, False, change_type, target_surface, now)


def format_pushback_message(decision: PushbackDecision) -> str | None:
    if decision.decision == "proceed" and not decision.requires_override:
        return None
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(decision.questions[:3], 1))
    return (
        "Pushback rápido antes de mexer em spec/runtime:\n\n"
        f"{questions}\n\n"
        f"Minha leitura: {decision.recommendation}\n\n"
        "Se quiser assumir o risco, responde: executa mesmo assim."
    )


def record_decision(decision: PushbackDecision, *, request_text: str, sender: str = "", chat_id: str = "") -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = decision.to_dict()
        payload.update({
            "sender": str(sender or ""),
            "chat_id": str(chat_id or ""),
            "request_excerpt": str(request_text or "")[:500],
        })
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("[spec_change_pushback] failed to record decision: %s", exc)
