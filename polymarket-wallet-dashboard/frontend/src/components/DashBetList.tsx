import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DashBet, DashBetsPage } from "@/lib/api";

const STATUS: Record<string, { label: string; cls: string }> = {
  WON: { label: "Ganhou", cls: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30" },
  LOST: { label: "Perdeu", cls: "bg-rose-500/15 text-rose-400 ring-rose-500/30" },
  VOID: { label: "Anulado", cls: "bg-slate-500/15 text-slate-400 ring-slate-500/30" },
};
const pnlCls = (v: number) => (v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");
const usd = (v: number) => `${v < 0 ? "-" : "+"}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const unitL = (u?: number) => (u === 1 ? "1U" : u === 0.5 ? "0.5U" : u === 0.25 ? "0.25U" : u ? `${u}U` : "");
const fmtDate = (s?: string) => (s ? new Date(s).toLocaleDateString("pt-BR") : "");

function pnlDisplay(b: DashBet): { text: string; v: number } {
  if (b.pnl !== null && b.pnl !== undefined) return { text: usd(b.pnl), v: b.pnl };
  const u = b.unit ?? 1;                     // model bets: unit P&L
  const v = b.status === "WON" ? u * (b.odds - 1) : b.status === "LOST" ? -u : 0;
  return { text: `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(2)}U`, v };
}

const PAGE_SIZE = 20;

export function DashBetList({ fetchKey, fetcher }: {
  fetchKey: (string | number)[];
  fetcher: (page: number) => Promise<DashBetsPage>;
}) {
  const [page, setPage] = useState(1);
  const { data, isLoading } = useQuery({
    queryKey: [...fetchKey, page],
    queryFn: () => fetcher(page),
  });

  if (isLoading) return <div className="skeleton h-32" />;
  const bets = data?.bets ?? [];
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (total === 0) return <div className="px-1 py-2 text-xs text-muted-foreground">Sem apostas liquidadas.</div>;

  return (
    <div className="space-y-2">
      <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
        Apostas liquidadas ({total})
      </div>
      <div className="space-y-1.5">
        {bets.map((b, i) => {
          const s = STATUS[b.status] ?? STATUS.VOID;
          const p = pnlDisplay(b);
          return (
            <div key={b.key ?? i} className="rounded-lg bg-muted/40 px-3 py-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-bold">{b.event}</div>
                  <div className="truncate text-[11px] text-muted-foreground">
                    {b.subcategory} · {b.side}
                    {b.confidence ? ` · ${b.confidence}` : b.unit ? ` · ${unitL(b.unit)}` : ""}
                    {" · "}odds {(b.odds || 0).toFixed(2)}
                  </div>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-bold ring-1 ring-inset", s.cls)}>{s.label}</span>
                  <span className={cn("text-sm font-black tabular-nums", pnlCls(p.v))}>{p.text}</span>
                </div>
              </div>
              <div className="mt-1 flex items-center gap-3 text-[10px] text-muted-foreground">
                {b.total_position ? <span>${Math.round(b.total_position).toLocaleString("en-US")} apostado</span> : null}
                <span>{fmtDate(b.updated_at)}</span>
                {b.market_url && (
                  <a href={b.market_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sky-400 hover:underline">
                    mercado <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {pages > 1 && (
        <div className="flex items-center justify-between pt-1 text-xs">
          <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 disabled:opacity-40">
            <ChevronLeft className="h-3.5 w-3.5" /> Anterior
          </button>
          <span className="text-muted-foreground">Página {page} de {pages}</span>
          <button disabled={page >= pages} onClick={() => setPage((p) => p + 1)}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 disabled:opacity-40">
            Próxima <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
