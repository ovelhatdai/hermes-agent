"""Read-only kanban API helpers for SPEC-120 smoke/dashboard checks."""
from __future__ import annotations

import html
import json
import os
from typing import Any

import asyncpg
from aiohttp import web

DEFAULT_DATABASE_URL = "postgresql://postgres:EvDb_Adv100k_2026!Migr@127.0.0.1:5432/hermes"


def _db_url() -> str:
    return os.getenv("KANBAN_DATABASE_URL") or os.getenv("HERMES_DB_URL") or DEFAULT_DATABASE_URL


def _row_to_card(row: asyncpg.Record) -> dict[str, Any]:
    item = dict(row)
    for key in ("payload", "result"):
        if item.get(key):
            item[key] = json.loads(item[key])
    return item


async def _list_cards(request: web.Request) -> web.Response:
    limit = max(1, min(100, int(request.query.get("limit", "25"))))
    conn = await asyncpg.connect(_db_url())
    try:
        rows = await conn.fetch(
            """
            SELECT id::text, parent_request_id::text, agent_alvo, skill, payload::text, status,
                   result::text, error, created_at::text, started_at::text, completed_at::text,
                   duration_ms, sla_ms
            FROM kanban.cards
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    finally:
        await conn.close()
    return web.json_response({"ok": True, "cards": [_row_to_card(row) for row in rows]})


async def _metrics(request: web.Request) -> web.Response:
    days = max(1, min(30, int(request.query.get("days", "7"))))
    conn = await asyncpg.connect(_db_url())
    try:
        rows = await conn.fetch(
            """
            WITH base AS (
              SELECT agent_alvo, skill, status, duration_ms, sla_ms, created_at
              FROM kanban.cards
              WHERE created_at >= NOW() - ($1::int * INTERVAL '1 day')
            )
            SELECT agent_alvo, skill,
                   COUNT(*)::int AS total,
                   COUNT(*) FILTER (WHERE status='done')::int AS done,
                   COUNT(*) FILTER (WHERE status='failed')::int AS failed,
                   COUNT(*) FILTER (WHERE status='timeout')::int AS timeout,
                   ROUND(AVG(duration_ms))::int AS avg_duration_ms,
                   MAX(duration_ms)::int AS max_duration_ms,
                   ROUND(AVG(sla_ms))::int AS avg_sla_ms,
                   MAX(created_at)::text AS last_seen_at
            FROM base
            GROUP BY agent_alvo, skill
            ORDER BY total DESC, agent_alvo, skill
            """,
            days,
        )
        total_row = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS total,
                   COUNT(*) FILTER (WHERE status='done')::int AS done,
                   COUNT(*) FILTER (WHERE status='failed')::int AS failed,
                   COUNT(*) FILTER (WHERE status='timeout')::int AS timeout
            FROM kanban.cards
            WHERE created_at >= NOW() - ($1::int * INTERVAL '1 day')
            """,
            days,
        )
    finally:
        await conn.close()

    skills = []
    alerts = []
    for row in rows:
        item = dict(row)
        total = item["total"] or 0
        failed = (item["failed"] or 0) + (item["timeout"] or 0)
        done = item["done"] or 0
        item["success_rate"] = round(done / total, 4) if total else 0
        item["failure_rate"] = round(failed / total, 4) if total else 0
        item["p99_proxy_ms"] = item["max_duration_ms"]
        slow = bool(item["avg_sla_ms"] and item["max_duration_ms"] and item["max_duration_ms"] > 2 * item["avg_sla_ms"])
        bad = total >= 3 and item["failure_rate"] > 0.30
        if bad:
            alerts.append({"level": "critical", "kind": "failure_rate", **item})
        if slow:
            alerts.append({"level": "warning", "kind": "duration_gt_2x_sla", **item})
        skills.append(item)

    summary = dict(total_row or {})
    summary["success_rate"] = round((summary.get("done") or 0) / summary["total"], 4) if summary.get("total") else 0
    summary["failure_rate"] = round(((summary.get("failed") or 0) + (summary.get("timeout") or 0)) / summary["total"], 4) if summary.get("total") else 0
    return web.json_response({"ok": True, "days": days, "summary": summary, "skills": skills, "alerts": alerts})


async def _dashboard(request: web.Request) -> web.Response:
    metrics_response = await _metrics(request)
    data = json.loads(metrics_response.text)
    rows = []
    for s in data["skills"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(s['agent_alvo'])}</td>"
            f"<td>{html.escape(s['skill'])}</td>"
            f"<td>{s['total']}</td>"
            f"<td>{s['done']}</td>"
            f"<td>{s['failed']}</td>"
            f"<td>{s['timeout']}</td>"
            f"<td>{s['success_rate']*100:.1f}%</td>"
            f"<td>{s.get('avg_duration_ms') or '-'}ms</td>"
            f"<td>{s.get('max_duration_ms') or '-'}ms</td>"
            "</tr>"
        )
    alerts = data["alerts"]
    alert_html = "".join(f"<li><strong>{html.escape(a['level'])}</strong> {html.escape(a['kind'])}: {html.escape(a['agent_alvo'])}/{html.escape(a['skill'])}</li>" for a in alerts) or "<li>Nenhum alerta ativo.</li>"
    summary = data["summary"]
    body = f"""<!doctype html>
<html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Hermes Kanban Operacional</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#07110e;color:#edf7ef}}
main{{max-width:1180px;margin:0 auto;padding:32px 20px}}
h1{{font-size:28px;margin:0 0 6px}}p{{color:#b6c9bd}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:22px 0}}
.card{{border:1px solid #264338;background:#0d1d17;border-radius:8px;padding:14px}}.num{{font-size:26px;font-weight:700}}
table{{width:100%;border-collapse:collapse;margin-top:14px;background:#0d1d17;border:1px solid #264338}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #20362d}}th{{color:#b8d8c5}}
ul{{background:#0d1d17;border:1px solid #264338;border-radius:8px;padding:16px 22px}}a{{color:#8ed7ff}}
</style></head><body><main>
<h1>Hermes Kanban Operacional</h1><p>SPEC-120: delegacoes multi-agent, performance por skill e alertas de self-improvement.</p>
<div class=\"grid\"><div class=\"card\"><div>Total</div><div class=\"num\">{summary.get('total',0)}</div></div><div class=\"card\"><div>Done</div><div class=\"num\">{summary.get('done',0)}</div></div><div class=\"card\"><div>Success</div><div class=\"num\">{summary.get('success_rate',0)*100:.1f}%</div></div><div class=\"card\"><div>Falhas</div><div class=\"num\">{(summary.get('failed') or 0)+(summary.get('timeout') or 0)}</div></div></div>
<h2>Alertas</h2><ul>{alert_html}</ul>
<h2>Skills</h2><table><thead><tr><th>Agente</th><th>Skill</th><th>Total</th><th>Done</th><th>Failed</th><th>Timeout</th><th>Success</th><th>Avg</th><th>Max</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>JSON: <a href=\"/api/kanban/metrics\">/api/kanban/metrics</a> · Cards: <a href=\"/api/kanban/cards?limit=20\">/api/kanban/cards</a></p>
</main></body></html>"""
    return web.Response(text=body, content_type="text/html")


def mount_kanban_subapp(app: Any, adapter: Any) -> None:
    app.router.add_get("/api/kanban/cards", _list_cards)
    app.router.add_get("/api/kanban/metrics", _metrics)
    app.router.add_get("/hermes-kanban", _dashboard)
    app.router.add_get("/hermes-kanban/", _dashboard)
