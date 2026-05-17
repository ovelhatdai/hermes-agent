"""Read-only Hermes Supervisor dashboard/API for SPEC-119."""
from __future__ import annotations

import html
import json
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


async def _goals_list(request: web.Request) -> web.Response:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT g.id,
                   g.nome,
                   g.descricao,
                   g.metric_sql,
                   g.threshold_expr,
                   g.action_auto_json,
                   g.action_pede_ok_json,
                   g.enabled,
                   g.cooldown_minutes,
                   g.last_fired_at,
                   g.total_fires,
                   g.updated_at,
                   COUNT(f.id)::int AS fires_30d,
                   COUNT(*) FILTER (WHERE f.action_taken LIKE 'auto%')::int AS auto_30d,
                   COUNT(*) FILTER (WHERE f.action_taken LIKE '%pediu_ok%')::int AS pediu_ok_30d,
                   MAX(f.fired_at) AS latest_fire_at
            FROM supervisor.goals g
            LEFT JOIN supervisor.goal_fires f
              ON f.goal_id = g.id
             AND f.fired_at > NOW() - INTERVAL '30 days'
            GROUP BY g.id
            ORDER BY g.enabled DESC, g.id
            """
        )
    finally:
        await conn.close()
    return web.json_response({"ok": True, "goals": [_as_dict(row) for row in rows]})


async def _goal_create(request: web.Request) -> web.Response:
    payload = await request.json()
    required = ("nome", "metric_sql", "threshold_expr", "action_auto_json")
    missing = [key for key in required if key not in payload]
    if missing:
        return web.json_response({"ok": False, "error": f"campos ausentes: {', '.join(missing)}"}, status=400)
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO supervisor.goals
              (nome, descricao, metric_sql, threshold_expr, action_auto_json, action_pede_ok_json, enabled, cooldown_minutes)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, COALESCE($7, true), COALESCE($8, 60))
            RETURNING *
            """,
            payload["nome"],
            payload.get("descricao"),
            payload["metric_sql"],
            payload["threshold_expr"],
            json.dumps(payload["action_auto_json"]),
            json.dumps(payload["action_pede_ok_json"]) if payload.get("action_pede_ok_json") is not None else None,
            payload.get("enabled"),
            payload.get("cooldown_minutes"),
        )
    finally:
        await conn.close()
    return web.json_response({"ok": True, "goal": _as_dict(row)}, status=201)


async def _goal_patch(request: web.Request) -> web.Response:
    goal_id = int(request.match_info["goal_id"])
    payload = await request.json()
    allowed = {
        "nome",
        "descricao",
        "metric_sql",
        "threshold_expr",
        "action_auto_json",
        "action_pede_ok_json",
        "enabled",
        "cooldown_minutes",
    }
    updates = [key for key in payload if key in allowed]
    if not updates:
        return web.json_response({"ok": False, "error": "nenhum campo atualizavel enviado"}, status=400)
    set_parts = []
    values: list[Any] = []
    for key in updates:
        values.append(json.dumps(payload[key]) if key.endswith("_json") and payload[key] is not None else payload[key])
        cast = "::jsonb" if key.endswith("_json") else ""
        set_parts.append(f"{key} = ${len(values) + 1}{cast}")
    values.insert(0, goal_id)
    sql = f"UPDATE supervisor.goals SET {', '.join(set_parts)}, updated_at = now() WHERE id = $1 RETURNING *"
    conn = await _connect()
    try:
        row = await conn.fetchrow(sql, *values)
    finally:
        await conn.close()
    if not row:
        return web.json_response({"ok": False, "error": "goal nao encontrado"}, status=404)
    return web.json_response({"ok": True, "goal": _as_dict(row)})


async def _goal_delete(request: web.Request) -> web.Response:
    goal_id = int(request.match_info["goal_id"])
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "UPDATE supervisor.goals SET enabled = false, updated_at = now() WHERE id = $1 RETURNING id, nome, enabled",
            goal_id,
        )
    finally:
        await conn.close()
    if not row:
        return web.json_response({"ok": False, "error": "goal nao encontrado"}, status=404)
    return web.json_response({"ok": True, "goal": _as_dict(row)})


async def _goal_fires(request: web.Request) -> web.Response:
    goal_id = request.query.get("goal_id")
    limit = max(1, min(500, int(request.query.get("limit", "100"))))
    where = "WHERE ($1::int IS NULL OR f.goal_id = $1)"
    conn = await _connect()
    try:
        rows = await conn.fetch(
            f"""
            SELECT f.id,
                   f.goal_id,
                   g.nome AS goal_nome,
                   f.fired_at,
                   f.metric_value,
                   f.action_taken,
                   f.ok_response,
                   f.resolved_at,
                   f.dedup_key,
                   f.action_result
            FROM supervisor.goal_fires f
            JOIN supervisor.goals g ON g.id = f.goal_id
            {where}
            ORDER BY f.fired_at DESC
            LIMIT $2
            """,
            int(goal_id) if goal_id else None,
            limit,
        )
    finally:
        await conn.close()
    return web.json_response({"ok": True, "fires": [_as_dict(row) for row in rows], "limit": limit})


async def _goals_dashboard(request: web.Request) -> web.Response:
    conn = await _connect()
    try:
        goals = await conn.fetch(
            """
            SELECT g.id,
                   g.nome,
                   g.descricao,
                   g.enabled,
                   g.cooldown_minutes,
                   g.last_fired_at,
                   g.total_fires,
                   COUNT(f.id)::int AS fires_30d,
                   COUNT(*) FILTER (WHERE f.action_taken LIKE 'auto%')::int AS auto_30d,
                   COUNT(*) FILTER (WHERE f.action_taken LIKE '%pediu_ok%')::int AS pediu_ok_30d
            FROM supervisor.goals g
            LEFT JOIN supervisor.goal_fires f
              ON f.goal_id = g.id
             AND f.fired_at > NOW() - INTERVAL '30 days'
            GROUP BY g.id
            ORDER BY g.enabled DESC, g.id
            """
        )
        fires = await conn.fetch(
            """
            SELECT f.id, g.nome, f.fired_at, f.action_taken, f.metric_value
            FROM supervisor.goal_fires f
            JOIN supervisor.goals g ON g.id = f.goal_id
            ORDER BY f.fired_at DESC
            LIMIT 80
            """
        )
        daily = await conn.fetch(
            """
            SELECT date_trunc('day', fired_at)::date AS day, COUNT(*)::int AS total
            FROM supervisor.goal_fires
            WHERE fired_at > NOW() - INTERVAL '30 days'
            GROUP BY 1
            ORDER BY 1
            """
        )
    finally:
        await conn.close()

    goal_rows = "".join(
        "<tr>"
        f"<td>{row['id']}</td>"
        f"<td><strong>{html.escape(row['nome'])}</strong><br><span>{html.escape(row['descricao'] or '')}</span></td>"
        f"<td>{'ativo' if row['enabled'] else 'off'}</td>"
        f"<td>{row['cooldown_minutes']}min</td>"
        f"<td>{html.escape(str(row['last_fired_at'] or '-'))}</td>"
        f"<td>{row['total_fires']}</td>"
        f"<td>{row['auto_30d']} / {row['pediu_ok_30d']}</td>"
        f"<td><button data-goal='{row['id']}'>desativar</button></td>"
        "</tr>"
        for row in goals
    )
    fire_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['fired_at']))}</td>"
        f"<td>{html.escape(row['nome'])}</td>"
        f"<td>{html.escape(row['action_taken'] or '')}</td>"
        f"<td><code>{html.escape(json.dumps(_json_value(row['metric_value']), ensure_ascii=False, default=str)[:240])}</code></td>"
        "</tr>"
        for row in fires
    )
    bars = "".join(
        f"<div class='bar' style='height:{max(8, min(120, int(row['total']) * 14))}px'><span>{row['total']}</span><small>{row['day'].strftime('%d/%m')}</small></div>"
        for row in daily
    )
    body = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Goals Operacionais</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#111413;color:#f5f7f2}}
main{{max-width:1240px;margin:0 auto;padding:28px 18px 44px}}h1{{font-size:28px;margin:0 0 6px}}p{{color:#b8c0ba}}
table{{width:100%;border-collapse:collapse;background:#171b19;border:1px solid #303832;margin:16px 0 30px}}
th,td{{padding:10px;border-bottom:1px solid #2b332e;text-align:left;vertical-align:top;font-size:14px}}th{{background:#202722;color:#d9e2da}}
td span{{color:#aab5ac}}code{{white-space:normal;color:#d4e7ff}}button{{border:1px solid #536056;background:#202822;color:#eef7ef;border-radius:6px;padding:7px 10px;cursor:pointer}}
.bars{{height:150px;display:flex;align-items:flex-end;gap:8px;border:1px solid #303832;background:#171b19;padding:14px;margin:18px 0 28px}}
.bar{{width:36px;background:#70b48d;display:flex;flex-direction:column;justify-content:space-between;align-items:center;color:#08110c;border-radius:4px 4px 0 0;font-size:12px;padding-top:4px}}.bar small{{transform:translateY(20px);color:#cbd5ce}}
a{{color:#91cdf7}}@media (max-width:760px){{table{{display:block;overflow-x:auto;white-space:nowrap}}}}
</style></head><body><main>
<h1>Hermes Goals Operacionais</h1>
<p>Vigilancia automatica de bugs conhecidos: metrica, limite, acao automatica e pedido de ok quando precisa de decisao humana.</p>
<h2>Goals ativos</h2>
<table><thead><tr><th>ID</th><th>Goal</th><th>Status</th><th>Cooldown</th><th>Ultimo disparo</th><th>Total</th><th>Auto / pediu ok 30d</th><th>Controle</th></tr></thead><tbody>{goal_rows}</tbody></table>
<h2>Disparos por dia</h2><div class="bars">{bars or '<p>Sem disparos nos ultimos 30 dias.</p>'}</div>
<h2>Historico recente</h2>
<table><thead><tr><th>Quando</th><th>Goal</th><th>Acao</th><th>Metrica</th></tr></thead><tbody>{fire_rows}</tbody></table>
<p>JSON: <a href="/api/supervisor/goals">/api/supervisor/goals</a> · <a href="/api/supervisor/goal_fires">/api/supervisor/goal_fires</a></p>
<script>
document.querySelectorAll('button[data-goal]').forEach(btn => btn.addEventListener('click', async () => {{
  if (!confirm('Desativar este Goal?')) return;
  const id = btn.getAttribute('data-goal');
  await fetch('/api/supervisor/goals/' + id, {{method:'DELETE'}});
  location.reload();
}}));
</script></main></body></html>"""
    return web.Response(text=body, content_type="text/html")


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
    app.router.add_get("/api/supervisor/goals", _goals_list)
    app.router.add_post("/api/supervisor/goals", _goal_create)
    app.router.add_patch("/api/supervisor/goals/{goal_id:\\d+}", _goal_patch)
    app.router.add_delete("/api/supervisor/goals/{goal_id:\\d+}", _goal_delete)
    app.router.add_get("/api/supervisor/goal_fires", _goal_fires)
    app.router.add_get("/hermes-supervisor", _dashboard)
    app.router.add_get("/hermes-supervisor/", _dashboard)
    app.router.add_get("/hermes-goals", _goals_dashboard)
    app.router.add_get("/hermes-goals/", _goals_dashboard)
