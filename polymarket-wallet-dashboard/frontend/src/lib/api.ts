// Typed client for the wallet-analyzer FastAPI backend (proxied at /api in dev).

export interface Metrics {
  markets: number;
  resolved: number;
  wins: number;
  losses: number;
  n_trades: number;
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  invested: number;
  current_value: number;
  win_rate: number | null;
  roi: number | null;
}

export interface SubCategory extends Metrics {
  subcategory: string;
}

export interface Category extends Metrics {
  category: string;
  subcategories: SubCategory[];
}

export interface MarketRecord {
  condition_id: string;
  title: string;
  slug: string;
  eventSlug: string;
  category: string;
  subcategory?: string;
  total_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  invested: number;
  current_value: number;
  resolved: boolean;
  won: boolean | null;
  n_trades: number;
}

export interface WalletReport {
  address: string;
  n_markets: number;
  n_trades: number;
  overall: Metrics;
  by_category: Category[];
  markets?: MarketRecord[];
  demo?: boolean;
  error?: string;
}

export async function fetchWallet(address: string, opts?: { enrichTags?: boolean; tradeLimit?: number }): Promise<WalletReport> {
  const p = new URLSearchParams({ address: address.trim() });
  if (opts?.enrichTags) p.set("enrich_tags", "true");
  if (opts?.tradeLimit) p.set("trade_limit", String(opts.tradeLimit));
  const r = await fetch(`/api/wallet?${p.toString()}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as WalletReport;
}
