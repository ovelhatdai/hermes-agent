#!/usr/bin/env python3
"""Hermes tools for the SPEC-111 encrypted family vault."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from tools.registry import registry

AUTHORIZED_DEFAULTS = {"5551991987972", "5551984213925"}
ALLOWED_FIELDS = {
    "cpf",
    "rg",
    "nome_completo",
    "endereco",
    "email",
    "telefone",
    "profissao",
    "passaporte",
    "nacionalidade",
}
FORBIDDEN_PATTERN = re.compile(
    r"\b(banco|bancario|bancaria|cartao|credito|senha|password|cvv|cvc|agencia|conta\s+corrente)\b",
    re.IGNORECASE,
)
DEFAULT_DSN = "postgresql://postgres@127.0.0.1:5432/hermes"
DEFAULT_SECRET_FILE = "/etc/secrets/familia-cofre.key"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and not digits.startswith("55"):
        digits = "55" + digits
    return digits


def _session_phone() -> str:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return ""
    for key in ("HERMES_SESSION_PHONE", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_USER_ID"):
        phone = _normalize_phone(get_session_env(key, ""))
        if phone:
            return phone
    return ""


def _consultante(args: dict[str, Any]) -> str:
    return _normalize_phone(args.get("consultante_telefone")) or _session_phone()


def _dsn() -> str:
    return (
        os.getenv("FAMILIA_COFRE_DATABASE_URL")
        or os.getenv("HERMES_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or DEFAULT_DSN
    )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(dsn=_dsn())


def _secret() -> str:
    path = Path(os.getenv("FAMILIA_COFRE_SECRET_FILE", DEFAULT_SECRET_FILE))
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("familia_cofre_secret_empty")
    return value


def _parse_fields(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = re.split(r"[,;\n]", raw)
    elif isinstance(raw, Iterable):
        items = list(raw)
    else:
        items = [raw]
    fields: list[str] = []
    for item in items:
        field = str(item or "").strip().lower()
        if not field:
            continue
        if field == "nome":
            field = "nome_completo"
        if field not in ALLOWED_FIELDS:
            raise ValueError(f"campo_nao_permitido:{field}")
        if field not in fields:
            fields.append(field)
    return fields


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    raise ValueError("metadados_devem_ser_json_objeto")


def _reject_forbidden(campo: str, valor: str, metadados: dict[str, Any]) -> str | None:
    probe = " ".join([campo, valor, json.dumps(metadados, ensure_ascii=False)])
    if FORBIDDEN_PATTERN.search(probe):
        return "dados_bancarios_cartao_senhas_nao_sao_permitidos"
    return None


def _clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return without_marks.lower()


async def _audit_denied(conn: asyncpg.Connection, telefone: str, campos: list[str] | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO familia_cofre.audit(consultante_telefone, membro_id, campos, acao)
        VALUES ($1, NULL, $2::text[], 'denied')
        """,
        telefone or "desconhecido",
        campos or [],
    )


async def _familia_listar_membros(args: dict[str, Any], **_: Any) -> str:
    telefone = _consultante(args)
    if not telefone:
        return _json({"success": False, "error": "consultante_telefone_obrigatorio"})
    conn = await _connect()
    try:
        autorizado = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM familia_cofre.membros WHERE $1 = ANY(responsavel_telefones))",
            telefone,
        )
        if not autorizado:
            await _audit_denied(conn, telefone, ["membros"])
            return _json({"success": False, "error": "acesso_negado", "alert_required": True})
        rows = await conn.fetch(
            """
            SELECT
              id,
              nome_publico,
              parentesco,
              data_nascimento,
              CASE WHEN data_nascimento IS NULL THEN NULL ELSE EXTRACT(YEAR FROM age(current_date, data_nascimento))::int END AS idade_anos,
              CASE WHEN data_nascimento IS NULL THEN NULL ELSE EXTRACT(MONTH FROM age(current_date, data_nascimento))::int END AS idade_meses
            FROM familia_cofre.membros
            ORDER BY id
            """
        )
        await conn.execute(
            "INSERT INTO familia_cofre.audit(consultante_telefone, membro_id, campos, acao) VALUES ($1, NULL, $2::text[], 'list')",
            telefone,
            ["membros"],
        )
        membros = []
        for row in rows:
            membros.append(
                {
                    "id": row["id"],
                    "nome_publico": row["nome_publico"],
                    "parentesco": row["parentesco"],
                    "data_nascimento": row["data_nascimento"].isoformat() if row["data_nascimento"] else None,
                    "idade_anos": row["idade_anos"],
                    "idade_meses": row["idade_meses"],
                }
            )
        return _json({"success": True, "membros": membros})
    finally:
        await conn.close()


async def _familia_get_dados(args: dict[str, Any], **_: Any) -> str:
    telefone = _consultante(args)
    if not telefone:
        return _json({"success": False, "error": "consultante_telefone_obrigatorio"})
    try:
        membro_id = int(args.get("membro_id"))
        campos = _parse_fields(args.get("campos"))
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})
    if not campos:
        return _json({"success": False, "error": "campos_obrigatorios"})
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "SELECT campo, valor FROM familia_cofre.get_dados($1, $2, $3::text[], $4)",
            telefone,
            membro_id,
            campos,
            _secret(),
        )
        dados = {row["campo"]: row["valor"] for row in rows}
        return _json(
            {
                "success": True,
                "membro_id": membro_id,
                "dados": dados,
                "campos_encontrados": sorted(dados),
                "campos_vazios": [campo for campo in campos if campo not in dados],
            }
        )
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", "") == "42501":
            return _json({"success": False, "error": "acesso_negado", "alert_required": True})
        return _json({"success": False, "error": "postgres_error"})
    finally:
        await conn.close()


async def _familia_atualizar_dado(args: dict[str, Any], **_: Any) -> str:
    telefone = _consultante(args)
    if not telefone:
        return _json({"success": False, "error": "consultante_telefone_obrigatorio"})
    try:
        membro_id = int(args.get("membro_id"))
        campo = _parse_fields([args.get("campo")])[0]
        valor = str(args.get("valor") or "").strip()
        metadados = _parse_metadata(args.get("metadados"))
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})
    if not valor:
        return _json({"success": False, "error": "valor_obrigatorio"})
    forbidden = _reject_forbidden(campo, valor, metadados)
    if forbidden:
        return _json({"success": False, "error": forbidden})
    conn = await _connect()
    try:
        await conn.execute(
            "SELECT familia_cofre.set_dado($1, $2, $3, $4, $5::jsonb, $6)",
            telefone,
            membro_id,
            campo,
            valor,
            json.dumps(metadados, ensure_ascii=False),
            _secret(),
        )
        return _json({"success": True, "membro_id": membro_id, "campo": campo})
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", "") == "42501":
            return _json({"success": False, "error": "acesso_negado", "alert_required": True})
        return _json({"success": False, "error": "postgres_error"})
    finally:
        await conn.close()


def _detect_fields(template: str) -> list[str]:
    text = _clean_text(template)
    pairs = [
        ("nome_completo", ("nome completo", "nome:", "nome ")),
        ("cpf", ("cpf",)),
        ("rg", ("rg", "identidade")),
        ("email", ("email", "e-mail")),
        ("telefone", ("telefone", "celular")),
        ("endereco", ("endereco", "endereço")),
        ("profissao", ("profissao", "profissão")),
        ("passaporte", ("passaporte",)),
        ("nacionalidade", ("nacionalidade",)),
    ]
    fields = [field for field, needles in pairs if any(needle in text for needle in needles)]
    if "nasc" in text or "data de nascimento" in text or "data nascimento" in text:
        fields.append("data_nascimento")
    if not fields:
        fields = ["nome_completo", "cpf", "data_nascimento"]
    return list(dict.fromkeys(fields))


def _detect_member_ids(template: str, telefone: str, membros: list[dict[str, Any]]) -> list[int]:
    text = _clean_text(template)
    selected: list[int] = []
    by_name = {_clean_text(m["nome_publico"]): m for m in membros}
    for key, member in by_name.items():
        if key and key in text:
            selected.append(member["id"])
    if any(word in text for word in ("mim", "meu", "minha", "titular")):
        target = "Daiane" if telefone == "5551984213925" else "Vinicius"
        member = by_name.get(_clean_text(target))
        if member:
            selected.insert(0, member["id"])
    if "esposa" in text:
        member = by_name.get("daiane")
        if member:
            selected.append(member["id"])
    deduped: list[int] = []
    for member_id in selected:
        if member_id not in deduped:
            deduped.append(member_id)
    return deduped


async def _familia_preencher_formulario(args: dict[str, Any], **_: Any) -> str:
    telefone = _consultante(args)
    template = str(args.get("template_texto") or "").strip()
    if not telefone:
        return _json({"success": False, "error": "consultante_telefone_obrigatorio"})
    if not template:
        return _json({"success": False, "error": "template_texto_obrigatorio"})
    conn = await _connect()
    try:
        autorizado = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM familia_cofre.membros WHERE $1 = ANY(responsavel_telefones))",
            telefone,
        )
        if not autorizado:
            await _audit_denied(conn, telefone, ["formulario"])
            return _json({"success": False, "error": "acesso_negado", "alert_required": True})
        rows = await conn.fetch(
            "SELECT id, nome_publico, parentesco, data_nascimento FROM familia_cofre.membros ORDER BY id"
        )
        membros = [dict(row) for row in rows]
        member_ids = _detect_member_ids(template, telefone, membros)
        if not member_ids:
            return _json({"success": False, "error": "membros_nao_detectados", "membros_disponiveis": membros})
        requested_fields = _detect_fields(template)
        encrypted_fields = [field for field in requested_fields if field in ALLOWED_FIELDS]
        secret = _secret()
        by_id = {m["id"]: m for m in membros}
        blocos = []
        for member_id in member_ids:
            member = by_id[member_id]
            dados = {}
            if encrypted_fields:
                data_rows = await conn.fetch(
                    "SELECT campo, valor FROM familia_cofre.get_dados($1, $2, $3::text[], $4)",
                    telefone,
                    member_id,
                    encrypted_fields,
                    secret,
                )
                dados.update({row["campo"]: row["valor"] for row in data_rows})
            if "data_nascimento" in requested_fields:
                dados["data_nascimento"] = member["data_nascimento"].isoformat() if member["data_nascimento"] else None
            linhas = [f"{member['nome_publico']} ({member['parentesco']}):"]
            for field in requested_fields:
                valor = dados.get(field) or "[pendente]"
                linhas.append(f"- {field}: {valor}")
            blocos.append({"membro_id": member_id, "nome_publico": member["nome_publico"], "dados": dados, "preview": "\n".join(linhas)})
        filled_template = template.rstrip() + "\n\nDados para preenchimento:\n" + "\n\n".join(b["preview"] for b in blocos)
        return _json({"success": True, "campos": requested_fields, "membros": blocos, "filled_template": filled_template})
    except asyncpg.PostgresError as exc:
        if getattr(exc, "sqlstate", "") == "42501":
            return _json({"success": False, "error": "acesso_negado", "alert_required": True})
        return _json({"success": False, "error": "postgres_error"})
    finally:
        await conn.close()


def _check_requirements() -> bool:
    return Path(os.getenv("FAMILIA_COFRE_SECRET_FILE", DEFAULT_SECRET_FILE)).exists()


LISTAR_SCHEMA = {
    "name": "familia_listar_membros",
    "description": "Lista os membros publicos do Cofre Familia e registra audit log. Restrito a Vinicius e Daiane.",
    "parameters": {
        "type": "object",
        "properties": {
            "consultante_telefone": {"type": "string", "description": "Telefone WhatsApp do solicitante. Se omitido, usa a origem da sessao."}
        },
        "required": [],
    },
}

GET_SCHEMA = {
    "name": "familia_get_dados",
    "description": "Le dados cifrados de um membro do Cofre Familia via pgcrypto, com audit log automatico. Nunca retorna secret.",
    "parameters": {
        "type": "object",
        "properties": {
            "consultante_telefone": {"type": "string", "description": "Telefone WhatsApp do solicitante. Restrito a Vini/Daiane."},
            "membro_id": {"type": "integer", "description": "ID do membro em familia_cofre.membros."},
            "campos": {"type": "array", "items": {"type": "string"}, "description": "Campos permitidos: cpf, rg, nome_completo, endereco, email, telefone, profissao, passaporte, nacionalidade."},
        },
        "required": ["membro_id", "campos"],
    },
}

SET_SCHEMA = {
    "name": "familia_atualizar_dado",
    "description": "Salva ou atualiza um dado pessoal cifrado no Cofre Familia apos confirmacao campo a campo por WhatsApp. Bloqueia banco, cartao e senhas.",
    "parameters": {
        "type": "object",
        "properties": {
            "consultante_telefone": {"type": "string", "description": "Telefone WhatsApp do solicitante. Restrito a Vini/Daiane."},
            "membro_id": {"type": "integer"},
            "campo": {"type": "string", "description": "Campo permitido do cofre."},
            "valor": {"type": "string", "description": "Valor confirmado pelo Vini/Daiane. Nao usar para banco, cartao ou senha."},
            "metadados": {"type": "object", "description": "Metadados opcionais, ex: orgao_emissor RG ou validade passaporte."},
        },
        "required": ["membro_id", "campo", "valor"],
    },
}

PREENCHER_SCHEMA = {
    "name": "familia_preencher_formulario",
    "description": "Monta um preview de preenchimento de formulario com dados do Cofre Familia, marcando campos pendentes quando ainda nao cadastrados.",
    "parameters": {
        "type": "object",
        "properties": {
            "consultante_telefone": {"type": "string", "description": "Telefone WhatsApp do solicitante. Restrito a Vini/Daiane."},
            "template_texto": {"type": "string", "description": "Texto do formulario ou pedido colado pelo usuario."},
        },
        "required": ["template_texto"],
    },
}

registry.register(
    name="familia_listar_membros",
    toolset="familia",
    schema=LISTAR_SCHEMA,
    handler=_familia_listar_membros,
    check_fn=_check_requirements,
    is_async=True,
    emoji="🔐",
)
registry.register(
    name="familia_get_dados",
    toolset="familia",
    schema=GET_SCHEMA,
    handler=_familia_get_dados,
    check_fn=_check_requirements,
    is_async=True,
    emoji="🔐",
)
registry.register(
    name="familia_atualizar_dado",
    toolset="familia",
    schema=SET_SCHEMA,
    handler=_familia_atualizar_dado,
    check_fn=_check_requirements,
    is_async=True,
    emoji="🔐",
)
registry.register(
    name="familia_preencher_formulario",
    toolset="familia",
    schema=PREENCHER_SCHEMA,
    handler=_familia_preencher_formulario,
    check_fn=_check_requirements,
    is_async=True,
    emoji="🔐",
)
