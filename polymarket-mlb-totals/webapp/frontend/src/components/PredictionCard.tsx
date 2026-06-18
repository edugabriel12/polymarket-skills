import { useState } from "react";
import { motion } from "framer-motion";
import { ChevronDown, ExternalLink, TrendingUp, Activity } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/StatusBadge";
import type { Suggestion } from "@/lib/api";
import { cn, gameEventUrl, pct } from "@/lib/utils";

function prettyMatchup(slug: string) {
  const m = slug.match(/^[a-z0-9]+-([a-z0-9]+)-([a-z0-9]+)-/i);
  return m ? `${m[1].toUpperCase()} vs ${m[2].toUpperCase()}` : slug;
}

export function PredictionCard({ s }: { s: Suggestion }) {
  const [open, setOpen] = useState(false);
  const st = s.stats ?? {};
  const market = (st.market ?? s.market ?? "TOTAL").toUpperCase();
  const side = (st.chosen_side ?? s.side ?? "").toUpperCase();
  const line = s.line ?? st.line ?? null;
  const price = s.recommendation.price;
  const odds = st.decimal_odds ?? (price > 0 ? 1 / price : 0);
  const edge = s.edge ?? st.edge_after_fee ?? 0;
  const pChosen = st.model_prob ?? st.p_over_eff ?? 0.5;

  const isPositive = side === "OVER" || side === "YES";
  const sides = market === "BTTS" ? ["YES", "NO"] : ["OVER", "UNDER"];
  const otherSide = sides.find((x) => x !== side) ?? sides[1];

  const marketLabel =
    market === "BTTS" ? "Ambos Marcam (BTTS)" : `Total ${line ?? ""} O/U`;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}>
      <Card className="overflow-hidden">
        <div className={cn("h-1.5 w-full bg-gradient-to-r", isPositive ? "from-sky-500 to-cyan-400" : "from-violet-500 to-fuchsia-400")} />
        <div className="p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-black tracking-tight">{prettyMatchup(s.game)}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">{marketLabel}</div>
            </div>
            <div className="flex flex-col items-end gap-1.5">
              <Badge tone={isPositive ? "over" : "under"}>{side}{line && market !== "BTTS" ? ` ${line}` : ""}</Badge>
              {s.status && <StatusBadge status={s.status} />}
            </div>
          </div>

          <div className="mt-4 grid grid-cols-3 gap-3 text-center">
            <Metric label="Payout" value={`${odds.toFixed(2)}x`} accent="text-foreground" />
            <Metric label="Edge" value={pct(edge)} accent={edge >= 0 ? "text-emerald-400" : "text-rose-400"} />
            <Metric label="Tamanho" value={pct(s.recommendation.size_pct, 2)} accent="text-foreground" />
          </div>

          {/* Chosen-side probability bar */}
          <div className="mt-4">
            <div className="mb-1 flex justify-between text-[11px] font-semibold">
              <span className="text-sky-400">{side} {pct(pChosen)}</span>
              <span className="text-violet-400">{otherSide} {pct(1 - pChosen)}</span>
            </div>
            <div className="flex h-2.5 overflow-hidden rounded-full">
              <div className="bg-sky-500" style={{ width: `${pChosen * 100}%` }} />
              <div className="bg-violet-500" style={{ width: `${(1 - pChosen) * 100}%` }} />
            </div>
          </div>

          <div className="mt-4 flex items-center justify-between">
            <button onClick={() => setOpen((o) => !o)}
              className="inline-flex items-center gap-1 text-xs font-semibold text-muted-foreground hover:text-foreground">
              <Activity className="h-3.5 w-3.5" /> Detalhes do modelo
              <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
            </button>
            {s.market_url && (
              <a href={gameEventUrl(s.market_url)} target="_blank" rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs font-semibold text-sky-400 hover:underline">
                Ver mercado <ExternalLink className="h-3.5 w-3.5" />
              </a>
            )}
          </div>

          {open && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
              className="mt-3 overflow-hidden rounded-xl border border-border bg-muted/40 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                <TrendingUp className="h-3.5 w-3.5" /> Log estatístico ({st.model ?? "modelo"})
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                {st.mu !== undefined && <Row k="μ (média total)" v={st.mu?.toFixed(3)} />}
                {st.lam_home !== undefined && <Row k="λ casa / fora" v={`${st.lam_home?.toFixed(2)} / ${st.lam_away?.toFixed(2)}`} />}
                {st.rho !== undefined && <Row k="ρ (Dixon-Coles)" v={st.rho?.toString()} />}
                {st.variance !== undefined && <Row k="Variância" v={st.variance?.toFixed(3)} />}
                {st.negbin_r !== undefined && <Row k="NegBin r / p" v={`${st.negbin_r?.toFixed(2)} / ${st.negbin_p?.toFixed(2)}`} />}
                {st.park_factor !== undefined && <Row k="Park factor" v={st.park_factor?.toString()} />}
                <Row k="P(modelo)" v={pct(pChosen)} />
                <Row k="Edge após fee" v={pct(edge)} />
                <Row k="Inputs externos" v={st.used_external ? "sim" : "não (mkt-implied)"} />
                <Row k="Mercado" v={market} />
              </div>
              {st.inputs && Object.keys(st.inputs).length > 0 && (
                <div className="mt-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
                  <span className="font-semibold">Fatores:</span>{" "}
                  {Object.entries(st.inputs)
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
