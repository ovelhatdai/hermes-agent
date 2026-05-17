"""SPEC-127.5 — Relatorios Meta Ads sob demanda por mentorada.

Endpoint: POST /api/relatorios/mentoradas

Body:
  {
    "mentoradas": ["luciana", "aline"],   # nomes (substring) OU act_ids OU "todas"
    "periodo_dias": 7,                     # default 7
    "comparativo": true,                   # opcional — comparacao side-by-side
    "enviar_whatsapp": true,               # default true
    "destinatarios": ["vini","joao","grupo_trafego"]  # default
  }

Output:
  {
    "ok": true,
    "html_url": "https://central.advogando100k.com.br/dashboards/relatorios/<slug>.html",
    "mentoradas_resolvidas": [...],
    "destinatarios_notificados": [...]
  }

Faz:
1. Resolve mentoradas (substring ILIKE em ads.accounts.account_name)
2. Query ads.daily_insights agregada por mentorada (account, campaign, ad)
3. Gera HTML interativo (Tailwind + Chart.js + Alpine.js) — clicavel/sortable/comparavel
4. Salva /opt/central-inteligencia/dashboards/relatorios/<slug>.html
5. Envia link via DM (Vini, Joao) + grupo Trafego — opcional via param

Auth: whitelist IP (loopback + bridges Docker).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html as htmllib
import ipaddress
import json
import logging
import os
import re
import secrets
import uuid
import unicodedata
from typing import Any, Optional

import aiohttp
import asyncpg
from aiohttp import web

logger = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "HERMES_DB_URL", "postgresql://postgres@127.0.0.1:5432/hermes"
)

# Pega creds do env de SPEC-130 se HERMES_DB_URL nao apontar pro hermes ads
ADS_DB_URL = os.environ.get("ADS_DB_URL")
if not ADS_DB_URL:
    pg_host = os.environ.get("PGHOST", "127.0.0.1")
    pg_port = os.environ.get("PGPORT", "5432")
    pg_db = os.environ.get("PGDATABASE", "hermes")
    pg_user = os.environ.get("PGUSER", "postgres")
    pg_pass = os.environ.get("PGPASSWORD", "")
    if pg_pass:
        ADS_DB_URL = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
    else:
        ADS_DB_URL = DB_URL

OUT_DIR = "/opt/central-inteligencia/dashboards/relatorios"
PUBLIC_BASE = "https://central.advogando100k.com.br/dashboards/relatorios"

BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "http://127.0.0.1:3000")
GATEWAY_URL = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642")
GATEWAY_TOKEN = os.environ.get("HERMES_GATEWAY_TOKEN", "")

# Default destinatarios — pode sobrescrever via body
DEFAULT_RECIPIENTS = {
    "vini": {"jid": "143658066157619@lid", "label": "Vinicius (DM)"},
    "joao": {"jid": "22767739080766@lid", "label": "Joao Marques (DM)"},
    "grupo_trafego": {"jid": "120363335061065667@g.us", "label": 'Grupo "Trafego Pago - ADV100K"'},
}

_ALLOWED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.20.0.0/16"),
    ipaddress.ip_network("172.17.0.0/16"),
]

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            ADS_DB_URL, min_size=1, max_size=3, command_timeout=20
        )
    return _pool


def _ip_allowed(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in _ALLOWED_NETS)


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii","ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:50]


# ---------- DB queries ----------
async def _resolve_mentoradas(con, queries: list[str]) -> list[dict]:
    """Resolve nomes/act_ids para lista de contas. 'todas' retorna tudo."""
    if not queries or queries == ["todas"]:
        rows = await con.fetch("""
            SELECT ad_account_id, account_name, business_key, status, last_error_code
            FROM ads.accounts
            WHERE business_key='mentorada' AND status<>'inactive'
            ORDER BY account_name
        """)
        return [dict(r) for r in rows]

    found: dict[str, dict] = {}
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        # act_id direto
        if q.startswith("act_"):
            rows = await con.fetch(
                "SELECT ad_account_id, account_name, business_key, status, last_error_code "
                "FROM ads.accounts WHERE ad_account_id=$1", q
            )
        else:
            rows = await con.fetch(
                "SELECT ad_account_id, account_name, business_key, status, last_error_code "
                "FROM ads.accounts WHERE business_key='mentorada' "
                "  AND account_name ILIKE $1 ORDER BY account_name",
                f"%{q}%",
            )
        for r in rows:
            found[r["ad_account_id"]] = dict(r)
    return list(found.values())


async def _aggregate_account(con, ad_account_id: str, days: int) -> dict:
    """KPIs agregados + serie diaria + top campaigns."""
    since = dt.date.today() - dt.timedelta(days=days)

    # KPIs total (level=campaign somando)
    totals = await con.fetchrow("""
        SELECT
          COALESCE(SUM(spend),0)::numeric(12,2) AS spend,
          COALESCE(SUM(impressions),0)::bigint AS impressions,
          COALESCE(SUM(reach),0)::bigint AS reach,
          COALESCE(SUM(clicks),0)::bigint AS clicks,
          COALESCE(SUM(leads),0)::bigint AS leads,
          CASE WHEN SUM(impressions)>0 THEN (SUM(clicks)::numeric/SUM(impressions)*100)::numeric(6,2) ELSE 0 END AS ctr,
          CASE WHEN SUM(clicks)>0 THEN (SUM(spend)/SUM(clicks))::numeric(8,2) ELSE 0 END AS cpc,
          CASE WHEN SUM(impressions)>0 THEN (SUM(spend)/SUM(impressions)*1000)::numeric(8,2) ELSE 0 END AS cpm,
          CASE WHEN SUM(leads)>0 THEN (SUM(spend)/SUM(leads))::numeric(8,2) ELSE 0 END AS cpa
        FROM ads.daily_insights
        WHERE ad_account_id=$1 AND date>=$2 AND level='campaign'
    """, ad_account_id, since)

    # Serie diaria
    daily = await con.fetch("""
        SELECT date::text AS date,
               COALESCE(SUM(spend),0)::numeric(10,2) AS spend,
               COALESCE(SUM(impressions),0) AS impressions,
               COALESCE(SUM(clicks),0) AS clicks,
               COALESCE(SUM(leads),0) AS leads
        FROM ads.daily_insights
        WHERE ad_account_id=$1 AND date>=$2 AND level='campaign'
        GROUP BY date ORDER BY date
    """, ad_account_id, since)

    # Top campanhas
    top = await con.fetch("""
        SELECT campaign_name,
               SUM(spend)::numeric(10,2) AS spend,
               SUM(impressions)::bigint AS impressions,
               SUM(clicks)::bigint AS clicks,
               SUM(leads)::bigint AS leads,
               CASE WHEN SUM(leads)>0 THEN (SUM(spend)/SUM(leads))::numeric(8,2) ELSE NULL END AS cpa
        FROM ads.daily_insights
        WHERE ad_account_id=$1 AND date>=$2 AND level='campaign'
              AND campaign_name IS NOT NULL
        GROUP BY campaign_name
        ORDER BY spend DESC
        LIMIT 8
    """, ad_account_id, since)

    return {
        "totals": dict(totals) if totals else {},
        "daily": [dict(r) for r in daily],
        "top_campaigns": [dict(r) for r in top],
        "since": since,
    }


def _to_jsonable(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    return v


def _serialize(o):
    if isinstance(o, dict):
        return {k: _serialize(_to_jsonable(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_serialize(_to_jsonable(x)) for x in o]
    return _to_jsonable(o)


# ---------- HTML render ----------
def render_html(report: dict) -> str:
    title = report["title"]
    days = report["periodo_dias"]
    mentoradas = report["mentoradas"]  # list[{account_name, ad_account_id, totals, daily, top_campaigns, ...}]
    generated_at = report["generated_at"]

    payload_json = json.dumps(_serialize(mentoradas), ensure_ascii=False)
    title_html = htmllib.escape(title)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{title_html}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<style>
  body {{ font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .kpi {{ transition: all .15s; }}
  .kpi:hover {{ transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.06); }}
  .num-pos {{ color:#15803d; }} .num-neg {{ color:#b91c1c; }}
  table thead th {{ position: sticky; top:0; background:#f9fafb; z-index:1; }}
  .sortable {{ cursor:pointer; user-select:none; }}
  .sortable:hover {{ background:#eef2ff; }}
  .pill-active {{ background:#dcfce7; color:#15803d; }}
  .pill-pending {{ background:#fef3c7; color:#a16207; }}
  .pill-disabled {{ background:#fee2e2; color:#b91c1c; }}
</style>
</head>
<body class="bg-gray-50 text-gray-900" x-data="dashboard()">
  <div class="max-w-screen-2xl mx-auto p-4 lg:p-6">
    <header class="mb-6 flex flex-col lg:flex-row lg:items-center lg:justify-between gap-2">
      <div>
        <h1 class="text-2xl lg:text-3xl font-semibold">{title_html}</h1>
        <p class="text-sm text-gray-500">Periodo: ultimos <strong>{days}</strong> dias · gerado em {generated_at} BRT</p>
      </div>
      <div class="flex gap-2 items-center text-sm">
        <input type="text" x-model="filterText" placeholder="Filtrar mentorada..."
               class="border rounded px-3 py-1.5 w-64 focus:ring-2 focus:ring-blue-300 focus:outline-none">
        <select x-model="sortKey" class="border rounded px-2 py-1.5">
          <option value="spend">Spend</option>
          <option value="leads">Leads</option>
          <option value="cpa">CPA</option>
          <option value="ctr">CTR</option>
          <option value="impressions">Impressoes</option>
          <option value="account_name">Nome</option>
        </select>
        <button @click="sortDesc=!sortDesc" class="border rounded px-2 py-1.5 text-xs">
          <span x-text="sortDesc ? '▼' : '▲'"></span>
        </button>
      </div>
    </header>

    <!-- KPIs gerais -->
    <section class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-6">
      <template x-for="kpi in summary" :key="kpi.label">
        <div class="kpi bg-white rounded-lg p-3 shadow-sm border">
          <div class="text-xs text-gray-500 uppercase tracking-wide" x-text="kpi.label"></div>
          <div class="text-2xl font-semibold mt-1" x-text="kpi.value"></div>
        </div>
      </template>
    </section>

    <!-- Tabela principal sortable -->
    <section class="bg-white rounded-lg shadow-sm border mb-6">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-xs uppercase text-gray-600 border-b">
          <tr>
            <th class="text-left p-2.5 sortable" @click="setSort('account_name')">Mentorada</th>
            <th class="text-right p-2.5 sortable" @click="setSort('spend')">Spend (R$)</th>
            <th class="text-right p-2.5 sortable" @click="setSort('impressions')">Imp.</th>
            <th class="text-right p-2.5 sortable" @click="setSort('clicks')">Clicks</th>
            <th class="text-right p-2.5 sortable" @click="setSort('ctr')">CTR %</th>
            <th class="text-right p-2.5 sortable" @click="setSort('cpc')">CPC</th>
            <th class="text-right p-2.5 sortable" @click="setSort('leads')">Leads</th>
            <th class="text-right p-2.5 sortable" @click="setSort('cpa')">CPA</th>
            <th class="text-center p-2.5">Status</th>
            <th class="text-center p-2.5">Ações</th>
          </tr>
        </thead>
        <tbody>
          <template x-for="m in filteredSorted" :key="m.ad_account_id">
            <tr class="border-b hover:bg-blue-50 cursor-pointer" @click="select(m)">
              <td class="p-2.5 font-medium" x-text="m.account_name"></td>
              <td class="p-2.5 text-right" x-text="fmtBRL(m.totals.spend)"></td>
              <td class="p-2.5 text-right" x-text="fmtNum(m.totals.impressions)"></td>
              <td class="p-2.5 text-right" x-text="fmtNum(m.totals.clicks)"></td>
              <td class="p-2.5 text-right" x-text="fmtPct(m.totals.ctr)"></td>
              <td class="p-2.5 text-right" x-text="fmtBRL(m.totals.cpc)"></td>
              <td class="p-2.5 text-right font-medium" x-text="fmtNum(m.totals.leads)"></td>
              <td class="p-2.5 text-right" x-text="m.totals.cpa>0?fmtBRL(m.totals.cpa):'—'"></td>
              <td class="p-2.5 text-center">
                <span class="px-2 py-0.5 rounded-full text-xs"
                      :class="{{'pill-active':m.status==='active','pill-pending':m.status==='permission_pending','pill-disabled':m.status==='disabled'||m.status==='inactive'}}"
                      x-text="m.status"></span>
              </td>
              <td class="p-2.5 text-center">
                <a :href="'https://business.facebook.com/adsmanager/manage/campaigns?act='+m.ad_account_id.replace('act_','')"
                   target="_blank" @click.stop class="text-blue-600 hover:underline text-xs">Gerenciador →</a>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </section>

    <!-- Detalhe mentorada selecionada -->
    <section x-show="selected" x-cloak class="bg-white rounded-lg shadow-sm border p-4 mb-6">
      <header class="flex justify-between items-center mb-3">
        <h2 class="text-xl font-semibold" x-text="selected ? selected.account_name : ''"></h2>
        <button @click="selected=null" class="text-sm text-gray-500">Fechar ✕</button>
      </header>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <h3 class="text-sm uppercase text-gray-500 mb-2">Tendencia diaria</h3>
          <canvas id="trendChart" height="160"></canvas>
        </div>
        <div>
          <h3 class="text-sm uppercase text-gray-500 mb-2">Top campanhas</h3>
          <table class="w-full text-xs">
            <thead><tr class="text-gray-500 border-b"><th class="text-left p-1.5">Campanha</th><th class="text-right p-1.5">Spend</th><th class="text-right p-1.5">Leads</th><th class="text-right p-1.5">CPA</th></tr></thead>
            <tbody>
              <template x-for="c in (selected ? selected.top_campaigns : [])" :key="c.campaign_name">
                <tr class="border-b">
                  <td class="p-1.5" x-text="c.campaign_name"></td>
                  <td class="p-1.5 text-right" x-text="fmtBRL(c.spend)"></td>
                  <td class="p-1.5 text-right" x-text="fmtNum(c.leads)"></td>
                  <td class="p-1.5 text-right" x-text="c.cpa>0?fmtBRL(c.cpa):'—'"></td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <footer class="text-xs text-gray-500 text-center mt-6">
      <p>Hermes · Relatorio Mentoradas · Auto-gerado · <a href="https://central.advogando100k.com.br/dashboards/" class="underline">central</a></p>
    </footer>
  </div>

<script>
const REPORT_DATA = {payload_json};

function dashboard() {{
  return {{
    data: REPORT_DATA,
    filterText: '',
    sortKey: 'spend',
    sortDesc: true,
    selected: null,
    chart: null,

    get filteredSorted() {{
      let arr = this.data.slice();
      if (this.filterText) {{
        const q = this.filterText.toLowerCase();
        arr = arr.filter(m => m.account_name.toLowerCase().includes(q));
      }}
      const k = this.sortKey;
      arr.sort((a,b) => {{
        const va = (k==='account_name') ? a.account_name : (a.totals[k]||0);
        const vb = (k==='account_name') ? b.account_name : (b.totals[k]||0);
        if (k==='account_name') {{
          return this.sortDesc ? vb.localeCompare(va) : va.localeCompare(vb);
        }}
        return this.sortDesc ? (vb-va) : (va-vb);
      }});
      return arr;
    }},

    get summary() {{
      const T = this.data.reduce((acc,m) => {{
        acc.spend += +m.totals.spend||0;
        acc.imp += +m.totals.impressions||0;
        acc.clicks += +m.totals.clicks||0;
        acc.leads += +m.totals.leads||0;
        return acc;
      }}, {{spend:0, imp:0, clicks:0, leads:0}});
      return [
        {{label:'Mentoradas', value: this.data.length}},
        {{label:'Spend total', value: this.fmtBRL(T.spend)}},
        {{label:'Impressoes', value: this.fmtNum(T.imp)}},
        {{label:'Clicks', value: this.fmtNum(T.clicks)}},
        {{label:'Leads', value: this.fmtNum(T.leads)}},
        {{label:'CPA medio', value: T.leads>0 ? this.fmtBRL(T.spend/T.leads) : '—'}},
      ];
    }},

    setSort(k) {{
      if (this.sortKey === k) this.sortDesc = !this.sortDesc;
      else {{ this.sortKey = k; this.sortDesc = true; }}
    }},

    select(m) {{
      this.selected = m;
      this.$nextTick(() => this.renderChart(m));
    }},

    renderChart(m) {{
      const ctx = document.getElementById('trendChart');
      if (!ctx) return;
      if (this.chart) this.chart.destroy();
      this.chart = new Chart(ctx, {{
        type:'line',
        data: {{
          labels: m.daily.map(d => d.date.slice(5)),
          datasets:[
            {{label:'Spend (R$)', data:m.daily.map(d=>+d.spend), borderColor:'#2563eb', backgroundColor:'#bfdbfe', tension:.3, yAxisID:'y'}},
            {{label:'Leads', data:m.daily.map(d=>+d.leads), borderColor:'#16a34a', backgroundColor:'#bbf7d0', tension:.3, yAxisID:'y2'}}
          ]
        }},
        options: {{
          interaction:{{mode:'index',intersect:false}},
          scales:{{
            y:{{position:'left', title:{{display:true,text:'Spend (R$)'}}}},
            y2:{{position:'right', title:{{display:true,text:'Leads'}}, grid:{{drawOnChartArea:false}}}}
          }}
        }}
      }});
    }},

    fmtBRL(n) {{
      const v = Number(n||0);
      return v.toLocaleString('pt-BR', {{style:'currency', currency:'BRL'}});
    }},
    fmtNum(n) {{ return Number(n||0).toLocaleString('pt-BR'); }},
    fmtPct(n) {{ return (Number(n||0).toFixed(2)) + '%'; }}
  }};
}}
</script>
</body>
</html>
"""


# ---------- WhatsApp dispatch ----------
async def _send_dm(jid: str, message: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{BRIDGE_URL}/send",
            json={"chatId": jid, "message": message},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            try: body = await r.json()
            except Exception: body = {"raw": await r.text()}
            return {"status": r.status, "body": body}


async def _send_group(jid: str, message: str) -> dict:
    if not GATEWAY_TOKEN:
        return {"status": 0, "error": "HERMES_GATEWAY_TOKEN ausente no env"}
    headers = {
        "Authorization": f"Bearer {GATEWAY_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "groupJids": [jid],
        "message": message,
        "idempotency_key": str(uuid.uuid4()),
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{GATEWAY_URL}/api/gateway/groups/broadcast",
            headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            try: ret = await r.json()
            except Exception: ret = {"raw": await r.text()}
            return {"status": r.status, "body": ret}


async def _dispatch(recipients_keys: list[str], message: str) -> list[dict]:
    out = []
    for key in recipients_keys:
        rec = DEFAULT_RECIPIENTS.get(key)
        if not rec:
            out.append({"recipient": key, "error": "unknown_key"})
            continue
        jid = rec["jid"]
        try:
            if jid.endswith("@g.us"):
                r = await _send_group(jid, message)
            else:
                r = await _send_dm(jid, message)
            out.append({"recipient": key, "label": rec["label"], "jid": jid, **r})
        except Exception as e:
            out.append({"recipient": key, "label": rec["label"], "jid": jid, "error": str(e)[:200]})
    return out


# ---------- handler ----------
async def handle_relatorio_mentoradas(request: web.Request) -> web.Response:
    src_ip = request.remote
    if not _ip_allowed(src_ip):
        return web.json_response({"error":"forbidden","ip":src_ip}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error":"invalid_json"}, status=400)

    queries = body.get("mentoradas") or ["todas"]
    if isinstance(queries, str):
        queries = [queries]
    days = int(body.get("periodo_dias", 7))
    enviar = bool(body.get("enviar_whatsapp", True))
    recipients = body.get("destinatarios") or ["vini","joao","grupo_trafego"]

    pool = await _get_pool()
    async with pool.acquire() as con:
        accounts = await _resolve_mentoradas(con, queries)
        if not accounts:
            return web.json_response(
                {"error":"sem_match","queries":queries},
                status=404,
            )
        for acc in accounts:
            agg = await _aggregate_account(con, acc["ad_account_id"], days)
            acc.update(agg)

    title_qs = ", ".join(queries) if queries != ["todas"] else "todas as mentoradas"
    title = f"Relatorio Mentoradas — {title_qs}"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    report = {
        "title": title,
        "periodo_dias": days,
        "mentoradas": accounts,
        "generated_at": now,
    }
    html = render_html(report)

    # Slug e nome do arquivo
    slug = _slugify(title_qs)[:40] or "relatorio"
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{slug}-{ts}.html"
    out_path = os.path.join(OUT_DIR, filename)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    public_url = f"{PUBLIC_BASE}/{filename}"

    # Mensagem WhatsApp
    n = len(accounts)
    total_spend = sum(float((m.get("totals") or {}).get("spend") or 0) for m in accounts)
    total_leads = sum(int((m.get("totals") or {}).get("leads") or 0) for m in accounts)
    cpa_geral = (total_spend / total_leads) if total_leads > 0 else 0
    spend_fmt = f"R$ {total_spend:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    cpa_fmt = (f"R$ {cpa_geral:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")) if cpa_geral>0 else "—"
    msg = (
        f"📊 *Relatorio Mentoradas — ultimos {days}d*\n"
        f"{n} mentoradas · spend {spend_fmt} · leads {total_leads:,}".replace(",",".") +
        f" · CPA medio {cpa_fmt}\n\n"
        f"Dashboard interativo (clicavel + filtravel + sortable):\n{public_url}"
    )

    notified = []
    if enviar:
        notified = await _dispatch(recipients, msg)

    return web.json_response({
        "ok": True,
        "html_url": public_url,
        "html_path": out_path,
        "mentoradas_resolvidas": [
            {"account_name":a["account_name"], "ad_account_id":a["ad_account_id"], "status":a["status"]}
            for a in accounts
        ],
        "totais": {"spend": float(total_spend), "leads": total_leads, "cpa": float(cpa_geral)},
        "destinatarios_notificados": notified if enviar else [],
    })


# ---------- mount ----------
def mount_relatorios_subapp(parent_app: web.Application, adapter: Any) -> None:
    parent_app.router.add_post("/api/relatorios/mentoradas", handle_relatorio_mentoradas)
    logger.info("[custom_extensions] relatorios route mounted: POST /api/relatorios/mentoradas")
