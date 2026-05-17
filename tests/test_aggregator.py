import asyncio

import pytest
import asyncpg

from hermes_cli import mentee_snapshot
from hermes_cli.mentee_snapshot import (
    aggregate_snapshot,
    _fetch_kanban_tasks,
    _fetch_latest_briefing,
    _fetch_sla_alerts,
    _fetch_trafego_cards,
)


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, *, fetch_rows=None, fetchrow_row=None, error=None):
        self.fetch_rows = fetch_rows or []
        self.fetchrow_row = fetchrow_row
        self.error = error
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if self.error:
            raise self.error
        return self.fetch_rows

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        if self.error:
            raise self.error
        return self.fetchrow_row


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class RoutingConn:
    async def fetch(self, sql, *args):
        if "trafego.cards" in sql:
            return [{"id": "1", "categoria": "trafego_pago", "severity": "high", "title": "Card", "created_at": None, "ack_at": None, "responsavel": "diulia"}]
        if "supervisor.event_log" in sql:
            return []
        if "kanban.cards" in sql:
            return []
        return []

    async def fetchrow(self, sql, *args):
        if "briefing.outputs" in sql:
            return None
        return None


@pytest.fixture
def sample_mentee():
    return {
        "id": "68cdc38e57d3f6bbc721dec7",
        "oab": "19570",
        "nome": "Karoline Catananti",
        "condutor": "Karoline Catananti",
        "status": "active",
    }


def test_aggregate_full_response(sample_mentee):
    result = asyncio.run(aggregate_snapshot(sample_mentee, FakePool(RoutingConn())))

    assert result["mentee"] == sample_mentee
    assert isinstance(result["trafego_cards"], list)
    assert isinstance(result["sla_alerts"], list)
    assert isinstance(result["kanban_tasks"], list)
    assert "latest_briefing" in result
    assert result["meta"]["cache_hit"] is False
    assert result["meta"]["latency_ms"] > 0
    assert result["meta"]["fetched_at"]


def test_missing_schema_graceful(sample_mentee):
    pool = FakePool(FakeConn(error=asyncpg.UndefinedTableError("missing")))

    assert asyncio.run(_fetch_trafego_cards("19570", pool)) == []
    assert asyncio.run(_fetch_sla_alerts("19570", pool)) == []
    assert asyncio.run(_fetch_kanban_tasks("19570", sample_mentee["id"], pool)) == []
    assert asyncio.run(_fetch_latest_briefing("19570", pool)) is None


def test_partial_failure_isolation(sample_mentee, monkeypatch):
    async def boom(*args, **kwargs):
        raise Exception("boom")

    monkeypatch.setattr(mentee_snapshot, "_fetch_kanban_tasks", boom)

    result = asyncio.run(aggregate_snapshot(sample_mentee, FakePool(RoutingConn())))

    assert result["kanban_tasks"] == []
    assert isinstance(result["trafego_cards"], list)
    assert isinstance(result["sla_alerts"], list)
    assert "latest_briefing" in result


def test_no_oab_does_not_crash(sample_mentee):
    sample_mentee = dict(sample_mentee, oab=None)

    result = asyncio.run(aggregate_snapshot(sample_mentee, FakePool(RoutingConn())))

    assert result["trafego_cards"] == []
    assert result["sla_alerts"] == []
    assert result["latest_briefing"] is None
