import asyncio
import json
from datetime import date
from decimal import Decimal

from tools import gestor_trafego_ads as ads


class FakeConn:
    def __init__(self, expected=None, current=None, previous=None):
        self.expected = expected or []
        self.current = current or []
        self.previous = previous or []
        self.executed = []

    async def fetchval(self, query, *args):
        return False

    async def fetch(self, query, *args):
        if "FROM ads.account_group_members" in query and "ads.daily_insights" not in query:
            return self.expected
        if "FROM ads.daily_insights" in query:
            start = args[1]
            if start < date(2026, 5, 8):
                return self.previous
            return self.current
        return []

    async def execute(self, *args):
        self.executed.append(args)

    async def close(self):
        pass


def run(coro):
    return asyncio.run(coro)


def decode(raw):
    return json.loads(raw)


def patch_connect(monkeypatch, conn):
    async def fake_connect():
        return conn

    async def fake_scope(jid, original_prompt="", conn=None):
        return {
            "person_type": "staff",
            "allowed_groups": ["*"],
            "block": False,
            "block_comparative": False,
        }

    monkeypatch.setattr(ads, "_connect", fake_connect)
    monkeypatch.setattr(ads, "resolve_scope_async", fake_scope)


def account(aid, name, business_key="des"):
    return {"ad_account_id": aid, "account_name": name, "business_key": business_key}


def row(aid, name, day, spend, leads, impressions=1000, clicks=100, business_key="des"):
    return {
        "group_key": "des",
        "business_key": business_key,
        "ad_account_id": aid,
        "account_name": name,
        "date": date.fromisoformat(day),
        "spend": Decimal(str(spend)),
        "leads": leads,
        "impressions": impressions,
        "clicks": clicks,
    }


def test_periodo_valido_com_dados_completos(monkeypatch):
    conn = FakeConn(
        expected=[account("act_1", "Conta 1"), account("act_2", "Conta 2")],
        current=[
            row("act_1", "Conta 1", "2026-05-08", 100, 10),
            row("act_2", "Conta 2", "2026-05-08", 200, 20),
        ],
    )
    patch_connect(monkeypatch, conn)

    result = decode(run(ads._get_ads_historico({"group_key": "des", "data_inicio": "2026-05-08", "data_fim": "2026-05-08"})))

    assert result["success"] is True
    assert result["fonte"] == "ads.daily_insights"
    assert result["gasto_total"] == 300.0
    assert result["leads_total"] == 30
    assert result["cpl_medio"] == 10.0
    assert "coverage_parcial" not in result


def test_periodo_sem_dado_retorna_zero_e_coverage_parcial(monkeypatch):
    conn = FakeConn(expected=[account("act_1", "Conta 1"), account("act_2", "Conta 2")], current=[])
    patch_connect(monkeypatch, conn)

    result = decode(run(ads._get_ads_historico({"group_key": "des", "data_inicio": "2026-05-08", "data_fim": "2026-05-08"})))

    assert result["success"] is True
    assert result["gasto_total"] == 0.0
    assert result["leads_total"] == 0
    assert result["coverage_parcial"] == {
        "contas_com_dado": 0,
        "contas_esperadas": 2,
        "contas_faltantes": ["Conta 1", "Conta 2"],
    }


def test_comparar_com_periodo_anterior_calcula_delta(monkeypatch):
    conn = FakeConn(
        expected=[account("act_1", "Conta 1")],
        current=[row("act_1", "Conta 1", "2026-05-08", 200, 20)],
        previous=[row("act_1", "Conta 1", "2026-05-01", 100, 25)],
    )
    patch_connect(monkeypatch, conn)

    result = decode(run(ads._get_ads_historico({
        "group_key": "des",
        "data_inicio": "2026-05-08",
        "data_fim": "2026-05-08",
        "comparar_com": "periodo_anterior",
    })))

    assert result["delta_vs_anterior"]["gasto_pct"] == 100.0
    assert result["delta_vs_anterior"]["leads_pct"] == -20.0
    assert result["delta_vs_anterior"]["periodo_anterior"] == {"inicio": "2026-05-07", "fim": "2026-05-07"}


def test_group_key_inexistente_retorna_erro_causal(monkeypatch):
    conn = FakeConn(expected=[], current=[])
    patch_connect(monkeypatch, conn)

    result = decode(run(ads._get_ads_historico({"group_key": "nao_existe", "data_inicio": "2026-05-08", "data_fim": "2026-05-08"})))

    assert result["success"] is False
    assert result["erro"] == "group_not_found"
    assert "Nenhuma conta ativa" in result["motivo"]
