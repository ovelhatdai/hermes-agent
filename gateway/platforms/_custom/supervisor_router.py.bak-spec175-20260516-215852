"""Read-only Hermes Supervisor dashboard/API for SPEC-119."""
from __future__ import annotations

import html
import os
from decimal import Decimal
from datetime import date, datetime
from typing import Any

import asyncpg
from aiohttp import web


def _pg_config() -> dict[str, Any]:
    url = os.getenv("SUPERVISOR_DATABASE_URL") or os.getenv("HERMES_DB_URL")
    if url:
        return {"dsn": url}
    return {
        "host": os.getenv("SUPERVISOR_PGHOST", os.getenv("PGHOST", "127.0.0.1")),
        "port": int(os.getenv("SUPERVISOR_PGPORT", os.getenv("PGPORT", "5432"))),
        "user": os.getenv("SUPERVISOR_PGUSER", os.getenv("PGUSER", "evolution")),
        "database": os.getenv("SUPERVISOR_PGDATABASE", os.getenv("PGDATABASE", "hermes")),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _as_dict(row: asyncpg.Record) -> dict[str, Any]:
    item = {key: _json_value(value) for key, value in dict(row).items()}
    return item


def _fmt_hours(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 24:
        return f"{number / 24:.1f}d"
    return f"{number:.1f}h"


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(**_pg_config())


async def _summary(request: web.Request) -> web.Response:
    conn = await _connect()
    try:
        total = await conn.fetchval("SELECT COUNT(*)::int FROM supervisor.v_open_alerts")
        groups = await conn.fetch(
            """
            SELECT area,
                   responsavel,
                   COUNT(*)::int AS total,
                   ROUND(AVG(age_hours)::numeric, 1) AS avg_age_hours,
                   ROUND(MAX(age_hours)::numeric, 1) AS max_age_hours,
                   MIN(detected_at) AS oldest_detected_at
            FROM supervisor.v_open_alerts
            GROUP BY area, responsavel
            ORDER BY area, total DESC, responsavel
            """
        )
        areas = await conn.fetch(
            """
            SELECT area,
                   COUNT(*)::int AS total,
                   ROUND(AVG(age_hours)::numeric, 1) AS avg_age_hours,
                   ROUND(MAX(age_hours)::numeric, 1) AS max_age_hours
            FROM supervisor.v_open_alerts
            GROUP BY area
            ORDER BY total DESC, area
            """
        )
    finally:
        await conn.close()
    return web.json_response(
        {
            "ok": True,
            "total_open_alerts": total,
            "areas": [_as_dict(row) for row in areas],
            "groups": [_as_dict(row) for row in groups],
        }
    )


async def _open_alerts(request: web.Request) -> web.Response:
    limit = max(1, min(500, int(request.query.get("limit", "100"))))
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT area,
                   item_type,
                   item_id,
                   responsavel,
                   escalation_level,
                   age_hours,
                   sla_hours,
                   detected_at
            FROM supervisor.v_open_alerts
            ORDER BY area, responsavel, age_hours DESC
            LIMIT $1
            """,
            limit,
        )
    finally:
        await conn.close()
    return web.json_response({"ok": True, "alerts": [_as_dict(row) for row in rows], "limit": limit})


async def _dashboard(request: web.Request) -> web.Response:
    conn = await _connect()
    try:
        summary_rows = await conn.fetch(
            """
            SELECT area,
                   responsavel,
                   COUNT(*)::int AS total,
                   ROUND(AVG(age_hours)::numeric, 1) AS avg_age_hours,
                   ROUND(MAX(age_hours)::numeric, 1) AS max_age_hours,
                   MIN(detected_at) AS oldest_detected_at
            FROM supervisor.v_open_alerts
            GROUP BY area, responsavel
            ORDER BY area, total DESC, responsavel
            """
        )
        alert_rows = await conn.fetch(
            """
            SELECT area,
                   item_type,
                   item_id,
                   responsavel,
                   escalation_level,
                   age_hours,
                   sla_hours,
                   detected_at
            FROM supervisor.v_open_alerts
            ORDER BY area, responsavel, age_hours DESC
            LIMIT 120
            """
        )
    finally:
        await conn.close()

    total = sum(int(row["total"] or 0) for row in summary_rows)
    area_totals: dict[str, int] = {}
    for row in summary_rows:
        area_totals[row["area"]] = area_totals.get(row["area"], 0) + int(row["total"] or 0)

    cards = "".join(
        f"<div class='card'><span>{html.escape(area)}</span><strong>{count}</strong></div>"
        for area, count in sorted(area_totals.items(), key=lambda item: (-item[1], item[0]))
    )
    group_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['area'])}</td>"
        f"<td>{html.escape(row['responsavel'])}</td>"
        f"<td>{row['total']}</td>"
        f"<td>{_fmt_hours(row['avg_age_hours'])}</td>"
        f"<td>{_fmt_hours(row['max_age_hours'])}</td>"
        f"<td>{html.escape(str(row['oldest_detected_at']))}</td>"
        "</tr>"
        for row in summary_rows
    )
    alert_table_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['area'])}</td>"
        f"<td>{html.escape(row['responsavel'])}</td>"
        f"<td>{html.escape(row['item_type'])}</td>"
        f"<td>{html.escape(row['item_id'])}</td>"
        f"<td>{row['escalation_level']}</td>"
        f"<td>{_fmt_hours(row['age_hours'])}</td>"
        f"<td>{_fmt_hours(row['sla_hours'])}</td>"
        f"<td>{html.escape(str(row['detected_at']))}</td>"
        "</tr>"
        for row in alert_rows
    )

    empty = "<p class='empty'>Nenhum alerta aberto no momento.</p>" if total == 0 else ""
    body = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Supervisor SLA</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#101214;color:#f4f7f5}}
main{{max-width:1220px;margin:0 auto;padding:30px 20px 44px}}
h1{{font-size:28px;margin:0 0 6px}}p{{color:#b7c0bd}}.meta{{margin:0 0 22px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0 24px}}
.card{{border:1px solid #2e3733;background:#171b1d;border-radius:8px;padding:14px;min-height:70px}}
.card span{{display:block;color:#aab6b1;font-size:13px;margin-bottom:8px}}.card strong{{font-size:28px}}
table{{width:100%;border-collapse:collapse;margin:14px 0 28px;background:#171b1d;border:1px solid #2e3733}}
th,td{{text-align:left;padding:10px;border-bottom:1px solid #29312e;font-size:14px;vertical-align:top}}
th{{color:#c8d6d1;background:#1f2527}}a{{color:#8fd7ff}}.empty{{padding:16px;border:1px solid #2e3733;border-radius:8px;background:#171b1d}}
@media (max-width:760px){{table{{display:block;overflow-x:auto;white-space:nowrap}}}}
</style></head><body><main>
<h1>Hermes Supervisor SLA</h1>
<p class="meta">Alertas abertos de <code>supervisor.v_open_alerts</code>, agrupados por area e responsavel. Dados sensiveis ficam apenas no backend.</p>
<div class="grid"><div class="card"><span>Total aberto</span><strong>{total}</strong></div>{cards}</div>
{empty}
<h2>Resumo por responsavel</h2>
<table><thead><tr><th>Area</th><th>Responsavel</th><th>Alertas</th><th>Idade media</th><th>Maior idade</th><th>Mais antigo detectado</th></tr></thead><tbody>{group_rows}</tbody></table>
<h2>Amostra operacional</h2>
<table><thead><tr><th>Area</th><th>Responsavel</th><th>Tipo</th><th>Item</th><th>Nivel</th><th>Idade</th><th>SLA</th><th>Detectado em</th></tr></thead><tbody>{alert_table_rows}</tbody></table>
<p>JSON: <a href="/api/supervisor/summary">/api/supervisor/summary</a> · <a href="/api/supervisor/open-alerts?limit=100">/api/supervisor/open-alerts</a></p>
</main></body></html>"""
    return web.Response(text=body, content_type="text/html")


def mount_supervisor_subapp(app: Any, adapter: Any) -> None:
    app.router.add_get("/api/supervisor/summary", _summary)
    app.router.add_get("/api/supervisor/open-alerts", _open_alerts)
    app.router.add_get("/hermes-supervisor", _dashboard)
    app.router.add_get("/hermes-supervisor/", _dashboard)
