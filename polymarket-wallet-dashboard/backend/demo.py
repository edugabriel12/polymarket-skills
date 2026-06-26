#!/usr/bin/env python3
"""Synthetic per-market records so the UI + rollup are demonstrable offline
(the Data API host is egress-blocked in the sandbox). Same record shape that
analyze_wallet.build_market_records emits, so the report is byte-for-byte real.
"""

from __future__ import annotations


def _rec(cond, title, slug, cat, total, realized, invested, won, n=3,
         event_slug="", current=0.0):
    return {
        "condition_id": cond, "title": title, "slug": slug, "eventSlug": event_slug or slug,
        "category": cat, "total_pnl": total, "realized_pnl": realized,
        "unrealized_pnl": round(total - realized, 2), "invested": invested,
        "current_value": current, "resolved": won is not None, "won": won, "n_trades": n,
    }


# A varied book: several sports + sub-types, wins and losses, a couple still open.
DEMO_RECORDS = [
    _rec("0x01", "Arsenal vs Chelsea", "epl-ars-che-2026-02-01", "Soccer", 120.0, 120.0, 100.0, True, 4),
    _rec("0x02", "Both teams to score - Arsenal vs Chelsea", "epl-ars-che-2026-02-01-btts",
         "Soccer", -45.0, -45.0, 45.0, False, 2),
    _rec("0x03", "Over/Under 2.5 goals - City vs Spurs", "epl-mci-tot-2026-02-02-total-2pt5",
         "Soccer", 30.0, 0.0, 50.0, None, 1, current=80.0),   # open
    _rec("0x04", "Total goals over 1.5 - Flamengo vs Palmeiras", "bra-fla-pal-2026-02-03-total-1pt5",
         "Soccer", 22.0, 22.0, 40.0, True, 2),
    _rec("0x05", "Alcaraz to win the match vs Sinner", "atp-alcaraz-sinner-2026-02-04",
         "Tennis", 60.0, 60.0, 80.0, True, 3),
    _rec("0x06", "Set betting - Swiatek vs Sabalenka 2-0", "wta-swiatek-sabalenka-2026-02-05-set-2-0",
         "Tennis", -30.0, -30.0, 30.0, False, 1),
    _rec("0x07", "Yankees moneyline vs Red Sox", "mlb-nyy-bos-2026-04-01", "Baseball", 18.0, 18.0, 50.0, True, 2),
    _rec("0x08", "Run line -1.5 Dodgers vs Padres", "mlb-lad-sd-2026-04-02-run-line",
         "Baseball", -50.0, -50.0, 50.0, False, 1),
    _rec("0x09", "T1 to win the series vs Gen.G", "lol-t1-geng-2026-03-01-series",
         "League of Legends", 90.0, 90.0, 70.0, True, 5),
    _rec("0x10", "Map 1 winner - G2 vs Fnatic", "lol-g2-fnc-2026-03-02-map-1",
         "League of Legends", -25.0, -25.0, 25.0, False, 1),
    _rec("0x11", "FaZe to win the series vs NAVI", "cs2-faze-navi-2026-03-03-series",
         "Counter-Strike", 40.0, 40.0, 60.0, True, 2),
    _rec("0x12", "Bitcoin above $120,000 by March", "crypto-btc-120k-2026-03-31",
         "Crypto", -20.0, 0.0, 20.0, None, 1, current=10.0),   # open
    _rec("0x13", "2028 Presidential election winner", "politics-2028-president-winner",
         "Politics", 200.0, 50.0, 150.0, None, 6, current=300.0),  # open outright
]


DEMO_CSV = (
    "Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro\n"
    '2026-06-25;"Curaçao vs. Côte d\'Ivoire: O/U 3.5";UNDER;Média;1,79;19999,96;78,6;15714,32\n'
    '2026-06-24;"Bosnia and Herzegovina vs. Qatar: O/U 2.5";OVER;Alta;1,68;99999,74;67,9;67924,81\n'
    '2026-06-24;"Spread: Morocco (-1.5)";MOROCCO;Alta;1,54;19999,97;53,8;10769,26\n'
    '2026-06-23;"Will Algeria win on 2026-06-23?";YES;Baixa;1,54;5999,99;-100;-5999,99\n'
    '2026-06-23;"Mexico vs. Korea Republic: Both Teams to Score";YES;Baixa;1,82;10000;-100;-10000\n'
    '2026-06-22;"Knicks vs. Spurs";KNICKS;Alta;1,99;100000;89;89000\n'
    '2026-06-21;"Boston Red Sox vs. Colorado Rockies";BOSTON RED SOX;Baixa;1,43;10000;-27,3;-2730\n'
    '2026-06-20;"Stars vs. Wild";STARS;Média;1,48;20000;-49,3;-9860\n'
    '2026-06-19;"UFC 328: Sean Strickland vs. Khamzat Chimaev";SEAN STRICKLAND;Alta;2,1;12000;79,8;9576\n'
    '2026-06-18;"Roland Garros ATP: Tiafoe vs Arnaldi";FRANCES TIAFOE;Média;1,7;15000;70;10500\n'
)


def demo_csv_report() -> dict:
    import wallet_report as wr
    rep = wr.analyze_csv(DEMO_CSV)
    rep["demo"] = True
    rep["filename"] = "exemplo_historico.csv"
    return rep


def demo_report() -> dict:
    import wallet_report as wr
    wr.attach_subcategories(DEMO_RECORDS)
    report = wr.rollup("demo (sample data)", DEMO_RECORDS, n_trades_total=31)
    report["markets"] = DEMO_RECORDS
    report["demo"] = True
    return report
