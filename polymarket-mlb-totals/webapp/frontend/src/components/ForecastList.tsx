import { useState } from "react";
import { ListTree, ChevronDown } from "lucide-react";
import { Card } from "@/components/ui/card";
import type { GameForecast } from "@/lib/api";
import { cn } from "@/lib/utils";

function prettyMatchup(slug: string) {
  const m = slug.replace(/^[a-z0-9]+-/, "").match(/^([a-z0-9]+)-([a-z0-9]+)-/i);
  return m ? `${m[1].toUpperCase()} @ ${m[2].toUpperCase()}` : slug.replace(/^mlb-/, "");
}

const BASIS_LABEL: Record<string, string> = {
  sharp: "sharp", factors: "fatores", market: "mercado",
};

// A calibrated prediction for EVERY game — including those without a tradeable market/edge.
export function ForecastList({ forecasts }: { forecasts: GameForecast[] }) {
  const [open, setOpen] = useState(true);
  const modeled = forecasts.filter((f) => f.forecast);
  if (modeled.length === 0) return null;

  return (
    <Card className="p-4">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between text-xs font-bold uppercase tracking-wider text-muted-foreground"
      >
        <span className="inline-flex items-center gap-1.5">
          <ListTree className="h-3.5 w-3.5" /> Previsão de todos os jogos ({modeled.length})
        </span>
        <ChevronDown className={cn("h-4 w-4 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {modeled.map((f) => {
            const fc = f.forecast!;
            const overPct = Math.round(fc.p_over * 100);
            return (
              <div key={f.game} className="rounded-xl border border-border bg-muted/30 px-3 py-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-black tracking-tight">{prettyMatchup(f.game)}</span>
                  <span className="flex items-center gap-1.5 text-[10px]">
                    {!f.has_market && (
                      <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 font-semibold text-amber-500">
                        sem mercado
                      </span>
                    )}
                    {f.basis && (
                      <span className="rounded-full bg-muted px-1.5 py-0.5 font-semibold text-muted-foreground">
                        {BASIS_LABEL[f.basis] ?? f.basis}
                      </span>
                    )}
                  </span>
                </div>
                <div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>
                    ~<span className="font-bold tabular-nums text-foreground">{fc.mean_total}</span> runs
                    {" · "}80% <span className="font-semibold tabular-nums text-foreground">{fc.pi80[0]}–{fc.pi80[1]}</span>
                  </span>
                  <span>
                    O/U {f.line}:{" "}
                    <span className="font-semibold tabular-nums text-sky-400">{overPct}%</span>
                    {" / "}
                    <span className="font-semibold tabular-nums text-violet-400">{100 - overPct}%</span>
                  </span>
                </div>
                <div className="mt-0.5 text-[10px] text-muted-foreground">
                  incerteza {fc.entropy_bits.toFixed(2)} bits
                  {f.edge_vs_market != null && (
                    <> · edge vs mercado{" "}
                      <span className={cn("font-semibold tabular-nums",
                        f.edge_vs_market >= 0 ? "text-emerald-400" : "text-rose-400")}>
                        {(f.edge_vs_market * 100 >= 0 ? "+" : "") + (f.edge_vs_market * 100).toFixed(1)}%
                      </span>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
