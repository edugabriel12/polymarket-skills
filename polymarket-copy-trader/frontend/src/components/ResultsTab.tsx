import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { KpiCards, type Kpi } from "@/components/KpiCards";
import { EntryList } from "@/components/EntryList";
import { api, type WalletStats } from "@/lib/api";
import { pct, usd, signedUsd, shortAddr, cn } from "@/lib/utils";

export function ResultsTab() {
  const { data } = useQuery({ queryKey: ["results"], queryFn: api.results, refetchInterval: 30000 });
  const [open, setOpen] = useState<number | null>(null);

  const p = data?.portfolio;
  const kpis: Kpi[] = [
    { label: "P&L total", value: signedUsd(p?.total_pnl ?? 0), tone: (p?.total_pnl ?? 0) >= 0 ? "emerald" : "rose" },
    { label: "Valor total", value: usd(p?.total_value), sub: `de ${usd(p?.starting_balance)}`, tone: "sky" },
    { label: "Realizado", value: signedUsd(p?.realized_pnl ?? 0), tone: "violet" },
    { label: "Não realizado", value: signedUsd(p?.unrealized_pnl ?? 0), tone: "amber" },
    { label: "Caixa", value: usd(p?.cash_balance), tone: "slate" },
    { label: "Posições", value: String(p?.num_open_positions ?? 0), tone: "slate" },
  ];

  const wallets = data?.wallets ?? [];

  return (
    <div className="space-y-5">
      <KpiCards items={kpis} />

      <div className="space-y-3">
        {wallets.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              Sem resultados ainda.
            </CardContent>
          </Card>
        )}
        {wallets.map((w) => (
          <WalletResultCard
            key={w.wallet_id}
            w={w}
            open={open === w.wallet_id}
            onToggle={() => setOpen(open === w.wallet_id ? null : w.wallet_id)}
          />
        ))}
      </div>
    </div>
  );
}

function WalletResultCard({ w, open, onToggle }: { w: WalletStats; open: boolean; onToggle: () => void }) {
  return (
    <Card>
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left"
      >
        <div className="flex items-center gap-2">
          {open ? <ChevronDown size={18} /> : <ChevronRight size={18} />}
          <span className="font-bold">{w.name}</span>
          <span className="font-mono text-xs text-muted-foreground">{shortAddr(w.address)}</span>
          {w.active === 0 && <Badge tone="voidc">pausada</Badge>}
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
          <M label="P&L" value={signedUsd(w.total_pnl)} tone={w.total_pnl >= 0 ? "text-emerald-400" : "text-rose-400"} />
          <M label="ROI" value={pct(w.roi)} />
          <M label="Win rate" value={pct(w.win_rate)} sub={`${w.n_wins}/${w.n_wins + w.n_losses}`} />
          <M label="Slippage médio" value={pct(w.avg_slippage)} />
          <M label="Executadas" value={pct(w.pct_executed)} sub={`${w.n_executed}/${w.n_entries}`} />
          <M label="Falhas" value={pct(w.pct_failed)} sub={`${w.n_skipped}`} />
        </div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3">
              <EntryList
                queryKey={["wallet-entries", w.wallet_id]}
                fetcher={(page) => api.walletEntries(w.wallet_id, page)}
                showWallet={false}
                emptyText="Esta carteira ainda não tem entradas."
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}

function M({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="text-right">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn("font-bold", tone)}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}
