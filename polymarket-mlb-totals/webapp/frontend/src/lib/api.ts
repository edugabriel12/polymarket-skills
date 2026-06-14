// Typed client for the FastAPI backend (proxied at /api in dev).

export interface Recommendation {
  token_id: string;
  side: string;
  size_pct: number;
  price: number;
  confidence: number;
  reasoning: string;
  strategy: string;
  fee_rate: number;
}

export interface StatsLog {
  model?: string;
  mu?: number;
  variance?: number;
  dispersion?: number;
  negbin_r?: number;
  negbin_p?: number;
  park_factor?: number;
  league_baseline?: number;
  used_external?: boolean;
  inputs?: Record<string, number | null>;
  p_over_eff?: number;
  p_under_eff?: number;
  p_push?: number;
  decimal_odds?: number;
  edge_after_fee?: number;
  chosen_side?: string;
}

export interface Suggestion {
  game: string;
  line: number;
  mu: number;
  prediction_id: number | null;
  recommendation: Recommendation;
  stats?: StatsLog;
  market_url?: string;
  status?: string;
  question?: string;
}

export interface Skipped {
  game: string;
  line?: number | null;
  side?: string | null;
  reason: string;
}

export interface AnalysesResponse {
  date: string;
  computed_at: string;
  cached: boolean;
  counts: { games: number; suggestions: number; skipped: number };
  suggestions: Suggestion[];
  skipped: Skipped[];
  disclaimer: string;
}

export interface PerfBlock {
  window: string;
  start: string;
  end: string;
  counts: { acerto: number; erro: number; pendente: number; anulado: number };
  settled: number;
  pnl: number;
  invested: number;
  roi: number | null;
  win_rate: number | null;
  win_rate_over: number | null;
  win_rate_under: number | null;
}

export interface PredictionRow {
  id: number;
  game_slug: string;
  game_date: string;
  line: number;
  side: string;
  entry_price: number;
  decimal_odds: number;
  edge: number;
  status: string;
  actual_total: number | null;
  market_url: string | null;
  size_usd: number;
}

export interface ResultsResponse {
  settlement: { checked: number; settled: unknown[] };
  performance: { daily: PerfBlock; weekly: PerfBlock; monthly: PerfBlock };
  pnl_by_day: { date: string; pnl: number }[];
  recent: PredictionRow[];
  generated_at: string;
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export const api = {
  analyses: (date?: string, force = false) =>
    get<AnalysesResponse>(
      `/api/analyses?${date ? `date=${date}&` : ""}force=${force}`
    ),
  results: () => get<ResultsResponse>("/api/results"),
  seedDemo: async (reset = true) => {
    const r = await fetch(`/api/seed-demo?reset=${reset}`, { method: "POST" });
    if (!r.ok) throw new Error(`seed failed: ${r.status}`);
    return r.json();
  },
};

export type Period = "daily" | "weekly" | "monthly";
