import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

export const usd = (v: number | null | undefined) =>
  v === null || v === undefined
    ? "—"
    : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

export const signedUsd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;

// Strip a per-market suffix (…-total-9pt5, …-btts) so the link points at the game
// event, not a single line. Normalizes URLs stored before the backend fix too.
const MARKET_SUFFIX_RE = /-(?:total-\d{1,2}(?:pt5)?|btts|both-teams-to-score|gg)(?=$|[/?#])/;

export const gameEventUrl = (url: string) => url.replace(MARKET_SUFFIX_RE, "");
