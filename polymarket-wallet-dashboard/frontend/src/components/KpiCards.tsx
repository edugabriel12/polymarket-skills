import { Card } from "@/components/ui/card";
import { Trophy, Layers, DollarSign, Percent } from "lucide-react";
import { pct, usd, signedUsd } from "@/lib/utils";
import type { Metrics } from "@/lib/api";

function Kpi({
  icon: Icon,
  label,
  value,
  sub,
  gradient,
}: {
  icon: typeof Trophy;
  label: string;
  value: string;
  sub?: string;
  gradient: string;
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

export function KpiCards({
  overall,
  nMarkets,
  nTrades,
}: {
  overall: Metrics;
  nMarkets: number;
  nTrades: number;
}) {
  const pnl = overall.total_pnl;
  const roi = overall.roi ?? 0;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Kpi
        icon={Trophy}
        label="Win rate"
        value={pct(overall.win_rate)}
        sub={`${overall.wins}/${overall.resolved} resolvidos`}
        gradient="from-emerald-500 to-teal-600"
      />
      <Kpi
        icon={Layers}
        label="Apostas"
        value={String(nMarkets)}
        sub={`${nTrades} trades`}
        gradient="from-sky-500 to-cyan-600"
      />
      <Kpi
        icon={DollarSign}
        label="P&L"
        value={signedUsd(pnl)}
        sub={`investido ${usd(overall.invested)}`}
        gradient={pnl >= 0 ? "from-emerald-500 to-green-600" : "from-rose-500 to-orange-600"}
      />
      <Kpi
        icon={Percent}
        label="ROI"
        value={pct(overall.roi)}
        sub={`valor atual ${usd(overall.current_value)}`}
        gradient={roi >= 0 ? "from-violet-500 to-fuchsia-600" : "from-rose-500 to-orange-600"}
      />
    </div>
  );
}
