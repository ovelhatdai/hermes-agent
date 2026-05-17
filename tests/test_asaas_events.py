from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gateway.platforms._custom.asaas_events import process_event, process_pending_events


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _FakeWhatsApp:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content):
        self.calls.append((chat_id, content))
        return SimpleNamespace(success=True, message_id=f"msg_{len(self.calls)}", error=None)


class _FakeConnection:
    def __init__(self, *, event_rows, payment_requests):
        self.event_rows = {row["id"]: deepcopy(row) for row in event_rows}
        self.payment_requests = {row["asaas_payment_id"]: deepcopy(row) for row in payment_requests}
        self.locked_ids = set()

    async def fetchval(self, query, *args):
        if "pg_try_advisory_lock" in query:
            event_id = int(args[0])
            if event_id in self.locked_ids:
                return False
            self.locked_ids.add(event_id)
            return True
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def fetchrow(self, query, *args):
        if "FROM public.asaas_event_log" in query and "WHERE id = $1" in query:
            row = self.event_rows.get(int(args[0]))
            return deepcopy(row) if row is not None else None
        if "FROM public.asaas_payment_request" in query and "WHERE asaas_payment_id = $1" in query:
            row = self.payment_requests.get(args[0])
            return deepcopy(row) if row is not None else None
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(self, query, *args):
        if "FROM public.asaas_event_log" in query and "processed_at IS NULL" in query:
            limit = int(args[0])
            rows = [
                deepcopy(row)
                for row in sorted(self.event_rows.values(), key=lambda item: (item["created_at"], item["id"]))
                if row.get("processed_at") is None
            ]
            return rows[:limit]
        raise AssertionError(f"unexpected fetch query: {query}")

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        if "SELECT pg_advisory_unlock($1)" in normalized:
            self.locked_ids.discard(int(args[0]))
            return "SELECT 1"
        if "UPDATE public.asaas_payment_request SET status = $1" in normalized:
            status, error_message, payment_id = args
            row = self.payment_requests.get(payment_id)
            if row is not None:
                row["status"] = status
                row["error_message"] = error_message
                row["updated_at"] = "updated"
            return "UPDATE 1"
        if "UPDATE public.asaas_event_log SET processed_at = NOW()" in normalized:
            notification_sent, zapsign_doc_id, error_message, event_id = args
            row = self.event_rows[int(event_id)]
            row["processed_at"] = "processed"
            row["notification_sent"] = notification_sent
            row["zapsign_doc_id"] = zapsign_doc_id
            row["error_message"] = error_message
            return "UPDATE 1"
        if "UPDATE public.asaas_event_log SET error_message = $1 WHERE id = $2" in normalized:
            error_message, event_id = args
            row = self.event_rows[int(event_id)]
            row["error_message"] = error_message
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute query: {query}")


class _FakeAdapter:
    def __init__(self, conn, whatsapp=None):
        self._pool = _FakePool(conn)
        self._whatsapp = whatsapp

    async def _ensure_media_dispatch_pool(self):
        return self._pool

    def _get_whatsapp_platform(self):
        return self._whatsapp


def _build_event_row(*, event_id=501, event_type="PAYMENT_RECEIVED", payment_id="pay_123", processed_at=None):
    return {
        "id": event_id,
        "asaas_event_id": f"evt_{event_id}",
        "event_type": event_type,
        "payment_id": payment_id,
        "customer_id": "cus_123",
        "amount": Decimal("7500.00"),
        "payload": {
            "id": f"evt_{event_id}",
            "event": event_type,
            "payment": {
                "id": payment_id,
                "value": 7500.0,
                "customer": "cus_123",
                "invoiceUrl": "https://sandbox.asaas.com/i/pay_123",
            },
        },
        "processed_at": processed_at,
        "zapsign_doc_id": None,
        "notification_sent": False,
        "error_message": None,
        "created_at": f"2026-04-26T12:00:{event_id % 60:02d}Z",
    }


def _build_payment_request(payment_id="pay_123"):
    return {
        "id": 101,
        "agent_source": "clara_sdr",
        "conv_id": "45",
        "lead_phone": "5511999998888",
        "lead_name": "Maria Silva",
        "lead_cpf": "12345678900",
        "lead_email": "maria@email.com",
        "sku": "TAG_DUPLA",
        "amount": Decimal("7500.00"),
        "installments": 12,
        "asaas_customer_id": "cus_123",
        "asaas_payment_id": payment_id,
        "invoice_url": "https://sandbox.asaas.com/i/pay_123",
        "status": "created",
        "error_message": None,
        "created_at": "2026-04-26T11:00:00Z",
        "updated_at": "2026-04-26T11:00:00Z",
    }


@pytest.fixture(autouse=True)
def _notify_targets(monkeypatch):
    monkeypatch.setenv("HERMES_NOTIFY_PHONE_VINI", "5551991987972")
    monkeypatch.setenv("HERMES_NOTIFY_PHONE_JOANNE", "5551984580681")
    monkeypatch.delenv("ZAPSIGN_CREATE_DOC_URL", raising=False)
    monkeypatch.delenv("ZAPSIGN_DOC_CREATE_URL", raising=False)
    monkeypatch.delenv("ZAPSIGN_API_TOKEN", raising=False)
    monkeypatch.delenv("ZAPSIGN_TOKEN", raising=False)
    monkeypatch.delenv("ZAPSIGN_TEMPLATE_TAG_DUPLA", raising=False)


@pytest.mark.asyncio
async def test_payment_received_updates_status_and_sends_notifications(monkeypatch):
    conn = _FakeConnection(
        event_rows=[_build_event_row()],
        payment_requests=[_build_payment_request()],
    )
    whatsapp = _FakeWhatsApp()
    adapter = _FakeAdapter(conn, whatsapp=whatsapp)

    async def _fake_zapsign(payment_request, event_row):
        return {"status": "success", "doc_id": "doc_123", "error_message": None}

    monkeypatch.setattr("gateway.platforms._custom.asaas_events.create_zapsign_doc_for_payment", _fake_zapsign)

    result = await process_event(adapter, {"id": 501})

    assert result["ok"] is True
    assert result["status"] == "processed"
    assert conn.payment_requests["pay_123"]["status"] == "paid"
    assert conn.event_rows[501]["processed_at"] == "processed"
    assert conn.event_rows[501]["zapsign_doc_id"] == "doc_123"
    assert len(whatsapp.calls) == 2
    assert all("Lead Maria Silva pagou TAG_DUPLA" in call[1] for call in whatsapp.calls)
    assert all("resultado ZapSign: success" in call[1] for call in whatsapp.calls)


@pytest.mark.asyncio
async def test_missing_zapsign_configuration_does_not_break_flow():
    conn = _FakeConnection(
        event_rows=[_build_event_row()],
        payment_requests=[_build_payment_request()],
    )
    whatsapp = _FakeWhatsApp()
    adapter = _FakeAdapter(conn, whatsapp=whatsapp)

    result = await process_event(adapter, {"id": 501})

    assert result["ok"] is True
    assert conn.payment_requests["pay_123"]["status"] == "paid"
    assert conn.event_rows[501]["processed_at"] == "processed"
    assert conn.event_rows[501]["error_message"] == "zapsign_not_configured"
    assert all("resultado ZapSign: config_missing" in call[1] for call in whatsapp.calls)


@pytest.mark.asyncio
async def test_duplicate_processing_is_skipped_after_processed_at(monkeypatch):
    conn = _FakeConnection(
        event_rows=[_build_event_row()],
        payment_requests=[_build_payment_request()],
    )
    whatsapp = _FakeWhatsApp()
    adapter = _FakeAdapter(conn, whatsapp=whatsapp)

    async def _fake_zapsign(payment_request, event_row):
        return {"status": "success", "doc_id": "doc_123", "error_message": None}

    monkeypatch.setattr("gateway.platforms._custom.asaas_events.create_zapsign_doc_for_payment", _fake_zapsign)

    first = await process_event(adapter, {"id": 501})
    second = await process_event(adapter, {"id": 501})

    assert first["status"] == "processed"
    assert second["status"] == "already_processed"
    assert len(whatsapp.calls) == 2


@pytest.mark.asyncio
async def test_sweep_reprocesses_pending_rows(monkeypatch):
    conn = _FakeConnection(
        event_rows=[
            _build_event_row(event_id=501, event_type="PAYMENT_RECEIVED", processed_at=None),
            _build_event_row(event_id=502, event_type="PAYMENT_OVERDUE", processed_at="processed"),
        ],
        payment_requests=[_build_payment_request()],
    )
    whatsapp = _FakeWhatsApp()
    adapter = _FakeAdapter(conn, whatsapp=whatsapp)

    async def _fake_zapsign(payment_request, event_row):
        return {"status": "success", "doc_id": "doc_123", "error_message": None}

    monkeypatch.setattr("gateway.platforms._custom.asaas_events.create_zapsign_doc_for_payment", _fake_zapsign)

    result = await process_pending_events(adapter, limit=10)

    assert result == {"processed": 1, "skipped": 0, "failed": 0, "scanned": 1}
    assert conn.event_rows[501]["processed_at"] == "processed"
    assert len(whatsapp.calls) == 2


@pytest.mark.asyncio
async def test_payment_overdue_marks_request_and_notifies():
    conn = _FakeConnection(
        event_rows=[_build_event_row(event_type="PAYMENT_OVERDUE")],
        payment_requests=[_build_payment_request()],
    )
    whatsapp = _FakeWhatsApp()
    adapter = _FakeAdapter(conn, whatsapp=whatsapp)

    result = await process_event(adapter, {"id": 501})

    assert result["ok"] is True
    assert conn.payment_requests["pay_123"]["status"] == "overdue"
    assert conn.event_rows[501]["processed_at"] == "processed"
    assert all("status evento: PAYMENT_OVERDUE" in call[1] for call in whatsapp.calls)
