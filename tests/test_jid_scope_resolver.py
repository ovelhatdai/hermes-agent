import asyncio
from datetime import date

from tools import gestor_trafego_ads as ads
from tools import jid_scope_resolver as resolver


def run(coro):
    return asyncio.run(coro)


class DummyResponse:
    def raise_for_status(self):
        return None


def test_resolve_scope_staff_fallback(monkeypatch):
    monkeypatch.setattr(resolver, "_fetch_scope_from_db", lambda jid: None)
    monkeypatch.setattr(resolver, "_alert_unknown_jid", lambda *args, **kwargs: False)

    scope = resolver.resolve_scope("5551991987972@s.whatsapp.net")

    assert scope["person_type"] == "staff"
    assert scope["allowed_groups"] == ["*"]
    assert scope["normalized_jid"] == "5551991987972"


def test_resolve_scope_mentorada_from_jid_scope(monkeypatch):
    calls = []

    def fake_fetch(jid):
        calls.append(jid)
        return {
            "person_type": "mentorada",
            "allowed_groups": ["mentorada-camila-tonello"],
            "block": False,
            "alerta_vini": False,
            "person_name": "Camila Tonello",
            "source": "ads.jid_scope",
        }

    monkeypatch.setattr(resolver, "_fetch_scope_from_db", fake_fetch)

    scope = resolver.resolve_scope("5551999990000@lid")

    assert calls == ["5551999990000"]
    assert scope["person_type"] == "mentorada"
    assert scope["allowed_groups"] == ["mentorada-camila-tonello"]
    assert scope["source"] == "ads.jid_scope"


def test_resolve_scope_comparative_blocks_mentorada(monkeypatch):
    monkeypatch.setattr(resolver, "_fetch_scope_from_db", lambda jid: {
        "person_type": "mentorada",
        "allowed_groups": ["mentorada-camila-tonello"],
        "block": False,
        "alerta_vini": False,
        "person_name": "Camila Tonello",
    })

    scope = resolver.resolve_scope("5551999990000", "Como tô comparada com a Luciana?")

    assert scope["block_comparative"] is True


def test_resolve_scope_unknown_alert_rate_limited(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(resolver, "_fetch_scope_from_db", lambda jid: None)
    monkeypatch.setattr(resolver, "ALERT_CACHE_PATH", tmp_path / "scope_alerts.json")
    monkeypatch.setenv("HERMES_BRIDGE_SEND_URL", "http://127.0.0.1:3000/send")

    def fake_post(url, json, timeout):
        sent.append({"url": url, "json": json, "timeout": timeout})
        return DummyResponse()

    monkeypatch.setattr(resolver.requests, "post", fake_post)

    first = resolver.resolve_scope("5511999999999", "CAC do DES")
    second = resolver.resolve_scope("5511999999999", "CAC do DES de novo")

    assert first["person_type"] == "unknown"
    assert first["block"] is True
    assert first["alert_sent"] is True
    assert second["block"] is True
    assert second["alert_sent"] is False
    assert len(sent) == 1
    assert sent[0]["json"]["to"] == "5551991987972"
    assert "5511999999999" in sent[0]["json"]["text"]


def test_get_ads_historico_returns_literal_comparative_block(monkeypatch):
    async def fake_scope(jid, original_prompt="", conn=None):
        return {
            "person_type": "mentorada",
            "allowed_groups": ["mentorada-camila-tonello"],
            "block": False,
            "block_comparative": True,
        }

    monkeypatch.setattr(ads, "resolve_scope_async", fake_scope)

    result = run(ads._get_ads_historico_core(
        conn=object(),
        group_key="mentorada-camila-tonello",
        start=date(2026, 5, 1),
        end=date(2026, 5, 7),
        requesting_jid="5551999990000",
        original_prompt="Como tô comparada com a Luciana?",
    ))

    assert result["erro"] == "scope_comparativo_bloqueado"
    assert result["mensagem_para_usuario"] == resolver.RESPOSTA_BLOQUEIO_COMPARATIVO


def test_get_ads_historico_denies_mentorada_other_group(monkeypatch):
    async def fake_scope(jid, original_prompt="", conn=None):
        return {
            "person_type": "mentorada",
            "allowed_groups": ["mentorada-camila-tonello"],
            "block": False,
            "block_comparative": False,
        }

    monkeypatch.setattr(ads, "resolve_scope_async", fake_scope)

    result = run(ads._get_ads_historico_core(
        conn=object(),
        group_key="mentorada-luciana",
        start=date(2026, 5, 1),
        end=date(2026, 5, 7),
        requesting_jid="5551999990000",
    ))

    assert result["erro"] == "scope_denied"
    assert result["success"] is False
