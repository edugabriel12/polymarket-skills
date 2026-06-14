#!/usr/bin/env python3
"""Seed the predictions DB with realistic sample rows for offline UI demos.

The live model needs network (blocked in the sandbox), so this populates a mix
of OVER/UNDER predictions across several dates with settled (ACERTO/ERRO/ANULADO)
and PENDENTE statuses, each with a Polymarket market_url — enough to render both
dashboard tabs without any API calls.

Usage:
    python seed_demo.py [--db PATH] [--reset]
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import predictions_db as pdb

_GAMES = [
    ("hou", "kc"), ("nyy", "bos"), ("lad", "sf"), ("sd", "col"),
    ("cin", "mil"), ("atl", "phi"), ("tb", "tor"), ("sea", "tex"),
]


def _row(away, home, game_date, line, side, price, mu, status, size=120.0):
    slug = f"mlb-{away}-{home}-{game_date}"
    odds = round(1 / price, 3)
    p_over = round(min(0.85, max(0.15, 0.5 + (mu - line) * 0.08)), 3)
    model_prob = p_over if side == "OVER" else round(1 - p_over, 3)
    return {
        "game_slug": slug, "game_date": game_date,
        "market_question": f"Will total runs be over or under {line}?",
        "condition_id": f"0x{away}{home}{game_date.replace('-', '')}",
        "token_id": f"{slug}-{side.lower()}",
        "line": line, "side": side, "entry_price": price, "decimal_odds": odds,
        "model_prob": model_prob, "edge": round(model_prob - price, 3),
        "mu": mu, "variance": round(mu * 2, 2), "dispersion": 2.0,
        "park_factor": 118.0 if home == "col" else 100.0,
        "confidence": 0.6, "size_pct": 0.01, "size_usd": size,
        "kelly_fraction": 0.18, "used_external": True, "fee_rate": 0.0,
        "strategy": "mlb-totals-negbin",
        "market_url": f"https://polymarket.com/event/{slug}",
        "stats": {
            "model": "negative_binomial", "mu": mu, "variance": round(mu * 2, 2),
            "dispersion": 2.0, "negbin_r": mu, "negbin_p": 0.5,
            "league_baseline": 8.5, "park_factor": 118.0 if home == "col" else 100.0,
            "used_external": True,
            "inputs": {"home_off": 1.08, "away_off": 1.03, "home_sp": 0.98,
                       "away_sp": 1.02, "home_field": 0.1, "temp_f": 78,
                       "wind_out_mph": 9},
            "line": line, "p_over_eff": p_over, "p_under_eff": round(1 - p_over, 3),
            "p_push": 0.0, "chosen_side": side, "entry_price": price,
            "decimal_odds": odds, "model_prob": model_prob,
            "edge_after_fee": round(model_prob - price, 3),
            "book_sum": 1.0, "price_sane": True, "size_pct": 0.01, "size_usd": size,
            "confidence": 0.6,
        },
    }


def seed(db_path: str, reset: bool = False) -> int:
    if reset:
        con = pdb.connect(db_path)
        with con:
            con.execute("DELETE FROM predictions")
        con.close()

    today = date.today()
    plan = [
        # (days_ago, line, side, price, mu, status)
        (3, 8.5, "OVER", 0.52, 9.4, "ACERTO"),
        (3, 9.5, "UNDER", 0.55, 8.1, "ACERTO"),
        (3, 7.5, "OVER", 0.48, 8.0, "ERRO"),
        (2, 10.5, "OVER", 0.50, 11.6, "ACERTO"),
        (2, 8.5, "UNDER", 0.57, 7.4, "ERRO"),
        (2, 9.0, "OVER", 0.50, 9.5, "ANULADO"),   # integer line push
        (1, 8.5, "OVER", 0.53, 9.2, "ACERTO"),
        (1, 9.5, "UNDER", 0.52, 8.3, "ERRO"),
        (0, 8.5, "OVER", 0.50, 9.6, "PENDENTE"),
        (0, 7.5, "UNDER", 0.56, 6.9, "PENDENTE"),
        (0, 10.5, "OVER", 0.49, 11.2, "PENDENTE"),
    ]
    n = 0
    for i, (days_ago, line, side, price, mu, status) in enumerate(plan):
        away, home = _GAMES[i % len(_GAMES)]
        gd = (today - timedelta(days=days_ago)).isoformat()
        row = _row(away, home, gd, line, side, price, mu, status)
        rid = pdb.record_prediction(row, db_path)
        if status in ("ACERTO", "ERRO", "ANULADO"):
            actual = {"ACERTO": line + 1 if side == "OVER" else line - 1,
                      "ERRO": line - 1 if side == "OVER" else line + 1,
                      "ANULADO": line}[status]
            pdb.settle_prediction(rid, actual, db_path)
        n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="Seed demo predictions for the UI.")
    p.add_argument("--db", default=pdb.DEFAULT_DB)
    p.add_argument("--reset", action="store_true", help="Delete existing rows first")
    args = p.parse_args()
    n = seed(args.db, args.reset)
    print(f"Seeded {n} demo predictions into {args.db}")


if __name__ == "__main__":
    main()
