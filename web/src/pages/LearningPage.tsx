import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Brain, RefreshCw, TrendingDown, ThumbsUp, ThumbsDown } from "lucide-react";
import { api, type LearningDashboardResponse } from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { usePageHeader } from "@/contexts/usePageHeader";
import { PluginSlot } from "@/plugins";

const RANGES = ["7d", "30d", "90d"] as const;
type Range = (typeof RANGES)[number];

function compact(n: number): string {
  return new Intl.NumberFormat("pt-BR", { notation: n >= 10000 ? "compact" : "standard" }).format(n || 0);
}

function MiniBars({ rows }: { rows: Array<{ name: string; uses: number }> }) {
  const max = Math.max(...rows.map((r) => r.uses), 1);
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">Sem dados suficientes ainda.</p>;
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.name} className="grid grid-cols-[minmax(0,1fr)_80px] items-center gap-3 text-sm">
          <div className="min-w-0">
            <div className="mb-1 truncate text-foreground">{row.name}</div>
            <div className="h-2 bg-muted">
              <div className="h-2 bg-emerald-500" style={{ width: `${Math.max(4, (row.uses / max) * 100)}%` }} />
            </div>
          </div>
          <div className="text-right font-mono text-muted-foreground">{compact(row.uses)}</div>
        </div>
      ))}
    </div>
  );
}

function FeedbackList({ title, icon, rows }: { title: string; icon: ReactNode; rows: Array<{ hash: string; count: number }> }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          {icon}
          <CardTitle className="text-base">{title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">Sem feedback neste período.</p>
        ) : (
          <div className="space-y-2">
            {rows.map((row) => (
              <div key={row.hash} className="flex items-center justify-between border-b border-border pb-2 text-sm last:border-0 last:pb-0">
                <span className="font-mono">#{row.hash}</span>
                <span className="text-muted-foreground">{row.count}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function LearningPage() {
  const [range, setRange] = useState<Range>("30d");
  const [data, setData] = useState<LearningDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setEnd } = usePageHeader();

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getLearningDashboard(range));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [range]);

  useEffect(() => {
    setEnd(
      <div className="flex items-center gap-2">
        <select className="h-8 border border-border bg-card px-2 text-sm" value={range} onChange={(e) => setRange(e.target.value as Range)}>
          {RANGES.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
        <Button ghost size="sm" onClick={load} disabled={loading}>
          {loading ? <Spinner /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>,
    );
    return () => setEnd(null);
  }, [range, loading, setEnd]);

  const feedbackTotals = useMemo(() => {
    const positive = data?.top_positive.reduce((sum, row) => sum + row.count, 0) ?? 0;
    const negative = data?.top_negative.reduce((sum, row) => sum + row.count, 0) ?? 0;
    return { positive, negative };
  }, [data]);

  return (
    <div className="space-y-5">
      <PluginSlot name="learning:top" />
      <div>
        <div className="flex items-center gap-2 text-muted-foreground">
          <Brain className="h-5 w-5" />
          <span className="text-xs uppercase tracking-[0.18em]">Hermes</span>
        </div>
        <h1 className="mt-1 text-2xl font-semibold text-foreground">Aprendizado</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">Uso, feedback e drift das respostas avaliadas por Vini e Daiane.</p>
      </div>

      {error && <div className="border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>}
      {loading && !data ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Carregando aprendizado...</div>
      ) : data ? (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <Card><CardHeader><CardTitle className="text-sm">Uso total</CardTitle></CardHeader><CardContent><div className="text-3xl font-semibold">{compact(data.usage_total)}</div><p className="text-xs text-muted-foreground">eventos em {range}</p></CardContent></Card>
            <Card><CardHeader><CardTitle className="text-sm">Cobertura 7d</CardTitle></CardHeader><CardContent><div className="text-3xl font-semibold">{compact(data.feedback_coverage_7d)}</div><p className="text-xs text-muted-foreground">respostas avaliadas</p></CardContent></Card>
            <Card><CardHeader><CardTitle className="text-sm">Positivos</CardTitle></CardHeader><CardContent><div className="text-3xl font-semibold text-emerald-500">{compact(feedbackTotals.positive)}</div></CardContent></Card>
            <Card><CardHeader><CardTitle className="text-sm">Negativos</CardTitle></CardHeader><CardContent><div className="text-3xl font-semibold text-red-500">{compact(feedbackTotals.negative)}</div></CardContent></Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader><CardTitle className="text-base">Top usos</CardTitle></CardHeader>
              <CardContent><MiniBars rows={data.top_skills.map((row) => ({ name: `${row.feature}/${row.name}`, uses: row.uses }))} /></CardContent>
            </Card>
            <div className="grid gap-4 sm:grid-cols-2">
              <FeedbackList title="Feedback positivo" icon={<ThumbsUp className="h-4 w-4 text-emerald-500" />} rows={data.top_positive} />
              <FeedbackList title="Feedback negativo" icon={<ThumbsDown className="h-4 w-4 text-red-500" />} rows={data.top_negative} />
            </div>
          </div>

          <Card>
            <CardHeader><div className="flex items-center gap-2"><TrendingDown className="h-5 w-5 text-muted-foreground" /><CardTitle className="text-base">Drift detectado</CardTitle></div></CardHeader>
            <CardContent>
              {data.drift_alerts.length === 0 ? (
                <p className="text-sm text-emerald-500">Nenhum drift significativo nos últimos 30 dias.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead><tr className="border-b border-border text-muted-foreground"><th className="py-2 text-left">Feature</th><th className="py-2 text-left">Nome</th><th className="py-2 text-right">Antes</th><th className="py-2 text-right">Agora</th><th className="py-2 text-right">Retenção</th></tr></thead>
                    <tbody>{data.drift_alerts.map((row) => <tr key={`${row.feature}:${row.name}`} className="border-b border-border last:border-0"><td className="py-2">{row.feature}</td><td className="py-2">{row.name}</td><td className="py-2 text-right">{row.prev_uses}</td><td className="py-2 text-right">{row.current_uses}</td><td className="py-2 text-right text-red-500">{row.retention_pct}%</td></tr>)}</tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      ) : null}
      <PluginSlot name="learning:bottom" />
    </div>
  );
}
