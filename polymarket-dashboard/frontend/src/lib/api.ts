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

// Fired whenever any request gets a 401 — lets the AuthProvider drop back to the login screen
// the moment a session expires or is revoked (e.g. after a password reset elsewhere).
let _onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  _onUnauthorized = fn;
}

async function get<T>(url: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, { credentials: "include" }); // send the session cookie
  } catch (err) {
    // backend fora do ar, proxy/porta errada, DNS, CORS…
    console.error(`[api] GET ${url} — falha de rede/fetch:`, err);
    throw err;
  }
  if (r.status === 401) _onUnauthorized?.();
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

// POST JSON and return the parsed body. The auth endpoints answer 200 with {error|message},
// so we only throw on a network/parse failure — logical errors come back in the body.
async function post<T>(url: string, body: unknown): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (err) {
    console.error(`[api] POST ${url} — falha de rede/fetch:`, err);
    throw err;
  }
  if (r.status === 401) _onUnauthorized?.();
  try {
    return (await r.json()) as T;
  } catch (err) {
    console.error(`[api] POST ${url} — resposta não é JSON válido:`, err);
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

export interface User {
  id: number;
  full_name: string;
  email: string;
  email_verified: boolean;
}

// Auth endpoints answer 200 with a subset of these fields.
export interface AuthResult {
  ok?: boolean;
  error?: string;
  message?: string;
  needs_verification?: boolean;
  user?: User;
}

export const api = {
  entries: () => get<EntriesResponse>("/api/entries"),
  results: () => get<ResultsResponse>("/api/results"),
  resultsBets: (category: string, page: number, pageSize = 20) =>
    get<BetsPage>(
      `/api/results/bets?category=${encodeURIComponent(category)}&page=${page}&page_size=${pageSize}`
    ),
  telegramStatus: () => get<TelegramStatus>("/api/telegram"),
  telegramSave: (token: string) =>
    post<TelegramSaveResult>("/api/telegram", { token }),

  me: () => get<{ user: User }>("/api/me"),
  auth: {
    register: (full_name: string, email: string, password: string, password_confirm: string) =>
      post<AuthResult>("/api/auth/register", { full_name, email, password, password_confirm }),
    login: (email: string, password: string) =>
      post<AuthResult>("/api/auth/login", { email, password }),
    logout: () => post<AuthResult>("/api/auth/logout", {}),
    verify: (token: string) => post<AuthResult>("/api/auth/verify", { token }),
    resendVerification: (email: string) =>
      post<AuthResult>("/api/auth/resend-verification", { email }),
    forgotPassword: (email: string) =>
      post<AuthResult>("/api/auth/forgot-password", { email }),
    resetPassword: (token: string, password: string, password_confirm: string) =>
      post<AuthResult>("/api/auth/reset-password", { token, password, password_confirm }),
  },
};

export const unitLabel = (u: number) =>
  u === 1 ? "1U" : u === 0.5 ? "0.5U" : u === 0.25 ? "0.25U" : `${u}U`;
