#!/usr/bin/env python3
"""SPEC-093 sandbox smoke harness for Hermes Asaas endpoints."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_BASE_URL = "http://127.0.0.1:8642"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_ENV_FILES = (
    "/root/.hermes/.env",
    "/etc/hermes/media-dispatch.env",
    "/etc/hermes/advogandodash.env",
)


@dataclass(slots=True)
class StepResult:
    name: str
    status: str
    required: bool
    message: str
    http_status: int | None = None
    body: str | None = None


RequestFn = Callable[..., tuple[int, str, Any | None]]


def _compact_body(body: str | None, *, limit: int = 180) -> str:
    if not body:
        return ""
    normalized = " ".join(body.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."


def _strip_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    quote_chars = {chr(34), chr(39)}
    if len(value) >= 2 and value[0] == value[-1] and value[0] in quote_chars:
        return value[1:-1]
    return value


def load_default_env_files() -> list[str]:
    loaded_files: list[str] = []
    for env_path in DEFAULT_ENV_FILES:
        path = Path(env_path)
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = _strip_env_value(value)
        loaded_files.append(str(path))
    return loaded_files


def _build_webhook_payload(event_id: str, payment_id: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "event": "PAYMENT_CREATED",
        "payment": {
            "id": payment_id,
            "value": 100.0,
            "customer": f"cus_{payment_id}",
        },
    }


def _build_create_payment_payload(stamp: int) -> dict[str, Any]:
    return {
        "sku": "TAG_DUPLA",
        "customer": {
            "name": f"Smoke SPEC093 {stamp}",
            "cpfCnpj": "12345678900",
            "email": f"spec093.smoke.{stamp}@example.com",
            "phone": "5511999998888",
        },
        "agent": "clara_sdr",
        "conv_id": f"spec093-smoke-{stamp}",
        "lead_phone": "5511999998888",
        "installments": 12,
    }


def _request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str, Any | None]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    data = None
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body_bytes = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request_failed:{exc.reason}") from exc

    body = body_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    return status, body, parsed


def _build_result(
    name: str,
    *,
    status: str,
    required: bool,
    message: str,
    http_status: int | None = None,
    body: str | None = None,
) -> StepResult:
    return StepResult(
        name=name,
        status=status,
        required=required,
        message=message,
        http_status=http_status,
        body=_compact_body(body),
    )


def run_smoke(
    *,
    base_url: str,
    timeout: float,
    request_json: RequestFn = _request_json,
    environ: Mapping[str, str] | None = None,
    skip_create_payment: bool = False,
) -> list[StepResult]:
    env = environ if environ is not None else os.environ
    base_url = base_url.rstrip("/")

    results: list[StepResult] = []
    health_url = f"{base_url}/health"
    webhook_url = f"{base_url}/asaas/webhook"
    create_payment_url = f"{base_url}/api/gateway/asaas/create-payment"

    try:
        status, body, parsed = request_json("GET", health_url, timeout=timeout)
        if status == 200 and isinstance(parsed, dict) and parsed.get("status") == "ok":
            platform = parsed.get("platform") or "unknown"
            results.append(
                _build_result(
                    "health",
                    status="PASS",
                    required=True,
                    message=f"health respondeu 200 e status=ok (platform={platform})",
                    http_status=status,
                    body=body,
                )
            )
        else:
            results.append(
                _build_result(
                    "health",
                    status="FAIL",
                    required=True,
                    message="health nao retornou o payload esperado",
                    http_status=status,
                    body=body,
                )
            )
    except RuntimeError as exc:
        results.append(_build_result("health", status="FAIL", required=True, message=str(exc)))

    probe_suffix = str(int(time.time()))
    webhook_payload = _build_webhook_payload(f"evt_smoke_{probe_suffix}", f"pay_smoke_{probe_suffix}")

    try:
        status, body, parsed = request_json("POST", webhook_url, payload=webhook_payload, timeout=timeout)
        if status == 401 and isinstance(parsed, dict) and parsed.get("error") == "missing_asaas_access_token":
            results.append(
                _build_result(
                    "webhook_missing_token",
                    status="PASS",
                    required=True,
                    message="webhook sem token retornou 401 missing_asaas_access_token",
                    http_status=status,
                    body=body,
                )
            )
        else:
            results.append(
                _build_result(
                    "webhook_missing_token",
                    status="FAIL",
                    required=True,
                    message="webhook sem token nao retornou 401 missing_asaas_access_token",
                    http_status=status,
                    body=body,
                )
            )
    except RuntimeError as exc:
        results.append(_build_result("webhook_missing_token", status="FAIL", required=True, message=str(exc)))

    webhook_token = (env.get("ASAAS_WEBHOOK_TOKEN") or "").strip()
    if not webhook_token:
        results.append(
            _build_result(
                "webhook_valid_token",
                status="FAIL",
                required=True,
                message="ASAAS_WEBHOOK_TOKEN ausente no ambiente; rode o smoke com os envs carregados",
            )
        )
        results.append(
            _build_result(
                "webhook_duplicate",
                status="FAIL",
                required=True,
                message="duplicate check bloqueado porque ASAAS_WEBHOOK_TOKEN esta ausente",
            )
        )
    else:
        headers = {"asaas-access-token": webhook_token}
        try:
            status, body, parsed = request_json("POST", webhook_url, headers=headers, payload=webhook_payload, timeout=timeout)
            if status == 200 and isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("duplicate") is False:
                results.append(
                    _build_result(
                        "webhook_valid_token",
                        status="PASS",
                        required=True,
                        message="webhook com token valido retornou 200 duplicate=false",
                        http_status=status,
                        body=body,
                    )
                )
            else:
                results.append(
                    _build_result(
                        "webhook_valid_token",
                        status="FAIL",
                        required=True,
                        message="webhook com token valido nao retornou 200 duplicate=false",
                        http_status=status,
                        body=body,
                    )
                )
        except RuntimeError as exc:
            results.append(_build_result("webhook_valid_token", status="FAIL", required=True, message=str(exc)))

        try:
            status, body, parsed = request_json("POST", webhook_url, headers=headers, payload=webhook_payload, timeout=timeout)
            if status == 200 and isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("duplicate") is True:
                results.append(
                    _build_result(
                        "webhook_duplicate",
                        status="PASS",
                        required=True,
                        message="duplicate webhook retornou 200 duplicate=true",
                        http_status=status,
                        body=body,
                    )
                )
            else:
                results.append(
                    _build_result(
                        "webhook_duplicate",
                        status="FAIL",
                        required=True,
                        message="duplicate webhook nao retornou 200 duplicate=true",
                        http_status=status,
                        body=body,
                    )
                )
        except RuntimeError as exc:
            results.append(_build_result("webhook_duplicate", status="FAIL", required=True, message=str(exc)))

    api_key = (env.get("ASAAS_API_KEY") or "").strip()
    gateway_token = (env.get("HERMES_GATEWAY_TOKEN") or "").strip()
    if skip_create_payment:
        results.append(_build_result("create_payment", status="SKIP", required=False, message="skip solicitado via CLI"))
    elif not api_key:
        results.append(
            _build_result(
                "create_payment",
                status="SKIP",
                required=False,
                message="ASAAS_API_KEY ausente; create-payment sandbox foi pulado",
            )
        )
    elif not gateway_token:
        results.append(
            _build_result(
                "create_payment",
                status="FAIL",
                required=False,
                message="ASAAS_API_KEY presente, mas HERMES_GATEWAY_TOKEN ausente para autenticar create-payment",
            )
        )
    else:
        create_payment_payload = _build_create_payment_payload(int(time.time()))
        headers = {"Authorization": f"Bearer {gateway_token}"}
        try:
            status, body, parsed = request_json(
                "POST",
                create_payment_url,
                headers=headers,
                payload=create_payment_payload,
                timeout=timeout,
            )
            required_keys = {"invoice_url", "payment_id", "due_date", "asaas_customer_id"}
            if status == 200 and isinstance(parsed, dict) and required_keys.issubset(parsed.keys()):
                results.append(
                    _build_result(
                        "create_payment",
                        status="PASS",
                        required=False,
                        message="create-payment sandbox retornou invoice_url, payment_id, due_date e asaas_customer_id",
                        http_status=status,
                        body=body,
                    )
                )
            else:
                results.append(
                    _build_result(
                        "create_payment",
                        status="FAIL",
                        required=False,
                        message="create-payment sandbox nao retornou o payload esperado",
                        http_status=status,
                        body=body,
                    )
                )
        except RuntimeError as exc:
            results.append(_build_result("create_payment", status="FAIL", required=False, message=str(exc)))

    return results


def print_report(results: list[StepResult], *, base_url: str) -> int:
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")
    blocking_failures = [result for result in results if result.status == "FAIL"]

    print("SPEC-093 Asaas sandbox smoke")
    print(f"Base URL: {base_url}")
    print()

    for result in results:
        line = f"[{result.status}] {result.name}: {result.message}"
        if result.http_status is not None:
            line += f" (http={result.http_status})"
        if result.body:
            line += f" | body={result.body}"
        print(line)

    print()
    print(f"Resumo: {passed} passed, {failed} failed, {skipped} skipped")

    if blocking_failures:
        print("Veredito: FAIL")
        return 1

    print("Veredito: PASS")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke sandbox da SPEC-093 no Hermes")
    parser.add_argument("--base-url", default=os.getenv("HERMES_GATEWAY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--skip-create-payment", action="store_true")
    parser.add_argument("--no-env-autoload", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.no_env_autoload:
        load_default_env_files()
    results = run_smoke(
        base_url=args.base_url,
        timeout=args.timeout,
        skip_create_payment=args.skip_create_payment,
    )
    return print_report(results, base_url=args.base_url.rstrip("/"))


if __name__ == "__main__":
    raise SystemExit(main())
