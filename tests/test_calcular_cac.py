import asyncio
import json
from datetime import date
from decimal import Decimal

from tools import gestor_trafego_ads as ads


def run(coro):
    return asyncio.run(coro)


def decode(raw):
    return json.loads(raw)


class FakeConn:
    def __init__(self, period_input=None, asaas_count=None):
        self.period_input = period_input
        self.asaas_count = asaas_count
        self.executed = []

    async def fetchrow(self, query, *args):
        if "FROM ads.account_groups" in query:
            return {"business_key": "des" if args[0] == "des" else "mentorada"}
        if "FROM ads.period_inputs" in query:
            return self.period_input
        return None

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return self.asaas_count is not None
        if "public.asaas_event_log" in query:
            return self.asaas_count
        return None

    async def execute(self, *args):
        self.executed.append(args)

    async def close(self):
        pass


def patch_connect(monkeypatch, conn):
    async def fake_connect():
        return conn

    monkeypatch.setattr(ads, "_connect", fake_connect)


def patch_historico(monkeypatch, payload):
    calls = []

    async def fake_get_ads_historico(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(ads, "get_ads_historico", fake_get_ads_historico)
    return calls


def historico_ok(spend=Decimal("6482.83"), leads=2775, business_key="des"):
    return {
        "success": True,
        "group_key": "des",
        "business_key": business_key,
        "periodo": {"inicio": date(2026, 5, 1), "fim": date(2026, 5, 12)},
        "gasto_total": spend,
        "leads_total": leads,
        "impressions_total": 0,
        "clicks_total": 0,
        "fonte": "ads.daily_insights",
    }


def test_cac_des_com_parametro_manual_chama_get_ads_historico(monkeypatch):
    conn = FakeConn()
    patch_connect(monkeypatch, conn)
    calls = patch_historico(monkeypatch, historico_ok())

    result = decode(run(ads._calcular_cac({
        "group_key": "des",
        "data_inicio": "2026-05-01",
        "data_fim": "2026-05-12",
        "contratos": 35,
        "requesting_jid": "5551989150954",
        "dry_run": True,
    })))

    assert calls == [{
        "group_key": "des",
        "data_inicio": "2026-05-01",
        "data_fim": "2026-05-12",
        "requesting_jid": "5551989150954",
    }]
    assert result["success"] is True
    assert result["fonte_gasto"] == "ads.daily_insights"
    assert result["fonte_contratos"] == "manual_param"
    assert result["gasto_total"] == 6482.83
    assert result["leads_total"] == 2775
    assert result["contratos"] == 35
    assert result["cac"] == 185.22
    assert result["cpl"] == 2.34
    assert result["conversao_pct"] == 1.26


def test_cobertura_parcial_bloqueia_antes_de_contratos(monkeypatch):
    patch_historico(monkeypatch, {
        **historico_ok(spend=Decimal("500.00"), leads=100),
        "coverage_parcial": {
            "contas_com_dado": 6,
            "contas_esperadas": 7,
            "contas_faltantes": ["Conta UNSETTLED"],
        },
    })

    result = decode(run(ads._calcular_cac({
        "group_key": "des",
        "data_inicio": "2026-05-01",
        "data_fim": "2026-05-12",
        "contratos": 35,
        "dry_run": True,
    })))

    assert result["success"] is False
    assert result["erro"] == "coverage_parcial"
    assert result["gasto_parcial"] == 500.0
    assert result["contas_faltantes"] == ["Conta UNSETTLED"]
    assert "Quer que eu calcule com as disponíveis ou aguardo?" in result["mensagem_para_usuario"]


def test_mentorada_sem_period_inputs_e_sem_asaas_retorna_contratos_ausentes(monkeypatch):
    patch_connect(monkeypatch, FakeConn(asaas_count=None))
    patch_historico(monkeypatch, historico_ok(spend=Decimal("100.00"), leads=20, business_key="mentorada"))

    result = decode(run(ads._calcular_cac({
        "group_key": "mentorada-camila-tonello",
        "data_inicio": "2026-05-01",
        "data_fim": "2026-05-12",
        "dry_run": True,
    })))

    assert result["success"] is False
    assert result["erro"] == "contratos_ausentes"
    assert result["fonte_contratos"] == "missing"
    assert "Quantos foram?" in result["mensagem_para_usuario"]


def test_contratos_zero_retorna_erro_causal(monkeypatch):
    patch_historico(monkeypatch, historico_ok())

    result = decode(run(ads._calcular_cac({
        "group_key": "des",
        "data_inicio": "2026-05-01",
        "data_fim": "2026-05-12",
        "contratos": 0,
        "dry_run": True,
    })))

    assert result["success"] is False
    assert result["erro"] == "contratos_zero"
    assert result["fonte_contratos"] == "manual_param"
    assert "maior que zero" in result["mensagem_para_usuario"]
