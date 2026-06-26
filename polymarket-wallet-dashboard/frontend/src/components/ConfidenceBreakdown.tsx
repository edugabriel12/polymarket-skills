import { Card } from "@/components/ui/card";
import { ShieldCheck } from "lucide-react";
import { pct, signedUsd, usd } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { ConfidenceBucket } from "@/lib/api";

const TONE: Record<string, string> = {
  Alta: "from-emerald-500 to-teal-600",
  Média: "from-amber-500 to-orange-600",
  Baixa: "from-slate-500 to-slate-600",
};

const pnlClass = (v: number) =>
  v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground";

function Cell({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("font-bold tabular-nums", cls)}>{value}</div>
    </div>
  );
}

export function ConfidenceBreakdown({ buckets }: { buckets: ConfidenceBucket[] }) {
  return (
    <div className="space-y-2.5">
      <h2 className="flex items-center gap-1.5 text-sm font-bold uppercase tracking-wider text-muted-foreground">
        <ShieldCheck className="h-4 w-4" /> Por nível de confiança
      </h2>
      <div className="grid gap-3 sm:grid-cols-3">
        {buckets.map((b) => (
          <Card key={b.confidence} className="overflow-hidden">
            <div className={`bg-gradient-to-r ${TONE[b.confidence] ?? "from-sky-500 to-violet-500"} px-4 py-2 text-sm font-black text-white`}>
              {b.confidence}
            </div>
            <div className="grid grid-cols-2 gap-3 p-4">
              <Cell label="Win rate" value={pct(b.win_rate)} />
              <Cell label="Apostas" value={String(b.markets)} />
              <Cell label="P&L" value={signedUsd(b.total_pnl)} cls={pnlClass(b.total_pnl)} />
              <Cell label="ROI" value={pct(b.roi)} cls={pnlClass(b.roi ?? 0)} />
              <div className="col-span-2 text-[11px] text-muted-foreground">
                {b.wins}V/{b.losses}D · investido {usd(b.invested)}
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
