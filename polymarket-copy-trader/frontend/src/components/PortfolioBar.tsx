import { useQuery } from "@tanstack/react-query";
import { Wallet, TrendingUp, Layers, Coins } from "lucide-react";
import { api } from "@/lib/api";
import { usd, signedUsd, cn } from "@/lib/utils";

// Compact banner for the paper mock wallet, shown above every tab.
export function PortfolioBar() {
  const { data } = useQuery({
    queryKey: ["portfolio"],
    queryFn: () => api.portfolio(true),
    refetchInterval: 30000,
  });

  const cell = (
    icon: React.ReactNode,
    label: string,
    value: string,
    tone?: string
  ) => (
    <div className="flex items-center gap-2">
      <div className="text-muted-foreground">{icon}</div>
      <div>
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className={cn("text-sm font-bold", tone)}>{value}</div>
      </div>
    </div>
  );

  const pnl = data?.total_pnl ?? 0;
  return (
    <div className="flex flex-wrap items-center gap-x-8 gap-y-3 rounded-2xl border border-border bg-card/70 px-5 py-3 backdrop-blur">
      {cell(<Coins size={18} />, "Caixa (fake)", usd(data?.cash_balance))}
      {cell(<Layers size={18} />, "Em posição", usd(data?.positions_value))}
      {cell(<Wallet size={18} />, "Valor total", usd(data?.total_value))}
      {cell(
        <TrendingUp size={18} />,
        "P&L total",
        `${signedUsd(pnl)} (${((data?.total_pnl_pct ?? 0) * 100).toFixed(1)}%)`,
        pnl >= 0 ? "text-emerald-400" : "text-rose-400"
      )}
      {cell(<Layers size={18} />, "Posições abertas", String(data?.num_open_positions ?? 0))}
    </div>
  );
}
