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
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
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

export const api = {
  entries: () => get<EntriesResponse>("/api/entries"),
  results: () => get<ResultsResponse>("/api/results"),
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
