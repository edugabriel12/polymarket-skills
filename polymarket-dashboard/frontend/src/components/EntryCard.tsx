import { motion } from "framer-motion";
import { ExternalLink, Radio, Clock } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { unitLabel, type Entry } from "@/lib/api";

function Metric({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div className="rounded-xl bg-muted/50 px-2 py-2 text-center">
      <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-sm font-black tabular-nums", accent)}>{value}</div>
    </div>
  );
}

export function EntryCard({ e }: { e: Entry }) {
  const isLive = e.live === "LIVE";
  const isPositive = e.side === "OVER" || e.side === "YES";
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
      <Card className="overflow-hidden">
        <div className={cn("h-1.5 w-full bg-gradient-to-r", isPositive ? "from-sky-500 to-cyan-400" : "from-violet-500 to-fuchsia-400")} />
        <div className="p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-black tracking-tight">{e.event}</div>
              <div className="mt-0.5 truncate text-xs text-muted-foreground">
                {e.category} · {e.subcategory}
              </div>
            </div>
            <div className="flex flex-col items-end gap-1.5">
              <Badge tone={isPositive ? "over" : "under"}>{e.side}</Badge>
              <span
                className={cn(
                  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-bold ring-1 ring-inset",
                  isLive ? "bg-rose-500/15 text-rose-400 ring-rose-500/30"
                         : "bg-amber-500/15 text-amber-500 ring-amber-500/30"
                )}
              >
                {isLive ? <Radio className="h-3 w-3" /> : <Clock className="h-3 w-3" />}
                {e.live}
              </span>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2">
            <Metric label="Payout" value={`${(e.odds || 0).toFixed(2)}x`} />
            <Metric label="Unidade" value={unitLabel(e.unit)} accent="text-emerald-400" />
            <Metric label="Entrada" value={(e.entry_price || 0).toFixed(2)} />
          </div>

          {e.market_url && (
            <a
              href={e.market_url}
              target="_blank"
              rel="noreferrer"
              className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-sky-400 hover:underline"
            >
              ver mercado <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      </Card>
    </motion.div>
  );
}
