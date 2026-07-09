"""One-shot manual resolution sweep — use to clean up positions that
the daemon's sweep missed (e.g. Busan/Beijing held open after end_date
because the legacy 0.99 price threshold was too strict).

Behaviour:
  1. Loads weather_edge.db
  2. Finds EXECUTED entries past end_date with no cashout/resolution
  3. For each, fetches Gamma /markets?slug=... and inspects:
       - m["closed"] boolean (authoritative)
       - m["outcomePrices"] (fallback if closed but no 0.99 marker)
  4. Inserts resolutions row + closes paper_engine position
  5. Prints a summary

Safe to re-run: each successful resolution writes a resolutions row
that excludes the entry from future passes.

Usage:
  python force_resolution_sweep.py            # process all eligible
  python force_resolution_sweep.py --dry-run  # report only, no writes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent.parent / "polymarket-paper-trader" / "scripts"))

import weather_edge_db as db  # noqa: E402
import weather_edge_helpers as weh  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
PRICE_THRESHOLD = 0.95

# Lazily-loaded cities/stations lookup (for resolving lat/lon per city so we
# can pull the realized high temp from the Open-Meteo archive).
_CITIES_CACHE = None


def _cities():
    global _CITIES_CACHE
    if _CITIES_CACHE is None:
        _CITIES_CACHE = weh.load_cities()
    return _CITIES_CACHE


def _observed_value_for(row: dict) -> tuple:
    """Realized high temp for this entry's target date, in the market's unit.

    Gamma does NOT publish the observed temperature (only YES/NO settlement
    prices), so we reconstruct it from the Open-Meteo archive using the
    resolution station's lat/lon. Returns (value, note); value is None when
    coords are unknown or the archive has no data yet (~1-2 day lag).
    Populating resolutions.observed_value here builds the ground-truth base
    the strategy advisor needs to calibrate MAE.
    """
    city = row.get("city_resolved")
    end_date = (row.get("end_date") or "")[:10]
    unit = (row.get("threshold_unit") or "").upper()
    if not city or not end_date or unit not in ("C", "F"):
        return None, "not_temp_market"
    station = weh.resolve_station(city, _cities())
    if not station or station.get("lat") is None or station.get("lon") is None:
        return None, "no_station_coords"

    # v13.4: lowest-temperature markets record the observed MIN, not the max.
    slug = (row.get("market_slug") or "").lower()
    question = (row.get("market_question") or "").lower()
    is_low = slug.startswith("lowest") or "lowest temperature" in question
    key = ("observed_min_" if is_low else "observed_max_") + unit.lower()

    # v17 Africa pilot: a station flagged resolution_source='metar' resolves
    # from REAL station observations (aviationweather.gov METAR), NEVER the
    # Open-Meteo archive. On that continent the archive returns ERA5 reanalysis
    # whose bias exceeds the 1°C market tick, and there is no regional model, so
    # a gridded proxy can settle a market the wrong way. If the METAR is
    # unavailable we return (None, "metar_unavailable") and leave the market
    # unresolved until it arrives — deliberately NOT falling back to ERA5.
    if station.get("resolution_source") == "metar":
        obs = weh.fetch_metar_daily_extremes(station.get("station"), end_date)
        if not obs:
            return None, "metar_unavailable"
        val = obs.get(key)
        if val is None:
            return None, "metar_no_value"
        return float(val), "metar-aviationweather"

    arch = weh.fetch_open_meteo_archive(station["lat"], station["lon"], end_date)
    if not arch:
        return None, "archive_unavailable"
    val = arch.get(key)
    if val is None:
        return None, "archive_no_value"
    return float(val), "open-meteo-archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Regex copied from dashboard/services/positions.py — strip the bracket
# threshold suffix from a market_slug to recover the parent event slug.
_THRESHOLD_SUFFIX_RE = __import__("re").compile(
    r"-\d{1,3}(?:-\d{1,3})?[cf]?(?:or\w+)?$",
    __import__("re").IGNORECASE,
)


def _parent_event_slug(slug: str) -> str:
    if not slug:
        return ""
    stripped = _THRESHOLD_SUFFIX_RE.sub("", slug)
    return stripped or slug


def _fetch_market_by_slug(slug: str) -> dict | None:
    """Try Gamma /markets first; on miss, fall back to /events?slug=parent
    and find the sub-market whose slug ends in our threshold suffix."""
    if not slug:
        return None
    try:
        r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=15)
        r.raise_for_status()
        results = r.json()
        if isinstance(results, list) and results:
            return results[0]
    except Exception:
        pass
    # Fallback: hit /events with parent slug and find our bracket
    parent = _parent_event_slug(slug)
    if not parent or parent == slug:
        return None
    try:
        r = requests.get(f"{GAMMA}/events",
                          params={"slug": parent}, timeout=15)
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list) or not events:
            return None
        ev = events[0]
        for sm in (ev.get("markets") or []):
            if sm.get("slug") == slug:
                return sm
        # No exact slug match — pick the sub-market whose slug ends like ours
        suffix = slug[len(parent):] if slug.startswith(parent) else ""
        if suffix:
            for sm in (ev.get("markets") or []):
                if (sm.get("slug") or "").endswith(suffix):
                    return sm
    except Exception:
        pass
    return None


def resolve_one(row: dict, dry_run: bool) -> dict:
    slug = row["market_slug"]
    # v9.11: pre-populate city/side so even early-return rows show usable info
    base = {
        "entry_id": row["entry_id"],
        "city": row.get("city_resolved") or "?",
        "side": row["side"],
        "entry_price": row.get("entry_price"),
    }
    m = _fetch_market_by_slug(slug)
    if m is None:
        return {**base, "status": "not_found"}
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
    except Exception:
        return {**base, "status": "bad_response"}
    if not outcomes or not prices or len(outcomes) != len(prices):
        return {**base, "status": "no_prices"}

    gamma_closed = bool(m.get("closed"))
    if prices[0] >= PRICE_THRESHOLD:
        final = "YES"
    elif prices[1] >= PRICE_THRESHOLD:
        final = "NO"
    elif gamma_closed:
        final = "VOID"
    else:
        return {**base, "status": "not_settled",
                "prices": prices, "closed": gamma_closed}

    payout = 1.0 if final == row["side"] else 0.0
    if final == "VOID":
        payout = float(row["entry_price"] or 0)

    pnl_est = (payout - float(row["entry_price"] or 0)) * float(row["size_shares"] or 0)
    # Realized temperature (Open-Meteo archive) → resolutions.observed_value.
    observed_value, observed_note = _observed_value_for(row)
    out = {
        **base,
        "final_outcome": final, "payout": payout,
        "pnl_estimate_usd": round(pnl_est, 2),
        "closed_flag": gamma_closed, "prices": prices,
        "observed_value": observed_value, "observed_source": observed_note,
        "status": "would_resolve" if dry_run else "resolved",
    }
    if dry_run:
        return out

    # Write resolution + close paper position
    with db.connect() as conn:
        db.insert_resolution(
            conn, entry_id=row["entry_id"],
            ts_resolved=_now_iso(),
            final_outcome=final,
            payout_per_share=payout,
            observed_value=observed_value,
        )
        conn.commit()

    try:
        import paper_engine
        token_id = (row["token_id_yes"] if row["side"] == "YES"
                     else row["token_id_no"])
        if token_id:
            try:
                paper_engine.close_position(
                    token_id=token_id, side=row["side"],
                    reasoning=f"force_resolution:{final}",
                    force_exit_price=payout,
                )
                out["paper_closed"] = True
            except RuntimeError as ce:
                out["paper_closed"] = False
                out["paper_err"] = str(ce)
    except ImportError as e:
        out["paper_err"] = str(e)
    return out


def _test_metar_resolution() -> int:
    """Hermetic test (no network) of the Africa-pilot METAR resolution path:
      (A) weh.fetch_metar_daily_extremes parses max/min for the target UTC day
          only, and fails-open (None) on non-200 / empty / no temps.
      (B) _observed_value_for routes pilot stations (resolution_source='metar')
          to the METAR source and NEVER touches the ERA5 archive — including
          when the METAR is unavailable, where it returns 'metar_unavailable'.
      (C) a non-pilot (EU) station still uses the archive, unchanged.
    """
    global _CITIES_CACHE
    _CITIES_CACHE = None  # force fresh load of the real weather-cities.json

    saved_get = weh.requests.get
    saved_metar = weh.fetch_metar_daily_extremes
    saved_archive = weh.fetch_open_meteo_archive

    class _R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    try:
        # (A) parsing — two obs on the target day (28/32°C) + one on another day
        # (99°C, must be excluded); mix reportTime strings and obsTime epochs.
        target = "2026-07-08"
        # 2026-07-09 12:00 UTC epoch for the out-of-day ob (obsTime path).
        other_epoch = 1783598400
        def fake_ok(url, params=None, **kw):
            return _R(200, [
                {"temp": 28.0, "reportTime": "2026-07-08 06:00:00"},
                {"temp": 32.0, "reportTime": "2026-07-08 13:00:00"},
                {"temp": 99.0, "obsTime": other_epoch},   # 07-09 → excluded
                {"temp": None, "reportTime": "2026-07-08 20:00:00"},  # skipped
            ])
        weh.requests.get = fake_ok
        obs = weh.fetch_metar_daily_extremes("HECA", target)
        assert obs and obs["observed_max_c"] == 32.0 and obs["observed_min_c"] == 28.0, obs
        assert obs["n_obs"] == 2 and obs["source"] == "metar-aviationweather", obs
        assert obs["observed_max_f"] == round(32.0 * 9 / 5 + 32, 2), obs
        print("Test MR-A1 PASS: parses max/min for target day only (32/28°C, "
              "out-of-day 99°C excluded)")

        weh.requests.get = lambda *a, **k: _R(500, [])
        assert weh.fetch_metar_daily_extremes("HECA", target) is None
        print("Test MR-A2 PASS: non-200 → None (fail-open)")

        weh.requests.get = lambda *a, **k: _R(200, [])
        assert weh.fetch_metar_daily_extremes("HECA", target) is None
        print("Test MR-A3 PASS: empty report list → None")

        weh.requests.get = lambda *a, **k: _R(
            200, [{"temp": 30.0, "reportTime": "2026-07-01 06:00:00"}])
        assert weh.fetch_metar_daily_extremes("HECA", target) is None
        print("Test MR-A4 PASS: no obs on target day → None")

        weh.requests.get = saved_get

        # (B) routing — pilot station uses METAR, archive NEVER called.
        arch_calls = {"n": 0}
        def spy_archive(*a, **k):
            arch_calls["n"] += 1
            return {"observed_max_c": 40.0, "observed_min_c": 20.0,
                    "observed_max_f": 104.0, "observed_min_f": 68.0}
        weh.fetch_open_meteo_archive = spy_archive

        pilot_row = {"city_resolved": "Cairo",
                     "end_date": "2026-07-08T23:59:00Z",
                     "threshold_unit": "C",
                     "market_slug": "highest-temperature-in-cairo-on-july-8",
                     "market_question": "Highest temperature in Cairo?"}

        weh.fetch_metar_daily_extremes = lambda icao, d, **k: {
            "observed_max_c": 41.5, "observed_min_c": 26.1,
            "observed_max_f": 106.7, "observed_min_f": 79.0}
        val, note = _observed_value_for(pilot_row)
        assert val == 41.5 and note == "metar-aviationweather", (val, note)
        assert arch_calls["n"] == 0, "ERA5 archive must NOT be called for pilot"
        print("Test MR-B1 PASS: pilot high market → METAR max 41.5°C "
              "(ERA5 archive untouched)")

        # lowest-temperature pilot market → observed MIN via METAR.
        low_row = dict(pilot_row,
                       market_slug="lowest-temperature-in-cairo-on-july-8",
                       market_question="Lowest temperature in Cairo?")
        val, note = _observed_value_for(low_row)
        assert val == 26.1 and note == "metar-aviationweather", (val, note)
        print("Test MR-B2 PASS: pilot low market → METAR min 26.1°C")

        # METAR unavailable → 'metar_unavailable', still NO ERA5 fallback.
        weh.fetch_metar_daily_extremes = lambda icao, d, **k: None
        val, note = _observed_value_for(pilot_row)
        assert val is None and note == "metar_unavailable", (val, note)
        assert arch_calls["n"] == 0, "no silent ERA5 fallback when METAR missing"
        print("Test MR-B3 PASS: METAR unavailable → 'metar_unavailable' "
              "(no ERA5 fallback)")

        # (C) non-pilot EU station still uses the archive; METAR never called.
        metar_calls = {"n": 0}
        def spy_metar(*a, **k):
            metar_calls["n"] += 1
            return None
        weh.fetch_metar_daily_extremes = spy_metar
        eu_row = dict(pilot_row, city_resolved="Lisbon",
                      market_slug="highest-temperature-in-lisbon-on-july-8",
                      market_question="Highest temperature in Lisbon?")
        val, note = _observed_value_for(eu_row)
        assert val == 40.0 and note == "open-meteo-archive", (val, note)
        assert metar_calls["n"] == 0, "non-pilot must not call METAR"
        assert arch_calls["n"] == 1, arch_calls
        print("Test MR-C1 PASS: non-pilot EU station → ERA5 archive (METAR "
              "untouched)")

        print("\nAll --test-metar-resolution PASS")
        return 0
    finally:
        weh.requests.get = saved_get
        weh.fetch_metar_daily_extremes = saved_metar
        weh.fetch_open_meteo_archive = saved_archive
        _CITIES_CACHE = None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would happen, don't write")
    p.add_argument("--test-metar-resolution", action="store_true",
                   help="Run the hermetic Africa-pilot METAR resolution test")
    args = p.parse_args()

    if args.test_metar_resolution:
        return _test_metar_resolution()

    with db.connect() as conn:
        rows = [dict(r) for r in db.query_unresolved_past_end(conn, _now_iso())]

    print(f"Found {len(rows)} EXECUTED entries past end_date.")
    if not rows:
        return 0

    results = []
    for row in rows:
        r = resolve_one(row, dry_run=args.dry_run)
        results.append(r)
        marker = "DRY" if args.dry_run else "OK"
        obs = r.get("observed_value")
        obs_s = f"{obs:.1f}" if obs is not None else (r.get("observed_source") or "-")
        print(f"  #{r['entry_id']:>3} {r.get('city','?'):<14} "
              f"{r.get('side','?'):<3} → {r.get('status'):<20} "
              f"{r.get('final_outcome','-'):<5} "
              f"payout=${r.get('payout',0):.2f} "
              f"pnl≈${r.get('pnl_estimate_usd',0):+.2f} "
              f"obs={obs_s}")

    total_pnl = sum(r.get("pnl_estimate_usd", 0)
                     for r in results
                     if r.get("status") in ("resolved", "would_resolve"))
    n_resolved = sum(1 for r in results
                      if r.get("status") in ("resolved", "would_resolve"))
    n_pending = sum(1 for r in results if r.get("status") == "not_settled")
    n_failed = len(results) - n_resolved - n_pending
    print()
    print(f"Summary: {n_resolved} resolved, {n_pending} still not settled, "
          f"{n_failed} failed")
    print(f"Estimated realized P&L: ${total_pnl:+.2f}")
    if args.dry_run:
        print("(DRY RUN — no changes written. Re-run without --dry-run to apply.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
