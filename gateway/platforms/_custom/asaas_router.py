"""SPEC-093 - Asaas routers for the Hermes gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hmac
import json
import logging
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - guarded by api_server requirements
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from gateway.platforms._custom.compat import ensure_media_dispatch_pool
from gateway.platforms._custom.media_dispatch_router import gateway_bearer_middleware

logger = logging.getLogger(__name__)

_ALLOWED_SKUS = {
    "TAG_SOLO": "ASAAS_PRICE_TAG_SOLO",
    "TAG_DUPLA": "ASAAS_PRICE_TAG_DUPLA",
    "REVOLUCAO_BASICO": "ASAAS_PRICE_REVOLUCAO_BASICO",
    "REVOLUCAO_AVANCADO": "ASAAS_PRICE_REVOLUCAO_AVANCADO",
    "ADVOGANDO_25K": "ASAAS_PRICE_ADVOGANDO_25K",
}
_PHONE_RE = re.compile(r"\D+")
_CARD_ONLY_BILLING_TYPE = "CREDIT_CARD"
_DISALLOWED_BILLING_TYPES = {"BOLETO", "PIX", "UNDEFINED"}


class AsaasConfigError(RuntimeError):
    """Raised when required Asaas configuration is missing or invalid."""


class AsaasAPIError(RuntimeError):
    """Raised when the Asaas API returns an error or malformed payload."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CustomerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    cpfCnpj: str = Field(min_length=11, max_length=18)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=8, max_length=32)

    @field_validator("name", "email", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("cpfCnpj", mode="before")
    @classmethod
    def _normalize_cpf_cnpj(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        digits = _PHONE_RE.sub("", value)
        return digits or value.strip()

    @field_validator("phone", mode="before")
    @classmethod
    def _normalize_phone(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        digits = _PHONE_RE.sub("", value)
        if digits and not digits.startswith("55"):
            digits = f"55{digits}"
        return digits or value.strip()


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=80)
    customer: CustomerPayload
    agent: str = Field(min_length=1, max_length=80)
    conv_id: str = Field(min_length=1, max_length=120)
    lead_phone: str = Field(min_length=8, max_length=32)
    installments: int | None = Field(default=None, ge=1)

    @field_validator("sku", mode="before")
    @classmethod
    def _normalize_sku(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("agent", "conv_id", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("lead_phone", mode="before")
    @classmethod
    def _normalize_lead_phone(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        digits = _PHONE_RE.sub("", value)
        if digits and not digits.startswith("55"):
            digits = f"55{digits}"
        return digits or value.strip()


@dataclass(slots=True)
class AsaasClient:
    base_url: str
    api_key: str
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "AsaasClient":
        base_url = (os.getenv("ASAAS_BASE_URL") or "https://sandbox.asaas.com/api/v3").strip().rstrip("/")
        api_key = (os.getenv("ASAAS_API_KEY") or "").strip()
        if not api_key:
            raise AsaasConfigError("asaas_api_key_missing")
        return cls(base_url=base_url, api_key=api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access_token": self.api_key,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = await client.request(method, path, params=params, json=json_body)

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            message = _asaas_error_message(payload) or response.text or "asaas_api_error"
            raise AsaasAPIError(message, status_code=response.status_code)

        if not isinstance(payload, dict):
            raise AsaasAPIError("asaas_invalid_json", status_code=response.status_code)
        return payload

    async def find_customer_by_cpf_cnpj(self, cpf_cnpj: str) -> dict[str, Any] | None:
        payload = await self._request("GET", "/customers", params={"cpfCnpj": cpf_cnpj})
        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return first
        return None

    async def create_customer(self, customer: CustomerPayload) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/customers",
            json_body={
                "name": customer.name,
                "cpfCnpj": customer.cpfCnpj,
                "email": customer.email,
                "mobilePhone": customer.phone,
                "phone": customer.phone,
            },
        )

    async def ensure_customer(self, customer: CustomerPayload) -> dict[str, Any]:
        existing = await self.find_customer_by_cpf_cnpj(customer.cpfCnpj)
        if existing is not None:
            return existing
        return await self.create_customer(customer)

    async def create_payment(
        self,
        *,
        customer_id: str,
        sku: str,
        amount: Decimal,
        due_date: str,
        installments: int,
        external_reference: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "customer": customer_id,
            "billingType": _CARD_ONLY_BILLING_TYPE,
            "dueDate": due_date,
            "description": f"{sku} via Hermes Gateway",
            "externalReference": external_reference,
        }
        if installments > 1:
            body["installmentCount"] = installments
            body["totalValue"] = float(amount)
        else:
            body["value"] = float(amount)
        payload = await self._request("POST", "/payments", json_body=body)
        billing_type = str(payload.get("billingType") or "").upper()
        if billing_type in _DISALLOWED_BILLING_TYPES:
            raise AsaasAPIError(f"asaas_card_only_violation: billingType={billing_type}")
        return payload


def _asaas_error_message(payload: dict[str, Any]) -> str | None:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            description = first.get("description") or first.get("message") or first.get("code")
            if isinstance(description, str) and description.strip():
                return description.strip()
        if isinstance(first, str) and first.strip():
            return first.strip()
    message = payload.get("message") or payload.get("error")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _json_error(status: int, error: str, *, detail: Any = None) -> "web.Response":
    payload: dict[str, Any] = {"ok": False, "error": error}
    if detail is not None:
        payload["detail"] = detail
    return web.json_response(payload, status=status)


def token_valid(received: str | None, expected: str | None) -> bool:
    received_token = (received or "").strip()
    expected_token = (expected or "").strip()
    if not received_token or not expected_token:
        return False
    return hmac.compare_digest(received_token, expected_token)


async def _get_pool(adapter: Any) -> Any:
    try:
        return await ensure_media_dispatch_pool(adapter)
    except Exception as exc:  # pragma: no cover - defensive path
        raise RuntimeError(f"db_pool_unavailable: {exc}") from exc


def _get_adapter(request: "web.Request") -> Any:
    adapter = request.config_dict.get("api_server_adapter")
    if adapter is None:
        raise RuntimeError("api_server_adapter_unavailable")
    return adapter


def _require_json_content_type(request: "web.Request") -> None:
    if request.content_type != "application/json":
        raise ValueError("unsupported_media_type")


def _expected_webhook_token() -> str:
    return (os.getenv("ASAAS_WEBHOOK_TOKEN") or "").strip()


def _require_string_field(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{field_name}")
    return value.strip()


def _coerce_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def price_for_sku(sku: str) -> Decimal:
    env_var = _ALLOWED_SKUS.get(sku)
    if env_var is None:
        raise ValueError("invalid_sku")

    raw_value = (os.getenv(env_var) or "").strip()
    if not raw_value:
        raise AsaasConfigError("price_not_configured")

    try:
        amount = Decimal(raw_value)
    except InvalidOperation as exc:  # pragma: no cover - env parsing guard
        raise AsaasConfigError("price_not_configured") from exc

    if amount <= 0:
        raise AsaasConfigError("price_not_configured")
    return amount.quantize(Decimal("0.01"))


def due_date_business_days(
    business_days: int | None = None,
    *,
    from_date: date | None = None,
) -> str:
    cursor = from_date or date.today()
    remaining = max(0, business_days if business_days is not None else int(os.getenv("ASAAS_DEFAULT_DUE_DAYS", "3")))
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor.isoformat()


def _max_installments() -> int:
    raw_value = (os.getenv("ASAAS_DEFAULT_MAX_INSTALLMENTS") or "12").strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        return 12
    return max(1, parsed)


def _external_reference(payload: CreatePaymentRequest) -> str:
    return f"{payload.agent}:{payload.conv_id}:{payload.lead_phone}:{payload.sku}"[:120]


def _invoice_url(payment_payload: dict[str, Any]) -> str | None:
    for key in ("invoiceUrl", "bankSlipUrl", "invoice_url"):
        value = payment_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _insert_payment_request(conn: Any, payload: CreatePaymentRequest, amount: Decimal, installments: int) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO public.asaas_payment_request (
            agent_source,
            conv_id,
            lead_phone,
            lead_name,
            lead_cpf,
            lead_email,
            sku,
            amount,
            installments,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'requested')
        RETURNING id
        """,
        payload.agent,
        payload.conv_id,
        payload.lead_phone,
        payload.customer.name,
        payload.customer.cpfCnpj,
        payload.customer.email,
        payload.sku,
        amount,
        installments,
    )
    if not row or "id" not in row:
        raise RuntimeError("payment_request_insert_failed")
    return int(row["id"])


async def _mark_payment_request_created(
    conn: Any,
    payment_request_id: int,
    *,
    customer_id: str,
    payment_id: str,
    invoice_url: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE public.asaas_payment_request
        SET
            asaas_customer_id = $1,
            asaas_payment_id = $2,
            invoice_url = $3,
            status = 'created',
            error_message = NULL,
            updated_at = NOW()
        WHERE id = $4
        """,
        customer_id,
        payment_id,
        invoice_url,
        payment_request_id,
    )


async def _mark_payment_request_failed(conn: Any, payment_request_id: int, error_message: str) -> None:
    await conn.execute(
        """
        UPDATE public.asaas_payment_request
        SET
            status = 'error',
            error_message = $1,
            updated_at = NOW()
        WHERE id = $2
        """,
        error_message[:500],
        payment_request_id,
    )


async def _insert_asaas_event_log(conn: Any, payload: dict[str, Any]) -> int | None:
    event_id = _require_string_field(payload, "id")
    event_type = _require_string_field(payload, "event")
    payment_payload = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
    payment_id = str(payment_payload.get("id") or "").strip() or None
    customer_id = str(payment_payload.get("customer") or "").strip() or None
    amount = _coerce_amount(payment_payload.get("value"))

    row = await conn.fetchrow(
        """
        INSERT INTO public.asaas_event_log (
            asaas_event_id,
            event_type,
            payment_id,
            customer_id,
            amount,
            payload
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (asaas_event_id) DO NOTHING
        RETURNING id
        """,
        event_id,
        event_type,
        payment_id,
        customer_id,
        amount,
        json.dumps(payload),
    )
    if row is None:
        return None
    return int(row["id"])


def _schedule_asaas_event_processing(adapter: Any, event_log_id: int, payload: dict[str, Any]) -> None:
    processor = getattr(adapter, "schedule_asaas_event_processing", None)
    if processor is None:
        processor = getattr(adapter, "process_asaas_event", None)
    if processor is None:
        return

    try:
        maybe_coro = processor(event_log_id, payload)
    except TypeError:
        maybe_coro = processor(payload)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception("[asaas_router] webhook fast-path scheduling failed: %s", exc)
        return

    if asyncio.iscoroutine(maybe_coro):
        asyncio.create_task(maybe_coro)


async def handle_create_payment(request: "web.Request") -> "web.Response":
    payment_request_id: int | None = None
    pool = None

    try:
        _require_json_content_type(request)
        payload = CreatePaymentRequest.model_validate(await request.json())

        amount = price_for_sku(payload.sku)
        max_installments = _max_installments()
        installments = payload.installments or max_installments
        if installments > max_installments:
            return _json_error(400, "invalid_request", detail="installments_exceeds_max")

        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
        async with pool.acquire() as conn:
            payment_request_id = await _insert_payment_request(conn, payload, amount, installments)

        due_date = due_date_business_days()
        asaas_client = AsaasClient.from_env()
        customer_payload = await asaas_client.ensure_customer(payload.customer)
        asaas_customer_id = str(customer_payload.get("id") or "").strip()
        if not asaas_customer_id:
            raise AsaasAPIError("asaas_customer_missing_id")

        payment_payload = await asaas_client.create_payment(
            customer_id=asaas_customer_id,
            sku=payload.sku,
            amount=amount,
            due_date=due_date,
            installments=installments,
            external_reference=_external_reference(payload),
        )
        payment_id = str(payment_payload.get("id") or "").strip()
        if not payment_id:
            raise AsaasAPIError("asaas_payment_missing_id")
        invoice_url = _invoice_url(payment_payload)
        billing_type = str(payment_payload.get("billingType") or _CARD_ONLY_BILLING_TYPE).upper()
        if billing_type in _DISALLOWED_BILLING_TYPES:
            raise AsaasAPIError(f"asaas_card_only_violation: billingType={billing_type}")

        async with pool.acquire() as conn:
            await _mark_payment_request_created(
                conn,
                payment_request_id,
                customer_id=asaas_customer_id,
                payment_id=payment_id,
                invoice_url=invoice_url,
            )

        return web.json_response(
            {
                "invoice_url": invoice_url,
                "payment_id": payment_id,
                "due_date": due_date,
                "asaas_customer_id": asaas_customer_id,
                "billing_type": billing_type,
            },
            status=200,
        )
    except ValidationError as exc:
        return _json_error(400, "invalid_request", detail=exc.errors())
    except ValueError as exc:
        if str(exc) == "unsupported_media_type":
            return _json_error(415, "unsupported_media_type")
        if str(exc) == "invalid_sku":
            return _json_error(400, "invalid_sku")
        return _json_error(400, "invalid_request", detail=str(exc))
    except AsaasConfigError as exc:
        error = str(exc)
        if payment_request_id is not None and pool is not None:
            async with pool.acquire() as conn:
                await _mark_payment_request_failed(conn, payment_request_id, error)
        return _json_error(500, error)
    except AsaasAPIError as exc:
        if payment_request_id is not None and pool is not None:
            async with pool.acquire() as conn:
                await _mark_payment_request_failed(conn, payment_request_id, str(exc))
        logger.warning("[asaas_router] Asaas API error on create-payment: status=%s error=%s", exc.status_code, exc)
        return _json_error(502, "asaas_api_error")
    except RuntimeError as exc:
        if payment_request_id is not None and pool is not None:
            async with pool.acquire() as conn:
                await _mark_payment_request_failed(conn, payment_request_id, str(exc))
        logger.exception("[asaas_router] create-payment runtime failure: %s", exc)
        return _json_error(500, "internal_error")
    except Exception as exc:  # pragma: no cover - defensive path
        if payment_request_id is not None and pool is not None:
            async with pool.acquire() as conn:
                await _mark_payment_request_failed(conn, payment_request_id, exc.__class__.__name__)
        logger.exception("[asaas_router] create-payment failed: %s", exc)
        return _json_error(500, "internal_error")


async def handle_asaas_webhook(request: "web.Request") -> "web.Response":
    received_token = request.headers.get("asaas-access-token")
    if not received_token or not received_token.strip():
        return _json_error(401, "missing_asaas_access_token")
    if not token_valid(received_token, _expected_webhook_token()):
        return _json_error(401, "invalid_asaas_access_token")

    try:
        _require_json_content_type(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid_request")

        adapter = _get_adapter(request)
        pool = await _get_pool(adapter)
        async with pool.acquire() as conn:
            event_log_id = await _insert_asaas_event_log(conn, payload)

        duplicate = event_log_id is None
        if not duplicate:
            _schedule_asaas_event_processing(adapter, event_log_id, payload)

        return web.json_response({"ok": True, "duplicate": duplicate}, status=200)
    except ValueError as exc:
        if str(exc) == "unsupported_media_type":
            return _json_error(415, "unsupported_media_type")
        return _json_error(400, "invalid_request", detail=str(exc))
    except Exception as exc:  # pragma: no cover - fail-soft for authenticated requests
        logger.exception("[asaas_router] webhook failed after auth: %s", exc)
        return web.json_response({"ok": True, "error_logged": True}, status=200)


def build_asaas_private_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application(middlewares=[gateway_bearer_middleware])
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("/create-payment", handle_create_payment)
    return subapp


def build_asaas_public_subapp(adapter: Any) -> "web.Application":
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp_not_installed")

    subapp = web.Application()
    subapp["api_server_adapter"] = adapter
    subapp.router.add_post("", handle_asaas_webhook)
    subapp.router.add_post("/", handle_asaas_webhook)
    subapp.router.add_post("/webhook", handle_asaas_webhook)
    return subapp


def mount_asaas_subapps(parent_app: "web.Application", adapter: Any) -> dict[str, "web.Application"]:
    private_subapp = build_asaas_private_subapp(adapter)
    public_subapp = build_asaas_public_subapp(adapter)
    parent_app.add_subapp("/api/gateway/asaas", private_subapp)
    parent_app.add_subapp("/asaas", public_subapp)
    legacy_public_subapp = build_asaas_public_subapp(adapter)
    parent_app.add_subapp("/api/webhook/asaas", legacy_public_subapp)
    return {"private": private_subapp, "public": public_subapp, "legacy_public": legacy_public_subapp}


__all__ = [
    "AsaasClient",
    "CustomerPayload",
    "CreatePaymentRequest",
    "build_asaas_private_subapp",
    "build_asaas_public_subapp",
    "due_date_business_days",
    "handle_asaas_webhook",
    "handle_create_payment",
    "mount_asaas_subapps",
    "price_for_sku",
    "token_valid",
]
