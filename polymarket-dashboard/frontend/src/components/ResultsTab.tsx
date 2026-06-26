import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronRight, Trophy, Layers, TrendingUp, Percent } from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn, pct } from "@/lib/utils";
import { api, type CategoryResult, type UnitMetrics } from "@/lib/api";

const signedU = (v: number) => `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(2)}U`;
const pnlCls = (v: number) => (v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");

function Kpi({ icon: Icon, label, value, sub, gradient }: {
  icon: typeof Trophy; label: string; value: string; sub?: string; gradient: string;
}) {
  return (
    <Card className="overflow-hidden">
      <div className={`bg-gradient-to-br ${gradient} px-4 py-3 text-white`}>
        <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider opacity-90">
          <Icon className="h-4 w-4" /> {label}
        </div>
        <div className="mt-1 text-2xl font-black tabular-nums">{value}</div>
      </div>
      {sub ? <div className="px-4 py-2 text-xs text-muted-foreground">{sub}</div> : null}
    </Card>
  );
}

function UnitStrip({ m }: { m: UnitMetrics }) {
  return (
    <div className="grid grid-cols-4 gap-2 text-sm tabular-nums">
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Win</div>
        <div className="font-bold">{pct(m.win_rate)}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Apostas</div>
        <div className="font-bold">{m.n_bets}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">P&L</div>
        <div className={cn("font-bold", pnlCls(m.pnl_u))}>{signedU(m.pnl_u)}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">ROI</div>
        <div className={cn("font-bold", pnlCls(m.roi ?? 0))}>{pct(m.roi)}</div>
      </div>
    </div>
  );
}

function CategoryRow({ c }: { c: CategoryResult }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="overflow-hidden">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/40">
        <ChevronRight className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1 truncate font-bold">{c.category}</div>
        <div className="w-[60%] shrink-0"><UnitStrip m={c} /></div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden border-t border-border">
            <div className="space-y-1.5 p-3">
              {c.by_unit.map((u) => (
                <div key={u.unit_label} className="flex items-center justify-between gap-4 rounded-lg bg-muted/40 px-3 py-2">
                  <div className="text-sm font-bold">{u.unit_label}</div>
                  <div className="w-[70%]"><UnitStrip m={u} /></div>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}

export function ResultsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["results"],
    queryFn: () => api.results(),
    refetchInterval: 30 * 1000,
    refetchOnWindowFocus: false,
  });

  if (isLoading) return <div className="skeleton h-72" />;
  const ov = data?.overall;
  if (!ov || data.n_bets === 0) {
    return <Card className="p-10 text-center text-sm text-muted-foreground">Sem apostas liquidadas ainda.</Card>;
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi icon={Trophy} label="Win rate" value={pct(ov.win_rate)} sub={`${ov.wins}V / ${ov.losses}D`} gradient="from-emerald-500 to-teal-600" />
        <Kpi icon={Layers} label="Apostas" value={String(ov.n_bets)} sub={`${ov.staked_u.toFixed(2)}U apostadas`} gradient="from-sky-500 to-cyan-600" />
        <Kpi icon={TrendingUp} label="P&L" value={signedU(ov.pnl_u)} gradient={ov.pnl_u >= 0 ? "from-emerald-500 to-green-600" : "from-rose-500 to-orange-600"} />
        <Kpi icon={Percent} label="ROI" value={pct(ov.roi)} gradient={(ov.roi ?? 0) >= 0 ? "from-violet-500 to-fuchsia-600" : "from-rose-500 to-orange-600"} />
      </div>

      <div className="space-y-2.5">
        <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">Por unidade sugerida</h2>
        <div className="grid gap-3 sm:grid-cols-3">
          {data.by_unit.map((u) => (
            <Card key={u.unit_label} className="p-4">
              <div className="mb-2 text-sm font-black">{u.unit_label}</div>
              <UnitStrip m={u} />
            </Card>
          ))}
        </div>
      </div>

      <div className="space-y-2.5">
        <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">Por categoria · clique para abrir as unidades</h2>
        {data.by_category.map((c) => (
          <CategoryRow key={c.category} c={c} />
        ))}
      </div>
    </div>
  );
}
