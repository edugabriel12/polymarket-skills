import { useState } from "react";
import { ChevronRight, Check } from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { FilterTree, WalletFilters } from "@/lib/api";

// Per-wallet forwarding filter picker: category -> subcategory -> confidence chips.
// ONLY the selected (category, subcategory, confidence) combos are forwarded to
// Sports/Telegram. Fully controlled (value + onChange); the parent owns the selection.

const clone = (v: WalletFilters): WalletFilters => JSON.parse(JSON.stringify(v));

const countCombos = (f: WalletFilters | FilterTree): number =>
  Object.values(f).reduce(
    (n, subs) => n + Object.values(subs).reduce((m, cs) => m + cs.length, 0), 0);

/** Pre-select every (category, subcategory, confidence) combo in a tree. */
export function fullSelection(tree: FilterTree): WalletFilters {
  const out: WalletFilters = {};
  for (const cat of Object.keys(tree)) {
    out[cat] = {};
    for (const sub of Object.keys(tree[cat])) out[cat][sub] = [...tree[cat][sub]];
  }
  return out;
}

function withConf(value: WalletFilters, tree: FilterTree, cat: string, sub: string,
                  conf: string, on: boolean): WalletFilters {
  const next = clone(value);
  const cur = new Set(next[cat]?.[sub] ?? []);
  if (on) cur.add(conf);
  else cur.delete(conf);
  const ordered = (tree[cat]?.[sub] ?? []).filter((c) => cur.has(c)); // keep canonical order
  if (ordered.length) {
    next[cat] = next[cat] ?? {};
    next[cat][sub] = ordered;
  } else if (next[cat]) {
    delete next[cat][sub];
    if (!Object.keys(next[cat]).length) delete next[cat];
  }
  return next;
}

function withSub(value: WalletFilters, tree: FilterTree, cat: string, sub: string,
                 on: boolean): WalletFilters {
  const next = clone(value);
  if (on) {
    next[cat] = next[cat] ?? {};
    next[cat][sub] = [...(tree[cat]?.[sub] ?? [])];
  } else if (next[cat]) {
    delete next[cat][sub];
    if (!Object.keys(next[cat]).length) delete next[cat];
  }
  return next;
}

function withCat(value: WalletFilters, tree: FilterTree, cat: string, on: boolean): WalletFilters {
  const next = clone(value);
  if (on) {
    next[cat] = {};
    for (const sub of Object.keys(tree[cat] ?? {})) next[cat][sub] = [...tree[cat][sub]];
  } else {
    delete next[cat];
  }
  return next;
}

function Chip({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold ring-1 transition",
        active
          ? "bg-sky-500/20 text-sky-300 ring-sky-500/40"
          : "bg-muted text-muted-foreground ring-border hover:bg-muted/70"
      )}
    >
      {active && <Check className="h-3 w-3" />}
      {children}
    </button>
  );
}

function CatRow({ cat, tree, value, onChange }: {
  cat: string; tree: FilterTree; value: WalletFilters;
  onChange: (next: WalletFilters) => void;
}) {
  const [open, setOpen] = useState(true);
  const subs = tree[cat] ?? {};
  const selSubs = value[cat] ?? {};
  const selCount = Object.values(selSubs).reduce((m, cs) => m + cs.length, 0);
  const totalCount = Object.values(subs).reduce((m, cs) => m + cs.length, 0);
  const allOn = totalCount > 0 && selCount === totalCount;

  return (
    <Card className="overflow-hidden">
      <div className="flex w-full items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronRight
            className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
          />
          <span className="truncate font-bold">{cat}</span>
          <span className="text-[11px] tabular-nums text-muted-foreground">{selCount}/{totalCount}</span>
        </button>
        <button
          type="button"
          onClick={() => onChange(withCat(value, tree, cat, !allOn))}
          className="shrink-0 rounded-lg border border-border px-2 py-1 text-[11px] font-semibold hover:bg-muted"
        >
          {allOn ? "Limpar" : "Tudo"}
        </button>
      </div>
      {open && (
        <div className="space-y-2 border-t border-border p-3">
          {Object.keys(subs).map((sub) => {
            const selected = new Set(selSubs[sub] ?? []);
            const subAll = subs[sub].length > 0 && selected.size === subs[sub].length;
            return (
              <div key={sub} className="rounded-lg bg-muted/40 px-3 py-2">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold">{sub}</span>
                  <button
                    type="button"
                    onClick={() => onChange(withSub(value, tree, cat, sub, !subAll))}
                    className="shrink-0 text-[11px] font-semibold text-sky-400 hover:underline"
                  >
                    {subAll ? "limpar" : "todas"}
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {subs[sub].map((conf) => (
                    <Chip
                      key={conf}
                      active={selected.has(conf)}
                      onClick={() => onChange(withConf(value, tree, cat, sub, conf, !selected.has(conf)))}
                    >
                      {conf}
                    </Chip>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function FilterSelector({ tree, value, onChange }: {
  tree: FilterTree; value: WalletFilters; onChange: (next: WalletFilters) => void;
}) {
  const cats = Object.keys(tree);
  if (!cats.length) {
    return <div className="text-sm text-muted-foreground">Nenhuma categoria encontrada no CSV.</div>;
  }
  const sel = countCombos(value);
  const total = countCombos(tree);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-muted-foreground">
          Só o que estiver marcado é enviado pro Sports/Telegram — {sel} de {total} combinações
          (categoria · sub-categoria · confiança).
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => onChange(fullSelection(tree))}
            className="rounded-lg border border-border px-2.5 py-1 text-xs font-semibold hover:bg-muted"
          >
            Selecionar tudo
          </button>
          <button
            type="button"
            onClick={() => onChange({})}
            className="rounded-lg border border-border px-2.5 py-1 text-xs font-semibold hover:bg-muted"
          >
            Limpar
          </button>
        </div>
      </div>
      <div className="space-y-2">
        {cats.map((cat) => (
          <CatRow key={cat} cat={cat} tree={tree} value={value} onChange={onChange} />
        ))}
      </div>
    </div>
  );
}
