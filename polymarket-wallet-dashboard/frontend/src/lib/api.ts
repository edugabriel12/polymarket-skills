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

export interface ConfidenceBucket extends Metrics {
  confidence: string;
}

export interface Category extends Metrics {
  category: string;
  subcategories: SubCategory[];
  by_confidence?: ConfidenceBucket[];
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
  address?: string;
  filename?: string;
  source?: string;
  n_markets: number;
  n_trades: number;
  overall: Metrics;
  by_confidence?: ConfidenceBucket[];
  by_category: Category[];
  markets?: MarketRecord[];
  demo?: boolean;
  error?: string;
}

export async function uploadCsv(file: File): Promise<WalletReport> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/wallet/csv", { method: "POST", body: fd });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as WalletReport;
}

export async function fetchCsvDemo(): Promise<WalletReport> {
  const r = await fetch("/api/csv-demo");
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as WalletReport;
}

// --- watched-wallet management + separated results ---------------------------
export interface ThresholdBand {
  floor: number;
  unit: number;
  n: number;
  min: number;
  median: number;
  max: number;
}

export interface WalletSummary {
  id: number;
  name: string;
  address: string;
  csv_filename?: string;
  created_at: string;
  n_markets?: number;
}

export interface WalletRecord extends WalletSummary {
  analysis: WalletReport;
  thresholds: Record<string, ThresholdBand>;
}

export interface ModelCategory {
  category: string;
  n_bets: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  invested: number;
  total_pnl: number;
  roi: number | null;
}

export interface ModelResults {
  entity: string;
  by_category: ModelCategory[];
  by_confidence: null;
}

async function jget<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export const wallets = {
  list: () => jget<{ wallets: WalletSummary[] }>("/api/wallets").then((x) => x.wallets),
  get: (id: number) => jget<WalletRecord>(`/api/wallets/${id}`),
  modelResults: () => jget<ModelResults>("/api/model-results"),
  add: async (name: string, address: string, file: File) => {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("address", address);
    fd.append("file", file);
    const r = await fetch("/api/wallets", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<WalletRecord>;
  },
  remove: async (id: number) => {
    const r = await fetch(`/api/wallets/${id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
};
