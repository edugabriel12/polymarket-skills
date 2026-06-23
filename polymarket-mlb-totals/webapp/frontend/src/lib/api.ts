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

export type Sport = "mlb" | "soccer" | "tennis";

// Layer 1 + 3: full predictive distribution summary per prediction.
export interface Forecast {
  mean_total: number;
  median_total: number;
  most_likely_total: number;
  pi50: [number, number];
  pi80: [number, number];
  pi80_mass: number;
  entropy_bits: number;
  p_over: number;
  p_under: number;
}

export interface StatsLog {
  forecast?: Forecast;
  model?: string;
  market?: string; // soccer: TOTAL | BTTS
  chosen_side?: string; // OVER/UNDER/YES/NO
  mu?: number;
  variance?: number;
  dispersion?: number;
  negbin_r?: number;
  negbin_p?: number;
  park_factor?: number;
  league_baseline?: number;
  // soccer (Dixon-Coles)
  lam_home?: number;
  lam_away?: number;
  rho?: number;
  used_external?: boolean;
  inputs?: Record<string, number | null>;
  p_over_eff?: number;
  p_under_eff?: number;
  p_push?: number;
  model_prob?: number;
  decimal_odds?: number;
  edge_after_fee?: number;
  line?: number | null;
}

export interface Suggestion {
  game: string;
  market?: string; // soccer: TOTAL | BTTS
  side?: string;
  line: number | null;
  mu?: number;
  lam_home?: number;
  lam_away?: number;
  edge?: number;
  forecast?: Forecast;
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
  sport: Sport;
  date: string;
  computed_at: string;
  cached: boolean;
  counts: Record<string, number>;
  suggestions: Suggestion[];
  skipped: Skipped[];
  disclaimer: string;
  error?: string | null;
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
  market?: string;
  line: number | null;
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
  sport: Sport;
  settlement: { checked: number; settled: unknown[] };
  performance: { daily: PerfBlock; weekly: PerfBlock; monthly: PerfBlock };
  pnl_by_day: { date: string; pnl: number }[];
  recent: PredictionRow[];
  generated_at: string;
}

export interface ReliabilityBin {
  bucket: string;
  n: number;
  avg_pred: number;
  empirical: number;
}

export interface CalibrationResponse {
  sport: Sport;
  logged: number;
  settled: number;
  settled_bet: number;
  all: {
    n: number;
    brier: number | null;
    log_loss: number | null;
    reliability: ReliabilityBin[];
    ece?: number | null;
    mce?: number | null;
    brier_decomposition?: {
      reliability: number;
      resolution: number;
      uncertainty: number;
      brier: number;
    } | null;
  };
  bet: { n: number; brier: number | null; log_loss: number | null };
  clv: {
    n: number;
    avg_ref_price?: number;
    avg_close_price?: number;
    avg_clv?: number;
    beat_close_pct?: number;
  };
  settled_offline?: number;
  settled_feed?: number;
  note?: string;
  generated_at: string;
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export interface HealthResponse {
  odds_api_key: boolean;
  sharp_close: {
    enabled: boolean;
    has_key: boolean;
    started: boolean;
    lead_min: number;
    next_wave: string | null;
    waves_today?: string[];
    last_run: string | null;
    last_suggestions: number | null;
  };
}

export const api = {
  health: () => get<HealthResponse>("/api/health"),
  analyses: (sport: Sport, date?: string, force = false) =>
    get<AnalysesResponse>(
      `/api/analyses?sport=${sport}&${date ? `date=${date}&` : ""}force=${force}`
    ),
  results: (sport: Sport) => get<ResultsResponse>(`/api/results?sport=${sport}`),
  calibration: (sport: Sport) => get<CalibrationResponse>(`/api/calibration?sport=${sport}`),
  seedDemo: async (sport: Sport, reset = true) => {
    const r = await fetch(`/api/seed-demo?sport=${sport}&reset=${reset}`, { method: "POST" });
    if (!r.ok) throw new Error(`seed failed: ${r.status}`);
    return r.json();
  },
};

export type Period = "daily" | "weekly" | "monthly";
