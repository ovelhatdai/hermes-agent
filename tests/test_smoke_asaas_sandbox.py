from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_asaas_sandbox.py"
    spec = importlib.util.spec_from_file_location("smoke_asaas_sandbox", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_smoke_passes_required_checks_and_skips_create_payment_without_api_key():
    module = _load_module()
    calls = []
    responses = [
        (200, '{"status":"ok","platform":"hermes-agent"}', {"status": "ok", "platform": "hermes-agent"}),
        (401, '{"ok":false,"error":"missing_asaas_access_token"}', {"ok": False, "error": "missing_asaas_access_token"}),
        (200, '{"ok":true,"duplicate":false}', {"ok": True, "duplicate": False}),
        (200, '{"ok":true,"duplicate":true}', {"ok": True, "duplicate": True}),
    ]

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses.pop(0)

    results = module.run_smoke(
        base_url="http://127.0.0.1:8642",
        timeout=3.0,
        request_json=fake_request,
        environ={"ASAAS_WEBHOOK_TOKEN": "hooktoken"},
    )

    assert [result.status for result in results] == ["PASS", "PASS", "PASS", "PASS", "SKIP"]
    assert results[-1].message == "ASAAS_API_KEY ausente; create-payment sandbox foi pulado"
    assert calls[2][2]["headers"]["asaas-access-token"] == "hooktoken"


def test_run_smoke_executes_create_payment_when_required_env_is_present():
    module = _load_module()
    responses = [
        (200, '{"status":"ok","platform":"hermes-agent"}', {"status": "ok", "platform": "hermes-agent"}),
        (401, '{"ok":false,"error":"missing_asaas_access_token"}', {"ok": False, "error": "missing_asaas_access_token"}),
        (200, '{"ok":true,"duplicate":false}', {"ok": True, "duplicate": False}),
        (200, '{"ok":true,"duplicate":true}', {"ok": True, "duplicate": True}),
        (
            200,
            '{"invoice_url":"https://sandbox.asaas.com/i/pay_123","payment_id":"pay_123","due_date":"2026-04-29","asaas_customer_id":"cus_123"}',
            {
                "invoice_url": "https://sandbox.asaas.com/i/pay_123",
                "payment_id": "pay_123",
                "due_date": "2026-04-29",
                "asaas_customer_id": "cus_123",
            },
        ),
    ]
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses.pop(0)

    results = module.run_smoke(
        base_url="http://127.0.0.1:8642",
        timeout=3.0,
        request_json=fake_request,
        environ={
            "ASAAS_WEBHOOK_TOKEN": "hooktoken",
            "ASAAS_API_KEY": "api-key",
            "HERMES_GATEWAY_TOKEN": "gateway-token",
        },
    )

    assert results[-1].status == "PASS"
    assert calls[-1][2]["headers"]["Authorization"] == "Bearer gateway-token"
    assert calls[-1][2]["payload"]["sku"] == "TAG_DUPLA"


def test_run_smoke_fails_when_runtime_route_is_not_ready():
    module = _load_module()
    responses = [
        (200, '{"status":"ok","platform":"hermes-agent"}', {"status": "ok", "platform": "hermes-agent"}),
        (404, "404: Not Found", None),
        (404, "404: Not Found", None),
        (404, "404: Not Found", None),
    ]

    def fake_request(method, url, **kwargs):
        return responses.pop(0)

    results = module.run_smoke(
        base_url="http://127.0.0.1:8642",
        timeout=3.0,
        request_json=fake_request,
        environ={"ASAAS_WEBHOOK_TOKEN": "hooktoken"},
    )

    assert results[0].status == "PASS"
    assert results[1].status == "FAIL"
    assert results[2].status == "FAIL"
    assert results[3].status == "FAIL"
    assert module.print_report(results, base_url="http://127.0.0.1:8642") == 1
