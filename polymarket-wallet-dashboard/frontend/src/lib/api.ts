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
  by_confidence?: ConfidenceBucket[];
}

export interface ConfidenceBucket extends Metrics {
  confidence: string;
}

export interface Category extends Metrics {
  category: string;
  subcategories: SubCategory[];
  by_confidence?: ConfidenceBucket[];
}

// Per-wallet forwarding filter: {category: {subcategory: [confidences]}}.
// `FilterTree` = options discovered in the CSV; `WalletFilters` = the user's selection.
export type FilterTree = Record<string, Record<string, string[]>>;
export type WalletFilters = Record<string, Record<string, string[]>>;

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
  filter_tree?: FilterTree;   // category -> subcategory -> [confidences] available to forward
  markets?: MarketRecord[];
  live_settled?: number;   // settled live bets in this report
  live_open?: number;      // open live bets (cards on Sports, not in the figures)
  demo?: boolean;
  error?: string;
}

// Lê o corpo de uma resposta não-ok, loga no console (com o detail do FastAPI) e
// devolve um Error rico. Usado por todos os fluxos de fetch para facilitar o debug.
async function httpError(r: Response, method: string, url: string): Promise<Error> {
  const body = await r.text().catch(() => "");
  console.error(`[api] ${method} ${url} -> HTTP ${r.status} ${r.statusText}`, body);
  return new Error(`HTTP ${r.status} ${r.statusText}${body ? ` — ${body.slice(0, 500)}` : ""}`);
}

export async function uploadCsv(file: File): Promise<WalletReport> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/wallet/csv", { method: "POST", body: fd });
  if (!r.ok) throw await httpError(r, "POST", "/api/wallet/csv");
  return (await r.json()) as WalletReport;
}

export async function fetchCsvDemo(): Promise<WalletReport> {
  const r = await fetch("/api/csv-demo");
  if (!r.ok) throw await httpError(r, "GET", "/api/csv-demo");
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
  filters?: WalletFilters | null;   // null = forward everything to Sports/Telegram
}

export interface WalletRecord extends WalletSummary {
  analysis: WalletReport;
  thresholds: Record<string, ThresholdBand>;
  filter_tree?: FilterTree;         // selectable options for the edit UI (from the CSV analysis)
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
  let r: Response;
  try {
    r = await fetch(url);
  } catch (err) {
    console.error(`[api] GET ${url} — falha de rede/fetch (backend fora do ar?):`, err);
    throw err;
  }
  if (!r.ok) throw await httpError(r, "GET", url);
  try {
    return (await r.json()) as T;
  } catch (err) {
    console.error(`[api] GET ${url} — resposta não é JSON válido:`, err);
    throw err;
  }
}

export interface DashBet {
  key?: string;
  event: string;
  category: string;
  subcategory: string;
  side: string;
  odds: number;
  entry_price: number;
  unit?: number;
  confidence?: string | null;
  total_position?: number;
  status: string;
  pnl?: number | null;
  live?: string;
  market_url?: string | null;
  updated_at?: string;
}

export interface DashBetsPage {
  total: number;
  page: number;
  page_size: number;
  bets: DashBet[];
}

export const wallets = {
  list: () => jget<{ wallets: WalletSummary[] }>("/api/wallets").then((x) => x.wallets),
  get: (id: number) => jget<WalletRecord>(`/api/wallets/${id}`),
  modelResults: () => jget<ModelResults>("/api/model-results"),
  bets: (id: number, category: string, page: number, pageSize = 20) =>
    jget<DashBetsPage>(
      `/api/wallets/${id}/bets?category=${encodeURIComponent(category)}&page=${page}&page_size=${pageSize}`
    ),
  modelBets: (category: string, page: number, pageSize = 20) =>
    jget<DashBetsPage>(
      `/api/model-bets?category=${encodeURIComponent(category)}&page=${page}&page_size=${pageSize}`
    ),
  openBets: (id: number, page: number, pageSize = 20) =>
    jget<DashBetsPage>(`/api/wallets/${id}/open-bets?page=${page}&page_size=${pageSize}`),
  modelOpenBets: (page: number, pageSize = 20) =>
    jget<DashBetsPage>(`/api/model-open-bets?page=${page}&page_size=${pageSize}`),
  add: async (name: string, address: string, file: File, filters?: WalletFilters) => {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("address", address);
    fd.append("file", file);
    if (filters) fd.append("filters", JSON.stringify(filters));
    const r = await fetch("/api/wallets", { method: "POST", body: fd });
    if (!r.ok) throw await httpError(r, "POST", "/api/wallets");
    return r.json() as Promise<WalletRecord>;
  },
  updateFilters: async (id: number, filters: WalletFilters) => {
    const fd = new FormData();
    fd.append("filters", JSON.stringify(filters));
    const r = await fetch(`/api/wallets/${id}`, { method: "PATCH", body: fd });
    if (!r.ok) throw await httpError(r, "PATCH", `/api/wallets/${id}`);
    return r.json() as Promise<WalletRecord>;
  },
  remove: async (id: number) => {
    const r = await fetch(`/api/wallets/${id}`, { method: "DELETE" });
    if (!r.ok) throw await httpError(r, "DELETE", `/api/wallets/${id}`);
    return r.json();
  },
};
