import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { pct, usd, signedUsd } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { Category, Metrics, SubCategory } from "@/lib/api";

const pnlClass = (v: number) =>
  v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground";

// A compact 4-metric strip reused for category headers and subcategory rows.
function MetricStrip({ m, dense = false }: { m: Metrics; dense?: boolean }) {
  return (
    <div className={cn("grid grid-cols-4 gap-2 tabular-nums", dense ? "text-xs" : "text-sm")}>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Win</div>
        <div className="font-bold">{pct(m.win_rate)}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Apostas</div>
        <div className="font-bold">{m.markets}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">P&L</div>
        <div className={cn("font-bold", pnlClass(m.total_pnl))}>{signedUsd(m.total_pnl)}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">ROI</div>
        <div className={cn("font-bold", pnlClass(m.roi ?? 0))}>{pct(m.roi)}</div>
      </div>
    </div>
  );
}

function SubRow({ s }: { s: SubCategory }) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-lg bg-muted/40 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold">{s.subcategory}</div>
        <div className="text-[11px] text-muted-foreground">
          {s.resolved} resolvidos · {s.wins}V/{s.losses}D · investido {usd(s.invested)}
        </div>
      </div>
      <div className="w-[52%] shrink-0">
        <MetricStrip m={s} dense />
      </div>
    </div>
  );
}

function CategoryItem({ c }: { c: Category }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/40"
      >
        <ChevronRight
          className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate font-bold">{c.category}</div>
          <div className="text-[11px] text-muted-foreground">
            {c.subcategories.length} sub-categoria(s)
          </div>
        </div>
        <div className="w-[58%] shrink-0">
          <MetricStrip m={c} />
        </div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden border-t border-border"
          >
            <div className="space-y-3 p-3">
              {c.by_confidence && c.by_confidence.length > 0 && (
                <div>
                  <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                    Por confiança
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {c.by_confidence.map((b) => (
                      <span
                        key={b.confidence}
                        className="rounded-lg bg-muted/60 px-2 py-1 text-[11px] tabular-nums"
                      >
                        <strong>{b.confidence}</strong>: {pct(b.win_rate)} win · {b.markets}{" "}
                        ap. ·{" "}
                        <span className={pnlClass(b.total_pnl)}>{signedUsd(b.total_pnl)}</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <div>
                <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                  Por sub-categoria
                </div>
                <div className="space-y-1.5">
                  {c.subcategories.map((s) => (
                    <SubRow key={s.subcategory} s={s} />
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}

export function CategoryBreakdown({ categories }: { categories: Category[] }) {
  return (
    <div className="space-y-2.5">
      <h2 className="text-sm font-bold uppercase tracking-wider text-muted-foreground">
        Por categoria · clique para abrir as sub-categorias
      </h2>
      {categories.map((c) => (
        <CategoryItem key={c.category} c={c} />
      ))}
    </div>
  );
}
