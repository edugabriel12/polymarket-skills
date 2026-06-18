import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  TrendingUp,
  DollarSign,
  Target,
  ArrowUpCircle,
  ArrowDownCircle,
  ExternalLink,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { api, type Period, type Sport } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { KpiCard } from "@/components/KpiCard";
import { StatusBadge } from "@/components/StatusBadge";
import { WinRateDonut, PnlBar, OverUnderSplit } from "@/components/charts";
import { cn, pct, signedUsd, usd } from "@/lib/utils";

const PERIODS: { key: Period; label: string }[] = [
  { key: "daily", label: "Diário" },
  { key: "weekly", label: "Semanal" },
  { key: "monthly", label: "Mensal" },
];

const PAGE_SIZE = 15;

export function ResultsTab({ sport }: { sport: Sport }) {
  const [period, setPeriod] = useState<Period>("monthly");
  const [page, setPage] = useState(0);
  // refetchOnMount + always-stale => every visit/interaction triggers backend settlement.
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["results", sport],
    queryFn: () => api.results(sport),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });

  const b = data?.performance[period];
  const recent = data?.recent ?? [];
  const pageCount = Math.max(1, Math.ceil(recent.length / PAGE_SIZE));
  // Reset to the first page whenever the dataset or sport changes.
  useEffect(() => setPage(0), [sport, recent.length]);
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = recent.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-xl border border-border bg-card p-1">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              onClick={() => setPeriod(p.key)}
              className={cn(
                "rounded-lg px-3 py-1.5 text-sm font-semibold transition-all",
                period === p.key
                  ? "bg-gradient-to-r from-emerald-500 to-sky-500 text-white shadow"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {data && <span>{data.settlement.checked} pendentes verificadas</span>}
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            Atualizar
          </Button>
        </div>
      </div>

      {isLoading || !b ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="skeleton h-28" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <KpiCard label="ROI" value={pct(b.roi)} sub={`${b.settled} liquidadas`} icon={TrendingUp}
              gradient="from-emerald-500 to-teal-600" positive={b.roi !== null ? b.roi >= 0 : null} />
            <KpiCard label="P&L" value={signedUsd(b.pnl)} sub={`investido ${usd(b.invested)}`} icon={DollarSign}
              gradient="from-sky-500 to-indigo-600" positive={b.pnl >= 0} />
            <KpiCard label="Win rate" value={pct(b.win_rate)} sub={`${b.counts.acerto}A · ${b.counts.erro}E`} icon={Target}
              gradient="from-violet-500 to-fuchsia-600" />
            <KpiCard label="Win Over" value={pct(b.win_rate_over)} sub="entradas no Over" icon={ArrowUpCircle}
              gradient="from-cyan-500 to-blue-600" />
            <KpiCard label="Win Under" value={pct(b.win_rate_under)} sub="entradas no Under" icon={ArrowDownCircle}
              gradient="from-fuchsia-500 to-purple-600" />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <CardHeader><CardTitle>Acerto vs Erro</CardTitle></CardHeader>
              <CardContent><WinRateDonut block={b} /></CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Win rate Over / Under</CardTitle></CardHeader>
              <CardContent><OverUnderSplit block={b} /></CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>P&L por dia</CardTitle></CardHeader>
              <CardContent><PnlBar data={data!.pnl_by_day} /></CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
              <CardTitle>Previsões recentes</CardTitle>
              {recent.length > 0 && (
                <span className="text-xs font-normal text-muted-foreground">
                  {recent.length} no total
                </span>
              )}
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="py-2 pr-3">Jogo</th>
                    <th className="px-3">Mercado</th>
                    <th className="px-3">Lado</th>
                    <th className="px-3">Linha</th>
                    <th className="px-3">Preço</th>
                    <th className="px-3">Status</th>
                    <th className="px-3">Link</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map((r) => (
                    <tr key={r.id} className="border-b border-border/60">
                      <td className="py-2 pr-3 font-medium">
                        {r.game_slug.replace(/^[a-z0-9]+-/, "").replace(/-\d{4}-\d{2}-\d{2}.*$/, "")}
                      </td>
                      <td className="px-3 text-xs text-muted-foreground">{r.market ?? "TOTAL"}</td>
                      <td className="px-3">
                        <span className={["OVER", "YES"].includes(r.side) ? "font-bold text-sky-400" : "font-bold text-violet-400"}>{r.side}</span>
                      </td>
                      <td className="px-3 tabular-nums">{r.line && r.line > 0 ? r.line : "—"}</td>
                      <td className="px-3 tabular-nums">{r.entry_price?.toFixed(2)}</td>
                      <td className="px-3"><StatusBadge status={r.status} /></td>
                      <td className="px-3">
                        {r.market_url && (
                          <a href={r.market_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">
                            <ExternalLink className="h-4 w-4" />
                          </a>
                        )}
                      </td>
                    </tr>
                  ))}
                  {pageRows.length === 0 && (
                    <tr>
                      <td colSpan={7} className="py-6 text-center text-sm text-muted-foreground">
                        Nenhuma previsão registrada ainda.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
              {pageCount > 1 && (
                <div className="mt-4 flex items-center justify-between gap-3">
                  <span className="text-xs text-muted-foreground">
                    Página {safePage + 1} de {pageCount}
                  </span>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                      disabled={safePage === 0}
                    >
                      <ChevronLeft className="h-4 w-4" />
                      Anterior
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
                      disabled={safePage >= pageCount - 1}
                    >
                      Próxima
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
