import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, Wallet } from "lucide-react";
import { Card } from "@/components/ui/card";
import { KpiCards } from "@/components/KpiCards";
import { ConfidenceBreakdown } from "@/components/ConfidenceBreakdown";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { wallets, type ModelResults } from "@/lib/api";
import { cn, pct } from "@/lib/utils";

const signed = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
const pnlCls = (v: number) => (v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");

function ModelView({ data }: { data: ModelResults }) {
  if (!data.by_category.length)
    return <Card className="p-6 text-center text-sm text-muted-foreground">Modelo ainda sem apostas liquidadas.</Card>;
  return (
    <div className="space-y-2.5">
      <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">Modelo · por categoria</h2>
      {data.by_category.map((c) => (
        <Card key={c.category} className="flex items-center justify-between gap-4 px-4 py-3">
          <div className="font-bold">{c.category}</div>
          <div className="grid w-[62%] grid-cols-4 gap-2 text-sm tabular-nums">
            <div><div className="text-[10px] uppercase text-muted-foreground">Win</div><div className="font-bold">{pct(c.win_rate)}</div></div>
            <div><div className="text-[10px] uppercase text-muted-foreground">Apostas</div><div className="font-bold">{c.n_bets}</div></div>
            <div><div className="text-[10px] uppercase text-muted-foreground">P&L</div><div className={cn("font-bold", pnlCls(c.total_pnl))}>{signed(c.total_pnl)}</div></div>
            <div><div className="text-[10px] uppercase text-muted-foreground">ROI</div><div className={cn("font-bold", pnlCls(c.roi ?? 0))}>{pct(c.roi)}</div></div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function WalletView({ id }: { id: number }) {
  const { data: rec } = useQuery({ queryKey: ["wallet", id], queryFn: () => wallets.get(id) });
  if (!rec || "error" in rec) return <div className="skeleton h-60" />;
  const a = rec.analysis;
  return (
    <div className="space-y-6">
      <KpiCards overall={a.overall} nMarkets={a.n_markets} nTrades={a.n_trades} />
      {a.by_confidence && a.by_confidence.length > 0 && <ConfidenceBreakdown buckets={a.by_confidence} />}
      <CategoryBreakdown categories={a.by_category} />
    </div>
  );
}

export function SeparatedResultsTab() {
  const { data: list = [] } = useQuery({ queryKey: ["wallets"], queryFn: () => wallets.list() });
  const { data: model } = useQuery({ queryKey: ["model-results"], queryFn: () => wallets.modelResults() });
  const [entity, setEntity] = useState<"model" | number>("model");

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <button onClick={() => setEntity("model")}
          className={cn("flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-sm font-semibold transition",
            entity === "model" ? "border-violet-500 bg-violet-500/10" : "border-border bg-card hover:bg-muted")}>
          <Cpu className="h-4 w-4" /> Modelo
        </button>
        {list.map((w) => (
          <button key={w.id} onClick={() => setEntity(w.id)}
            className={cn("flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-sm font-semibold transition",
              entity === w.id ? "border-sky-500 bg-sky-500/10" : "border-border bg-card hover:bg-muted")}>
            <Wallet className="h-4 w-4" /> {w.name}
          </button>
        ))}
      </div>

      {entity === "model" ? (model ? <ModelView data={model} /> : <div className="skeleton h-40" />) : <WalletView id={entity} />}
    </div>
  );
}
