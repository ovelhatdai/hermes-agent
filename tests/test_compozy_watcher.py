from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.platforms._custom.compozy_watcher import Milestone, classify_event, handle_milestone


class FakeConn:
    def __init__(self) -> None:
        self.logged: set[tuple[str, str]] = set()
        self.buffered: dict[int, dict] = {}
        self._buffer_seq = 0

    async def fetchval(self, query: str, *args):
        if "compozy_alert_log" in query:
            return 1 if (str(args[0]), str(args[1])) in self.logged else None
        if "compozy_alert_buffer" in query:
            return (
                1
                if any(
                    row["run_id"] == str(args[0]) and row["milestone"] == str(args[1])
                    for row in self.buffered.values()
                )
                else None
            )
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query: str, *args):
        if "INSERT INTO compozy_alert_log" in query:
            self.logged.add((str(args[0]), str(args[2])))
            return "INSERT 0 1"
        if "INSERT INTO compozy_alert_buffer" in query:
            self._buffer_seq += 1
            self.buffered[self._buffer_seq] = {
                "id": self._buffer_seq,
                "run_id": str(args[0]),
                "spec_id": str(args[1]),
                "milestone": str(args[2]),
                "milestone_type": str(args[3]),
                "body": str(args[4]),
                "release_after": args[5],
                "attempts": 0,
                "last_error": None,
            }
            return "INSERT 0 1"
        if "UPDATE compozy_alert_buffer" in query:
            row = self.buffered[int(args[0])]
            row["attempts"] += 1
            row["last_error"] = str(args[1])
            return "UPDATE 1"
        if "DELETE FROM compozy_alert_buffer" in query:
            self.buffered.pop(int(args[0]), None)
            return "DELETE 1"
        raise AssertionError(f"Unexpected execute query: {query}")

    async def fetch(self, query: str, *args):
        if "FROM compozy_alert_buffer" not in query:
            raise AssertionError(f"Unexpected fetch query: {query}")
        cutoff = args[0]
        return [
            row
            for row in self.buffered.values()
            if row["release_after"] <= cutoff
        ]


class FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn()

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_classify_run_failed():
    run = {"spec_id": "SPEC-077", "run_id": "run-123"}
    event = {
        "kind": "run.failed",
        "task": "task_05",
        "error": "boom",
        "ts": "2026-04-22T12:00:00Z",
    }

    milestone = classify_event(run, event)

    assert milestone is not None
    assert milestone.type == "failed"
    assert milestone.key == "failed"
    assert "SPEC-077" in milestone.message
    assert "task_05" in milestone.message
    assert "boom" in milestone.message


@pytest.mark.asyncio
async def test_quiet_hours_blocks_started_not_failed(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "America/Sao_Paulo")
    monkeypatch.setenv("QUIET_HOURS_START", "22")
    monkeypatch.setenv("QUIET_HOURS_END", "7")

    pool = FakePool()
    sent_messages: list[str] = []

    async def fake_send(message: str):
        sent_messages.append(message)
        return {"success": True}

    run = {"spec_id": "SPEC-077", "run_id": "run-quiet"}
    quiet_now = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)  # 00:00 BRT

    started = Milestone(type="started", key="started", message="start")
    failed = Milestone(type="failed", key="failed", message="fail")

    started_outcome = await handle_milestone(pool, run, started, send_func=fake_send, now=quiet_now)
    failed_outcome = await handle_milestone(pool, run, failed, send_func=fake_send, now=quiet_now)

    assert started_outcome == "buffered"
    assert failed_outcome == "sent"
    assert sent_messages == ["fail"]
    assert ("run-quiet", "failed") in pool.conn.logged
    assert any(row["milestone"] == "started" for row in pool.conn.buffered.values())


@pytest.mark.asyncio
async def test_dedup_same_milestone_twice(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "America/Sao_Paulo")
    monkeypatch.setenv("QUIET_HOURS_START", "22")
    monkeypatch.setenv("QUIET_HOURS_END", "7")

    pool = FakePool()
    sent_messages: list[str] = []

    async def fake_send(message: str):
        sent_messages.append(message)
        return {"success": True}

    run = {"spec_id": "SPEC-077", "run_id": "run-dedup"}
    daytime = datetime(2026, 4, 22, 15, 0, tzinfo=timezone.utc)  # 12:00 BRT
    milestone = Milestone(type="completed", key="completed", message="done")

    first = await handle_milestone(pool, run, milestone, send_func=fake_send, now=daytime)
    second = await handle_milestone(pool, run, milestone, send_func=fake_send, now=daytime)

    assert first == "sent"
    assert second == "dedup_skipped"
    assert sent_messages == ["done"]
    assert ("run-dedup", "completed") in pool.conn.logged
