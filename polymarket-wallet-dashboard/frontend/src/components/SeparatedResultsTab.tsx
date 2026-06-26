import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, Wallet, ChevronRight } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { Card } from "@/components/ui/card";
import { KpiCards } from "@/components/KpiCards";
import { ConfidenceBreakdown } from "@/components/ConfidenceBreakdown";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { DashBetList } from "@/components/DashBetList";
import { wallets, type ModelResults, type ModelCategory } from "@/lib/api";
import { cn, pct } from "@/lib/utils";

const signed = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
const pnlCls = (v: number) => (v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");

function ModelCategoryRow({ c }: { c: ModelCategory }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="overflow-hidden">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/40">
        <ChevronRight className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1 truncate font-bold">{c.category}</div>
        <div className="grid w-[58%] grid-cols-4 gap-2 text-sm tabular-nums">
          <div><div className="text-[10px] uppercase text-muted-foreground">Win</div><div className="font-bold">{pct(c.win_rate)}</div></div>
          <div><div className="text-[10px] uppercase text-muted-foreground">Apostas</div><div className="font-bold">{c.n_bets}</div></div>
          <div><div className="text-[10px] uppercase text-muted-foreground">P&L</div><div className={cn("font-bold", pnlCls(c.total_pnl))}>{signed(c.total_pnl)}</div></div>
          <div><div className="text-[10px] uppercase text-muted-foreground">ROI</div><div className={cn("font-bold", pnlCls(c.roi ?? 0))}>{pct(c.roi)}</div></div>
        </div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden border-t border-border">
            <div className="p-3">
              <DashBetList fetchKey={["model-bets", c.category]} fetcher={(p) => wallets.modelBets(c.category, p)} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}

function ModelView({ data }: { data: ModelResults }) {
  if (!data.by_category.length)
    return <Card className="p-6 text-center text-sm text-muted-foreground">Modelo ainda sem apostas liquidadas.</Card>;
  return (
    <div className="space-y-2.5">
      <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">Modelo · por categoria · clique para ver as apostas</h2>
      {data.by_category.map((c) => <ModelCategoryRow key={c.category} c={c} />)}
    </div>
  );
}

function WalletView({ id }: { id: number }) {
  const { data: rec } = useQuery({ queryKey: ["wallet", id], queryFn: () => wallets.get(id) });
  if (!rec || "error" in rec) return <div className="skeleton h-60" />;
  const a = rec.analysis;
  if (a.n_markets === 0) {
    return (
      <Card className="p-8 text-center text-sm text-muted-foreground">
        Ainda sem apostas liquidadas desta carteira (desde que foi adicionada).
        {!!a.live_open && <div className="mt-1 text-xs">{a.live_open} aposta(s) em aberto.</div>}
      </Card>
    );
  }
  return (
    <div className="space-y-6">
      <div className="rounded-xl bg-sky-500/10 px-3 py-1.5 text-xs font-semibold text-sky-400">
        Apostas ao vivo desde a adição · {a.live_settled} liquidada(s)
        {!!a.live_open && ` · ${a.live_open} em aberto`}
      </div>
      <KpiCards overall={a.overall} nMarkets={a.n_markets} nTrades={a.n_trades} />
      {a.by_confidence && a.by_confidence.length > 0 && <ConfidenceBreakdown buckets={a.by_confidence} />}
      <CategoryBreakdown
        categories={a.by_category}
        renderBets={(cat) => (
          <DashBetList fetchKey={["wallet-bets", id, cat]} fetcher={(p) => wallets.bets(id, cat, p)} />
        )}
      />
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
