"""SPEC-077 Task 08 -- Hermes watcher for Compozy run milestones."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

try:
    import asyncpg
except ImportError:  # pragma: no cover - runtime guard
    asyncpg = None  # type: ignore[assignment]

from gateway.config import Platform, load_gateway_config
from tools.send_message_tool import _send_to_platform

logger = logging.getLogger(__name__)

TASK_TOKEN_RE = re.compile(r"(task_\d+)", re.IGNORECASE)
CRITICAL_REGEX_KEYS = (
    "critical_tasks_regex",
    "critical_task_regex",
    "task_critical_regex",
)


@dataclass(frozen=True, slots=True)
class Milestone:
    type: str
    key: str
    message: str
    event_ts: datetime | None = None


def _watch_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("HERMES_TIMEZONE", "America/Sao_Paulo"))


def _quiet_hour(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        hour = int(raw)
    except ValueError:
        return default
    return max(0, min(hour, 23))


def _lookback_minutes() -> int:
    raw = (os.getenv("COMPOZY_WATCH_LOOKBACK_MINUTES") or "20").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 20
    return max(1, minutes)


def _runner_url() -> str:
    return (os.getenv("COMPOZY_RUNNER_URL") or "http://127.0.0.1:9150").strip().rstrip("/")


def _runner_headers() -> dict[str, str]:
    token = (os.getenv("COMPOZY_API_TOKEN") or "").strip()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _spec_root() -> Path:
    explicit = (os.getenv("COMPOZY_SPEC_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    repo_root = (os.getenv("COMPOZY_REPO_ROOT") or "/opt/second-brain").strip()
    return Path(repo_root).expanduser() / ".compozy" / "tasks"


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if not digits:
        raise RuntimeError("VINI_PHONE_E164 missing or invalid")
    if "@" in phone:
        return phone
    return f"{digits}@s.whatsapp.net"


def _database_dsn() -> str:
    explicit = (
        os.getenv("HERMES_MEDIA_DISPATCH_DATABASE_URL", "")
        or os.getenv("DATABASE_URL", "")
    ).strip()
    if explicit:
        return explicit

    host = (os.getenv("HERMES_MEDIA_DISPATCH_DB_HOST") or os.getenv("PGHOST") or "127.0.0.1").strip()
    port = (os.getenv("HERMES_MEDIA_DISPATCH_DB_PORT") or os.getenv("PGPORT") or "5432").strip()
    database = (os.getenv("HERMES_MEDIA_DISPATCH_DB_NAME") or os.getenv("PGDATABASE") or "hermes").strip()
    user = (os.getenv("HERMES_MEDIA_DISPATCH_DB_USER") or os.getenv("PGUSER") or "evolution").strip()
    password = (os.getenv("HERMES_MEDIA_DISPATCH_DB_PASSWORD") or os.getenv("PGPASSWORD") or "").strip()

    if not host or not port or not database or not user:
        return ""

    if password:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return f"postgresql://{user}@{host}:{port}/{database}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def in_quiet_hours(now: datetime | None = None) -> bool:
    localized = (now or _utc_now()).astimezone(_watch_timezone())
    start = _quiet_hour("QUIET_HOURS_START", 22)
    end = _quiet_hour("QUIET_HOURS_END", 7)
    hour = localized.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def should_delay_for_quiet_hours(milestone_type: str, now: datetime | None = None) -> bool:
    if milestone_type == "failed":
        return False
    return in_quiet_hours(now)


def next_release_after(now: datetime | None = None) -> datetime:
    localized = (now or _utc_now()).astimezone(_watch_timezone())
    target = localized.date()
    if localized.hour >= _quiet_hour("QUIET_HOURS_START", 22):
        target += timedelta(days=1)
    release_local = datetime.combine(
        target,
        time(hour=_quiet_hour("QUIET_HOURS_END", 7), minute=0, second=0),
        tzinfo=_watch_timezone(),
    )
    if release_local <= localized:
        release_local += timedelta(days=1)
    return release_local.astimezone(timezone.utc)


def _iter_event_maps(event: dict[str, Any]):
    yield event
    for key in ("data", "payload", "details", "attributes", "metadata"):
        value = event.get(key)
        if isinstance(value, dict):
            yield value


def _extract_text(event: dict[str, Any], *keys: str) -> str | None:
    for mapping in _iter_event_maps(event):
        for key in keys:
            value = mapping.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, dict):
                nested = _extract_text(value, "message", "text", "name", "id")
                if nested:
                    return nested
                continue
            return str(value).strip()
    return None


def _normalize_task_name(raw: str | None) -> str | None:
    if not raw:
        return None
    match = TASK_TOKEN_RE.search(raw)
    if match:
        return match.group(1).lower()
    return raw.strip()


def extract_task_name(event: dict[str, Any]) -> str | None:
    candidate = _extract_text(
        event,
        "task",
        "task_id",
        "current_task",
        "job",
        "job_id",
        "name",
        "slug",
    )
    return _normalize_task_name(candidate)


def is_explicitly_critical(event: dict[str, Any]) -> bool:
    for mapping in _iter_event_maps(event):
        for key in ("task_critical", "critical", "is_critical"):
            value = mapping.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
    return False


def extract_phase_label(run: dict[str, Any], event: dict[str, Any]) -> str | None:
    explicit = _extract_text(event, "phase", "phase_label")
    if explicit:
        return explicit

    for mapping in (event, run):
        phase_index = mapping.get("phase_index")
        phase_total = mapping.get("phase_total")
        if phase_index and phase_total:
            return f"Fase {phase_index}/{phase_total}"

    run_phase = run.get("phase")
    if isinstance(run_phase, str) and run_phase.strip():
        return run_phase.strip()
    return None


def extract_error_text(event: dict[str, Any]) -> str:
    text = _extract_text(event, "error", "message", "detail", "reason") or ""
    return text[:300]


def extract_elapsed_human(run: dict[str, Any]) -> str:
    explicit = run.get("elapsed_human")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    seconds = run.get("elapsed_seconds")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def normalize_event_kind(event: dict[str, Any]) -> str:
    raw = _extract_text(event, "type", "kind", "event") or ""
    normalized = raw.strip().lower().replace(".", "_").replace("-", "_")
    aliases = {
        "job_completed": "task_completed",
        "task_completed": "task_completed",
        "task_complete": "task_completed",
        "phase_done": "phase_completed",
        "run_done": "run_completed",
        "run_complete": "run_completed",
        "run_error": "run_failed",
    }
    return aliases.get(normalized, normalized)


@lru_cache(maxsize=256)
def _critical_pattern(spec_root: str, spec_id: str) -> re.Pattern[str] | None:
    root_path = Path(spec_root)
    matches = sorted(root_path.glob(f"{spec_id}-*/_prd.md"))
    if not matches:
        return None
    text = matches[0].read_text(encoding="utf-8", errors="ignore")
    for key in CRITICAL_REGEX_KEYS:
        match = re.search(rf"(?mi)^{re.escape(key)}\s*:\s*(.+?)\s*$", text)
        if not match:
            continue
        pattern_text = match.group(1).strip().strip("\"").strip("'")
        if not pattern_text:
            return None
        try:
            return re.compile(pattern_text, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid %s for %s: %s", key, spec_id, pattern_text)
            return None
    return None


def build_critical_matcher(spec_root: Path | None = None) -> Callable[[str, str | None], bool]:
    root = str((spec_root or _spec_root()).resolve())

    def _matches(spec_id: str, task_name: str | None) -> bool:
        if not spec_id or not task_name:
            return False
        pattern = _critical_pattern(root, spec_id)
        return bool(pattern and pattern.search(task_name))

    return _matches


def classify_event(
    run: dict[str, Any],
    event: dict[str, Any],
    *,
    critical_matcher: Callable[[str, str | None], bool] | None = None,
) -> Milestone | None:
    spec_id = str(run.get("spec_id") or "").strip()
    run_id = str(run.get("run_id") or "").strip()
    kind = normalize_event_kind(event)
    event_ts = parse_timestamp(event.get("ts") or event.get("timestamp") or event.get("created_at"))

    if kind == "run_started":
        return Milestone(
            type="started",
            key="started",
            message=f"🚀 {spec_id} comecou (run {run_id})",
            event_ts=event_ts,
        )

    if kind == "run_failed":
        task_name = extract_task_name(event) or str(run.get("current_task") or "?")
        error_text = extract_error_text(event) or "erro nao informado"
        return Milestone(
            type="failed",
            key="failed",
            message=f"🔴 {spec_id} falhou em {task_name}:\n{error_text}",
            event_ts=event_ts,
        )

    if kind == "run_completed":
        tasks_total = run.get("tasks_total") or run.get("tasks_completed") or "?"
        elapsed = extract_elapsed_human(run)
        return Milestone(
            type="completed",
            key="completed",
            message=f"🎉 {spec_id} finalizada! {tasks_total} tasks em {elapsed}",
            event_ts=event_ts,
        )

    if kind == "phase_completed":
        phase_label = extract_phase_label(run, event)
        if not phase_label:
            return None
        phase_key = phase_label.lower().strip()
        return Milestone(
            type="phase_done",
            key=f"phase_done:{phase_key}",
            message=f"✅ {spec_id} — {phase_label} concluida",
            event_ts=event_ts,
        )

    if kind == "task_completed":
        task_name = extract_task_name(event)
        matcher = critical_matcher or build_critical_matcher()
        if not is_explicitly_critical(event) and not matcher(spec_id, task_name):
            return None
        task_label = task_name or "task-desconhecida"
        return Milestone(
            type="task_critical",
            key=f"task_critical:{task_label}",
            message=f"📍 {spec_id} completou task critica {task_label}",
            event_ts=event_ts,
        )

    return None


async def _log_exists(conn: Any, run_id: str, milestone_key: str) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM compozy_alert_log WHERE run_id = $1 AND milestone = $2",
        run_id,
        milestone_key,
    )
    return bool(row)


async def _buffer_exists(conn: Any, run_id: str, milestone_key: str) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM compozy_alert_buffer WHERE run_id = $1 AND milestone = $2",
        run_id,
        milestone_key,
    )
    return bool(row)


async def milestone_exists(conn: Any, run_id: str, milestone_key: str) -> bool:
    return await _log_exists(conn, run_id, milestone_key) or await _buffer_exists(conn, run_id, milestone_key)


async def record_sent_alert(conn: Any, run: dict[str, Any], milestone: Milestone, sent_at: datetime | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO compozy_alert_log (run_id, spec_id, milestone, sent_at)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (run_id, milestone) DO NOTHING
        """,
        str(run.get("run_id") or ""),
        str(run.get("spec_id") or ""),
        milestone.key,
        sent_at or _utc_now(),
    )


async def buffer_alert(conn: Any, run: dict[str, Any], milestone: Milestone, release_after: datetime) -> None:
    await conn.execute(
        """
        INSERT INTO compozy_alert_buffer (
            run_id,
            spec_id,
            milestone,
            milestone_type,
            body,
            release_after,
            buffered_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (run_id, milestone) DO NOTHING
        """,
        str(run.get("run_id") or ""),
        str(run.get("spec_id") or ""),
        milestone.key,
        milestone.type,
        milestone.message,
        release_after,
    )


async def mark_buffer_failure(conn: Any, buffer_id: Any, error: str) -> None:
    await conn.execute(
        """
        UPDATE compozy_alert_buffer
        SET attempts = attempts + 1,
            last_error = $2,
            updated_at = NOW()
        WHERE id = $1
        """,
        buffer_id,
        error[:300],
    )


async def delete_buffered_alert(conn: Any, buffer_id: Any) -> None:
    await conn.execute("DELETE FROM compozy_alert_buffer WHERE id = $1", buffer_id)


async def load_due_buffered_alerts(conn: Any, now: datetime | None = None) -> list[Any]:
    return await conn.fetch(
        """
        SELECT id, run_id, spec_id, milestone, milestone_type, body, release_after
        FROM compozy_alert_buffer
        WHERE release_after <= $1
        ORDER BY release_after ASC, buffered_at ASC
        """,
        now or _utc_now(),
    )


async def send_whatsapp_alert(message: str) -> dict[str, Any]:
    chip = (os.getenv("HERMES_ALERT_CHIP") or "").strip()
    if not chip:
        raise RuntimeError("HERMES_ALERT_CHIP missing")
    config = load_gateway_config()
    pconfig = config.platforms.get(Platform.WHATSAPP)
    if not pconfig or not pconfig.enabled:
        raise RuntimeError("whatsapp platform not configured")

    target = _normalize_phone(os.getenv("VINI_PHONE_E164", ""))
    result = await _send_to_platform(Platform.WHATSAPP, pconfig, target, message)
    if not isinstance(result, dict):
        raise RuntimeError("unexpected whatsapp sender result")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


async def handle_milestone(
    pool: Any,
    run: dict[str, Any],
    milestone: Milestone,
    *,
    send_func: Callable[[str], Any] = send_whatsapp_alert,
    now: datetime | None = None,
) -> str:
    current_time = now or _utc_now()
    run_id = str(run.get("run_id") or "")

    async with pool.acquire() as conn:
        if await milestone_exists(conn, run_id, milestone.key):
            return "dedup_skipped"
        if should_delay_for_quiet_hours(milestone.type, current_time):
            await buffer_alert(conn, run, milestone, next_release_after(current_time))
            return "buffered"

    await send_func(milestone.message)

    async with pool.acquire() as conn:
        await record_sent_alert(conn, run, milestone, current_time)
    return "sent"


async def flush_buffered_alerts(
    pool: Any,
    *,
    send_func: Callable[[str], Any] = send_whatsapp_alert,
    now: datetime | None = None,
) -> int:
    current_time = now or _utc_now()
    if in_quiet_hours(current_time):
        return 0

    async with pool.acquire() as conn:
        rows = await load_due_buffered_alerts(conn, current_time)

    flushed = 0
    for row in rows:
        run = {"run_id": row["run_id"], "spec_id": row["spec_id"]}
        milestone = Milestone(
            type=str(row["milestone_type"]),
            key=str(row["milestone"]),
            message=str(row["body"]),
        )
        async with pool.acquire() as conn:
            if await _log_exists(conn, run["run_id"], milestone.key):
                await delete_buffered_alert(conn, row["id"])
                continue
        try:
            await send_func(milestone.message)
        except Exception as exc:
            async with pool.acquire() as conn:
                await mark_buffer_failure(conn, row["id"], str(exc))
            logger.warning("Failed to flush buffered milestone %s: %s", milestone.key, exc)
            continue

        async with pool.acquire() as conn:
            await record_sent_alert(conn, run, milestone, current_time)
            await delete_buffered_alert(conn, row["id"])
        flushed += 1
    return flushed


def _coerce_runs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("runs", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _runner_payload_looks_ready(payload: Any) -> bool:
    return isinstance(payload, list) or (isinstance(payload, dict) and any(isinstance(payload.get(key), list) for key in ("runs", "items", "data")))


async def fetch_recent_runs(client: httpx.AsyncClient, cutoff: datetime) -> tuple[list[dict[str, Any]], str | None]:
    response = await client.get(f"{_runner_url()}/api/runs", params={"limit": 200}, headers=_runner_headers())
    if response.status_code == 404:
        return [], "runner_unavailable"
    response.raise_for_status()
    payload = response.json()
    if not _runner_payload_looks_ready(payload):
        return [], "runner_unavailable"

    runs = []
    for run in _coerce_runs(payload):
        last_event_at = parse_timestamp(run.get("last_event_at") or run.get("updated_at") or run.get("started_at"))
        if last_event_at and last_event_at >= cutoff:
            runs.append(run)
    return runs, None


async def fetch_run_events(client: httpx.AsyncClient, run_id: str, since: datetime) -> list[dict[str, Any]]:
    response = await client.get(
        f"{_runner_url()}/api/runs/{run_id}/events",
        params={"since": since.isoformat()},
        headers=_runner_headers(),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("events", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


async def create_pool():
    if asyncpg is None:
        raise RuntimeError("asyncpg not installed")
    dsn = _database_dsn()
    if not dsn:
        raise RuntimeError("database dsn not configured")
    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4, command_timeout=30)


async def run_once(
    *,
    pool: Any | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    send_func: Callable[[str], Any] = send_whatsapp_alert,
    critical_matcher: Callable[[str, str | None], bool] | None = None,
) -> dict[str, Any]:
    current_time = now or _utc_now()
    cutoff = current_time - timedelta(minutes=_lookback_minutes())
    summary = {
        "ok": True,
        "status": "ok",
        "runs_scanned": 0,
        "events_seen": 0,
        "sent": 0,
        "buffered": 0,
        "dedup_skipped": 0,
        "flushed": 0,
    }

    owns_pool = pool is None
    owns_client = client is None
    pool = pool or await create_pool()
    client = client or httpx.AsyncClient(timeout=10.0)

    try:
        summary["flushed"] = await flush_buffered_alerts(pool, send_func=send_func, now=current_time)
        runs, status = await fetch_recent_runs(client, cutoff)
        if status:
            summary["status"] = status
            summary["ok"] = status == "runner_unavailable"
            return summary

        matcher = critical_matcher or build_critical_matcher()
        for run in runs:
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            summary["runs_scanned"] += 1
            events = await fetch_run_events(client, run_id, cutoff)
            summary["events_seen"] += len(events)
            for event in events:
                milestone = classify_event(run, event, critical_matcher=matcher)
                if milestone is None:
                    continue
                outcome = await handle_milestone(
                    pool,
                    run,
                    milestone,
                    send_func=send_func,
                    now=current_time,
                )
                if outcome in summary:
                    summary[outcome] += 1
    finally:
        if owns_client:
            await client.aclose()
        if owns_pool:
            await pool.close()

    return summary


def main() -> int:
    logging.basicConfig(
        level=os.getenv("COMPOZY_WATCH_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        summary = asyncio.run(run_once())
    except Exception as exc:  # pragma: no cover - CLI guard
        print(json.dumps({"ok": False, "status": "error", "error": str(exc)}, ensure_ascii=True))
        return 1

    print(json.dumps(summary, ensure_ascii=True))
    if summary.get("status") == "runner_unavailable":
        return 0
    return 0 if summary.get("ok") else 1


__all__ = [
    "Milestone",
    "build_critical_matcher",
    "classify_event",
    "flush_buffered_alerts",
    "handle_milestone",
    "in_quiet_hours",
    "main",
    "milestone_exists",
    "next_release_after",
    "parse_timestamp",
    "run_once",
    "send_whatsapp_alert",
    "should_delay_for_quiet_hours",
]
