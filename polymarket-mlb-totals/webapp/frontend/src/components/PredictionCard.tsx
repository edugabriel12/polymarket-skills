import { useState } from "react";
import { motion } from "framer-motion";
import { ChevronDown, ExternalLink, TrendingUp, Activity } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/StatusBadge";
import type { Suggestion } from "@/lib/api";
import { cn, pct } from "@/lib/utils";

function prettyMatchup(slug: string) {
  const m = slug.match(/^mlb-([a-z]+)-([a-z]+)-/i);
  return m ? `${m[1].toUpperCase()} @ ${m[2].toUpperCase()}` : slug;
}

export function PredictionCard({ s }: { s: Suggestion }) {
  const [open, setOpen] = useState(false);
  const isOver = s.recommendation.side === "YES" && (s.stats?.chosen_side ?? "").toUpperCase() === "OVER";
  const sideLabel = (s.stats?.chosen_side ?? "").toUpperCase() || "—";
  const odds = s.stats?.decimal_odds ?? 1 / s.recommendation.price;
  const edge = s.stats?.edge_after_fee ?? 0;
  const pOver = s.stats?.p_over_eff ?? 0.5;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}>
      <Card className="overflow-hidden">
        <div className={cn("h-1.5 w-full bg-gradient-to-r", isOver ? "from-sky-500 to-cyan-400" : "from-violet-500 to-fuchsia-400")} />
        <div className="p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-black tracking-tight">{prettyMatchup(s.game)}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">Linha {s.line} · μ {s.mu.toFixed(2)}</div>
            </div>
            <div className="flex flex-col items-end gap-1.5">
              <Badge tone={sideLabel === "OVER" ? "over" : "under"}>{sideLabel} {s.line}</Badge>
              {s.status && <StatusBadge status={s.status} />}
            </div>
          </div>

          <div className="mt-4 grid grid-cols-3 gap-3 text-center">
            <Metric label="Payout" value={`${odds.toFixed(2)}x`} accent="text-foreground" />
            <Metric label="Edge" value={pct(edge)} accent={edge >= 0 ? "text-emerald-400" : "text-rose-400"} />
            <Metric label="Tamanho" value={pct(s.recommendation.size_pct, 2)} accent="text-foreground" />
          </div>

          {/* P(Over) vs P(Under) bar */}
          <div className="mt-4">
            <div className="mb-1 flex justify-between text-[11px] font-semibold">
              <span className="text-sky-400">Over {pct(pOver)}</span>
              <span className="text-violet-400">Under {pct(1 - pOver)}</span>
            </div>
            <div className="flex h-2.5 overflow-hidden rounded-full">
              <div className="bg-sky-500" style={{ width: `${pOver * 100}%` }} />
              <div className="bg-violet-500" style={{ width: `${(1 - pOver) * 100}%` }} />
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between">
            <button
              onClick={() => setOpen((o) => !o)}
              className="inline-flex items-center gap-1 text-xs font-semibold text-muted-foreground hover:text-foreground"
            >
              <Activity className="h-3.5 w-3.5" /> Detalhes do modelo
              <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
            </button>
            {s.market_url && (
              <a
                href={s.market_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs font-semibold text-sky-400 hover:underline"
              >
                Ver mercado <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
          </div>

          {open && s.stats && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              className="mt-3 overflow-hidden rounded-xl border border-border bg-muted/40 p-3"
            >
              <div className="mb-2 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                <TrendingUp className="h-3.5 w-3.5" /> Log estatístico (NegBin)
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <Row k="μ (média total)" v={s.stats.mu?.toFixed(3)} />
                <Row k="Variância" v={s.stats.variance?.toFixed(3)} />
                <Row k="Dispersão" v={s.stats.dispersion?.toString()} />
                <Row k="NegBin r / p" v={`${s.stats.negbin_r?.toFixed(2)} / ${s.stats.negbin_p?.toFixed(2)}`} />
                <Row k="Park factor" v={s.stats.park_factor?.toString()} />
                <Row k="Baseline liga" v={s.stats.league_baseline?.toString()} />
                <Row k="P(Over) / P(Under)" v={`${pct(s.stats.p_over_eff)} / ${pct(s.stats.p_under_eff)}`} />
                <Row k="P(push)" v={pct(s.stats.p_push)} />
                <Row k="Inputs externos" v={s.stats.used_external ? "sim" : "não (mkt-implied)"} />
                <Row k="Edge após fee" v={pct(s.stats.edge_after_fee)} />
              </div>
              {s.stats.inputs && Object.keys(s.stats.inputs).length > 0 && (
                <div className="mt-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
                  <span className="font-semibold">Fatores:</span>{" "}
                  {Object.entries(s.stats.inputs)
                    .filter(([, v]) => v !== null && v !== undefined)
                    .map(([k, v]) => `${k}=${v}`)
                    .join(" · ")}
                </div>
              )}
            </motion.div>
          )}
        </div>
      </Card>
    </motion.div>
  );
}

function Metric({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div className="rounded-xl bg-muted/50 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("text-sm font-black tabular-nums", accent)}>{value}</div>
    </div>
  );
}

function Row({ k, v }: { k: string; v?: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-semibold tabular-nums">{v ?? "—"}</span>
    </div>
  );
}
