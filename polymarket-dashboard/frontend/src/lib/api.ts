// Typed client for the Polymarket Sports storefront backend (proxied at /api in dev).

export interface Entry {
  key: string;
  event: string;
  category: string;
  subcategory: string;
  side: string;
  odds: number;
  entry_price: number;
  unit: number;
  confidence: string | null;
  live: string; // "LIVE" | "PRÉ-LIVE"
  market_url: string | null;
  game_start: string | null;
  status: string;
  pnl: number | null;
}

export interface EntryCategory {
  category: string;
  entries: Entry[];
}

export interface EntriesResponse {
  n_open: number;
  categories: EntryCategory[];
}

export interface UnitMetrics {
  unit?: number;
  unit_label?: string;
  n_bets: number;
  wins: number;
  losses: number;
  voids: number;
  staked_u: number;
  pnl_u: number;
  win_rate: number | null;
  roi: number | null;
}

export interface CategoryResult extends UnitMetrics {
  category: string;
  by_unit: UnitMetrics[];
}

export interface ResultsResponse {
  n_bets: number;
  overall: UnitMetrics;
  by_unit: UnitMetrics[];
  by_category: CategoryResult[];
}

async function get<T>(url: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url);
  } catch (err) {
    // backend fora do ar, proxy/porta errada, DNS, CORS…
    console.error(`[api] GET ${url} — falha de rede/fetch:`, err);
    throw err;
  }
  if (!r.ok) {
    const body = await r.text().catch(() => "");
    console.error(`[api] GET ${url} -> HTTP ${r.status} ${r.statusText}`, body);
    throw new Error(`HTTP ${r.status} ${r.statusText}${body ? ` — ${body.slice(0, 500)}` : ""}`);
  }
  try {
    return (await r.json()) as T;
  } catch (err) {
    console.error(`[api] GET ${url} — resposta não é JSON válido:`, err);
    throw err;
  }
}

export interface TelegramStatus {
  configured: boolean;
  chat_id: string;
}

export interface TelegramSaveResult {
  ok: boolean;
  chat_id?: string;
  tested?: boolean;
  error?: string | null;
}

export interface BetsPage {
  total: number;
  page: number;
  page_size: number;
  bets: Entry[];
}

export const api = {
  entries: () => get<EntriesResponse>("/api/entries"),
  results: () => get<ResultsResponse>("/api/results"),
  resultsBets: (category: string, page: number, pageSize = 20) =>
    get<BetsPage>(
      `/api/results/bets?category=${encodeURIComponent(category)}&page=${page}&page_size=${pageSize}`
    ),
  telegramStatus: () => get<TelegramStatus>("/api/telegram"),
  telegramSave: async (token: string): Promise<TelegramSaveResult> => {
    const r = await fetch("/api/telegram", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<TelegramSaveResult>;
  },
};

export const unitLabel = (u: number) =>
  u === 1 ? "1U" : u === 0.5 ? "0.5U" : u === 0.25 ? "0.25U" : `${u}U`;
