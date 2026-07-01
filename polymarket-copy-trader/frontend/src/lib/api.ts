// Typed client for the copy-trader backend. Dev server proxies /api -> :8002.

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`GET ${url} -> ${r.status}`);
  return r.json() as Promise<T>;
}

async function jsend<T>(url: string, method: string, form?: Record<string, string>): Promise<T> {
  const opts: RequestInit = { method };
  if (form) {
    const body = new FormData();
    Object.entries(form).forEach(([k, v]) => body.append(k, v));
    opts.body = body;
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `${method} ${url} -> ${r.status}`;
    try {
      const j = await r.json();
      if (j?.error) msg = j.error;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

export interface WalletStats {
  wallet_id: number;
  name: string;
  address: string;
  active: number;
  n_entries: number;
  n_executed: number;
  n_skipped: number;
  pct_executed: number;
  pct_failed: number;
  invested: number;
  total_pnl: number;
  roi: number;
  win_rate: number;
  n_wins: number;
  n_losses: number;
  avg_slippage: number;
}

export interface Entry {
  id: number;
  wallet_id: number;
  wallet_name?: string;
  wallet_address?: string;
  condition_id: string;
  token_id: string | null;
  market_question: string | null;
  market_url: string | null;
  copy_action: "BUY" | "SELL";
  source_price: number | null;
  executed_usd: number | null;
  requested_usd: number | null;
  shares: number | null;
  avg_fill_price: number | null;
  best_price: number | null;
  slippage_pct: number | null;
  volume_24h: number | null;
  status: "EXECUTED" | "SKIPPED";
  skip_reason: string | null;
  result_status: "OPEN" | "WIN" | "LOSS" | "VOID";
  current_price: number | null;
  realized_pnl: number | null;
  created_at: string;
}

export interface EntriesPage {
  total: number;
  page: number;
  page_size: number;
  entries: Entry[];
  stats?: WalletStats;
}

export interface OpenPosition {
  wallet_id: number;
  wallet_name: string | null;
  condition_id: string;
  market_question: string | null;
  market_url: string | null;
  side: string | null;
  shares: number;
  avg_entry: number;
  current_price: number;
  value: number;
  unrealized_pnl: number;
}

export interface Portfolio {
  starting_balance: number;
  cash_balance: number;
  positions_value: number;
  total_value: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  total_pnl_pct: number;
  num_open_positions: number;
  open_positions: OpenPosition[];
}

export interface Config {
  slippage_cap: number;
  max_usd: number;
  min_usd: number;
  starting_balance: number;
  disclaimer: string;
}

export const api = {
  config: () => jget<Config>("/api/config"),
  wallets: () => jget<{ wallets: WalletStats[] }>("/api/wallets"),
  addWallet: (name: string, address: string) =>
    jsend<{ wallet: any }>("/api/wallets", "POST", { name, address }),
  toggleWallet: (id: number, active: boolean) =>
    jsend<{ wallet: any }>(`/api/wallets/${id}`, "PATCH", { active: String(active) }),
  removeWallet: (id: number) => jsend<{ deleted: boolean }>(`/api/wallets/${id}`, "DELETE"),
  entries: (walletId: number | null, status: string | null, page: number, pageSize = 20) => {
    const q = new URLSearchParams();
    if (walletId != null) q.set("wallet_id", String(walletId));
    if (status) q.set("status", status);
    q.set("page", String(page));
    q.set("page_size", String(pageSize));
    return jget<EntriesPage>(`/api/entries?${q.toString()}`);
  },
  walletEntries: (id: number, page: number, pageSize = 20) =>
    jget<EntriesPage>(`/api/wallets/${id}/entries?page=${page}&page_size=${pageSize}`),
  results: () => jget<{ wallets: WalletStats[]; portfolio: Portfolio }>("/api/results"),
  portfolio: (refresh = true) => jget<Portfolio>(`/api/portfolio?refresh=${refresh}`),
  poll: () => jsend<{ recorded: number }>("/api/poll", "POST"),
  reset: () => jsend<{ reset: boolean }>("/api/portfolio/reset", "POST"),
};
