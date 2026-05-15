#!/usr/bin/env python3
"""Weather edge bot — discovery, entry proposal, monitor, cashout.

Runs 24/7 as a daemon. Each loop iteration (60s tick) decides which tasks to
run based on per-task last_run timestamps:

  Discovery  → every 10 min: scan weather markets resolving in next 48h,
                parse, fetch forecast, compute edge, INSERT entries(PROPOSED).
  Execute    → every 60s: pick up entries(APPROVED) from judge, re-validate
                orderbook + risk, execute via paper_engine, mark EXECUTED.
  Monitor    → every 60s: walk open positions; per-position adaptive cadence
                (30 min if TTR<24h, 60 min if 24-48h). Cash out only when
                forecast_prob < entry_implied AND best_bid >= entry_price.
  Resolution → every 1h: poll Gamma for outcomePrices on past-end positions.
  Heartbeat  → every 5 min: emit health log line.

Paper-only mode (default). To bypass the judge gatekeeper, pass --judge-mode=off
(operator override; not recommended).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "polymarket-scanner" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "polymarket-paper-trader" / "scripts"))


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dep). Reads agent/.env if present
    and sets missing env vars. Existing OS env vars take precedence."""
    env_path = REPO_ROOT / "agent" / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

from weather_edge_helpers import (  # noqa: E402
    parse_market, forecast_probability, compute_max_size_for_slippage,
    implied_probabilities, compute_edge, load_cities, MarketSpec,
    evaluate_cashout_triggers,
)
import weather_edge_db as db  # noqa: E402

LOG_DIR = Path.home() / ".polymarket-paper"
LOG_FILE = LOG_DIR / "weather_edge.jsonl"  # default; overridable via --log-file

GAMMA_API = "https://gamma-api.polymarket.com"
WEATHER_TAG = "weather"
FORECAST_SCRIPT = REPO_ROOT / "polymarket-forecast-skill" / "scripts" / "get_weather.py"

# Keywords used to client-side filter markets to weather-related ones.
# (Gamma's tag_slug=weather param is silently ignored, so we filter ourselves.)
import re as _re
_WEATHER_KEYWORDS = _re.compile(
    r"\b(weather|temperature|temp|rain|rainfall|snow|snowfall|"
    r"precipitation|precip|hurricane|storm|wind|fahrenheit|celsius|"
    r"hottest|coldest|warmest|coolest|degrees|°[fc]|inches\s+of|"
    r"mm\s+of|cm\s+of)\b",
    _re.IGNORECASE,
)

# Default cadences (seconds)
DISCOVERY_INTERVAL = 600        # 10 min
MONITOR_TICK = 60               # check every minute; per-position adaptive
HEARTBEAT_INTERVAL = 300        # 5 min
RESOLUTION_SWEEP_INTERVAL = 3600  # 1 hour
EXECUTE_INTERVAL = 60           # check approved queue every minute

PAPER_DB = Path.home() / ".polymarket-paper" / "portfolio.db"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event_type: str, payload: dict | None = None, level: str = "INFO") -> None:
    """Append one JSONL line to the log file AND print to stdout (journald)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "level": level, "event_type": event_type,
           "payload": payload or {}}
    line = json.dumps(rec, default=str, ensure_ascii=False)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[log-error] {e}", file=sys.stderr, flush=True)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Gamma + forecast helpers
# ---------------------------------------------------------------------------


def _fetch_weather_events(now: datetime, max_pages: int = 3) -> list[dict]:
    """Fetch weather-tagged events from /events and flatten to their markets.

    Some Polymarket weather markets only appear via the /events endpoint
    (multi-outcome events). Each event contains a `markets` array of binary
    sub-markets (one per bracket).
    """
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out: list[dict] = []
    # Try a few param variations
    for params_try in [
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "tag_slug": "weather", "limit": 100},
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "tag": "weather", "limit": 100},
        # Search by query (events with weather/temperature in title)
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "q": "temperature", "limit": 100},
    ]:
        for page in range(max_pages):
            p = {**params_try, "offset": page * 100}
            try:
                r = requests.get(f"{GAMMA_API}/events", params=p, timeout=30)
                r.raise_for_status()
                events = r.json()
            except requests.exceptions.RequestException as e:
                log_event("error", {"where": "_fetch_weather_events",
                                     "params": list(params_try.keys()),
                                     "page": page, "err": str(e)}, level="WARN")
                break
            if not isinstance(events, list) or not events:
                break
            for ev in events:
                ev_title = ev.get("title", "")
                ev_slug = ev.get("slug", "")
                # Each event has a `markets` array of binary sub-markets
                sub_markets = ev.get("markets") or []
                for sm in sub_markets:
                    if not isinstance(sm, dict):
                        continue
                    # Inject parent event context so the parser can resolve city
                    sm["events"] = sm.get("events") or [{"title": ev_title,
                                                          "slug": ev_slug}]
                    out.append(sm)
            if len(events) < 100:
                break
        if out:
            log_event("events_endpoint_hit", {"variant": list(params_try.keys()),
                                                "events_returned": len(out)})
            return out
    return out


def _fetch_with_params(params: dict, max_pages: int = 5) -> list[dict]:
    """Paginate Gamma /markets with given params. Returns flat list."""
    out: list[dict] = []
    for page in range(max_pages):
        p = {**params, "limit": 100, "offset": page * 100}
        try:
            r = requests.get(f"{GAMMA_API}/markets", params=p, timeout=30)
            r.raise_for_status()
            batch = r.json()
        except requests.exceptions.RequestException as e:
            log_event("error", {"where": "_fetch_with_params", "page": page,
                                "err": str(e)}, level="WARN")
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def fetch_weather_markets(min_volume: float = 5000, max_pages: int = 5) -> list[dict]:
    """Fetch markets ending in next ~7 days, filter client-side to weather.

    Strategy: server-side filter `end_date_min=now` so we skip the pile of
    stale 2025 markets that Gamma still returns with closed=false. Try a few
    param-name variants because Gamma's parameter naming changes between
    versions (snake_case vs camelCase).
    """
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Try multiple known variants of the date-filter param name. First one
    # that returns mostly future-ending markets wins.
    candidate_params = [
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "end_date_min": now_iso},
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "endDateMin": now_iso},
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "start_date_max": now_iso, "end_date_min": now_iso},
        # Last resort: no date filter, hope newer markets come back
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "false"},  # newest endDate first
    ]

    all_markets: list[dict] = []
    chosen_param_set = None
    for idx, params in enumerate(candidate_params):
        batch = _fetch_with_params(params, max_pages=max_pages)
        if not batch:
            continue
        # Validate: count how many have endDate > now in the first 50
        future_count = 0
        for m in batch[:50]:
            try:
                ed = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
                if ed >= now:
                    future_count += 1
            except (ValueError, TypeError):
                pass
        log_event("fetch_attempt", {"variant": idx, "params": list(params.keys()),
                                    "returned": len(batch),
                                    "future_in_first_50": future_count})
        if future_count >= 30:  # accept if most are future
            all_markets = batch
            chosen_param_set = idx
            break

    if not all_markets:
        log_event("fetch_failed", {"reason": "no variant returned mostly future markets"},
                  level="WARN")
        return []
    log_event("fetch_using_variant", {"variant": chosen_param_set,
                                       "total": len(all_markets)})

    # Dump first 3 markets in compact form so we can see what fields exist.
    for i, m in enumerate(all_markets[:3]):
        events = m.get("events") or []
        ev_titles = [e.get("title") for e in events if isinstance(e, dict)]
        log_event("fetch_sample", {
            "i": i,
            "question": (m.get("question") or "")[:120],
            "slug": (m.get("slug") or "")[:80],
            "endDate": m.get("endDate"),
            "volumeNum": m.get("volumeNum"),
            "event_titles": ev_titles[:2],
            "event_slugs": [e.get("slug") for e in events if isinstance(e, dict)][:2],
            "all_keys": sorted(m.keys())[:30],
        })

    # Also try /events endpoint and merge any weather-tagged markets we find
    events_extras = _fetch_weather_events(now)
    if events_extras:
        log_event("events_endpoint_added", {"count": len(events_extras)})
        all_markets.extend(events_extras)

    out = []
    n_keyword_match = 0
    n_past_end = 0
    n_low_vol = 0
    for m in all_markets:
        question = m.get("question", "")
        events = m.get("events") or []
        event_text = " ".join(
            (e.get("title", "") + " " + e.get("slug", ""))
            for e in events if isinstance(e, dict))
        combined = f"{event_text} {question}"
        if not _WEATHER_KEYWORDS.search(combined):
            continue
        n_keyword_match += 1
        # Stash combined text on the market dict for the discovery stage.
        m["_combined_text"] = combined.strip()
        # Skip clearly stale markets (endDate already past)
        try:
            end_date = datetime.fromisoformat(
                (m.get("endDate") or "").replace("Z", "+00:00"))
            if end_date < now:
                n_past_end += 1
                continue
        except (ValueError, TypeError):
            continue
        try:
            vol = float(m.get("volumeNum", 0) or 0)
            if vol < min_volume:
                n_low_vol += 1
                continue
            out.append(m)
        except (ValueError, TypeError):
            continue

    log_event("fetch_summary", {
        "total_fetched": len(all_markets),
        "keyword_matches": n_keyword_match,
        "past_end_skipped": n_past_end,
        "low_volume_skipped": n_low_vol,
        "passed": len(out),
    })
    return out


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook directly from CLOB HTTP endpoint (no SDK dependency).

    Endpoint returns: {market, asset_id, bids: [...], asks: [...]} where each
    side has {price, size} entries (as strings).
    """
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=15)
        if r.status_code != 200:
            log_event("error", {"where": "fetch_orderbook",
                                 "token_id": token_id[:12],
                                 "status": r.status_code}, level="WARN")
            return None
        data = r.json()
        bids = sorted(
            [{"price": float(b["price"]), "size": float(b["size"])}
             for b in (data.get("bids") or [])],
            key=lambda b: b["price"], reverse=True,
        )
        asks = sorted(
            [{"price": float(a["price"]), "size": float(a["size"])}
             for a in (data.get("asks") or [])],
            key=lambda a: a["price"],
        )
        return {"bids": bids, "asks": asks}
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        log_event("error", {"where": "fetch_orderbook", "token_id": token_id[:12],
                            "err": str(e)}, level="WARN")
        return None


_FORECAST_KEY_WARNED = False
# Per-process cache of cities that OpenWeather doesn't recognize.
# Avoids re-querying every discovery cycle for cities the parser extracted
# but OpenWeather can't geocode (typos, very small towns, ambiguous names).
_FORECAST_UNKNOWN_CITIES: set[str] = set()


def fetch_forecast(city: str, days: int = 5) -> Optional[dict]:
    """Subprocess get_weather.py forecast and return parsed JSON, or None.

    Caches the set of cities OpenWeather couldn't resolve, so repeat
    discovery cycles don't waste API calls on the same bad names.
    """
    global _FORECAST_KEY_WARNED
    if not os.environ.get("OPENWEATHER_API_KEY"):
        if not _FORECAST_KEY_WARNED:
            log_event("config_missing", {"key": "OPENWEATHER_API_KEY",
                                          "impact": "all forecast lookups will fail"},
                      level="ERROR")
            _FORECAST_KEY_WARNED = True
        return None

    if city in _FORECAST_UNKNOWN_CITIES:
        return None

    if not FORECAST_SCRIPT.exists():
        log_event("error", {"where": "fetch_forecast",
                            "err": f"forecast script not found: {FORECAST_SCRIPT}"},
                  level="ERROR")
        return None

    try:
        env = {**os.environ}  # ensure subprocess inherits OPENWEATHER_API_KEY
        result = subprocess.run(
            [sys.executable, str(FORECAST_SCRIPT), "forecast", city, str(days)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0:
            # If OpenWeather doesn't recognize the city, cache it so we don't
            # retry every discovery cycle. Heuristic: stderr mentions 404 / not
            # found, or non-zero exit on first try.
            stderr = (result.stderr or "")[:500]
            if any(s in stderr.lower() for s in ("404", "not found",
                                                  "city not found")):
                _FORECAST_UNKNOWN_CITIES.add(city)
            log_event("error", {"where": "fetch_forecast", "city": city,
                                "exit_code": result.returncode,
                                "stderr": stderr,
                                "stdout": result.stdout[:200],
                                "cached_as_unknown": city in _FORECAST_UNKNOWN_CITIES},
                      level="WARN")
            return None
        out = result.stdout.strip()
        if not out:
            log_event("error", {"where": "fetch_forecast", "city": city,
                                "err": "empty stdout from get_weather.py"}, level="WARN")
            return None
        return json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log_event("error", {"where": "fetch_forecast", "city": city,
                            "err": str(e), "type": type(e).__name__}, level="WARN")
        return None


# ---------------------------------------------------------------------------
# Discovery cycle: scan + propose entries
# ---------------------------------------------------------------------------


def run_discovery(args, cities: dict) -> int:
    """Run one discovery scan. Returns count of new proposals inserted."""
    log_event("discovery_start", {"min_edge_pp": args.min_edge_pp,
                                  "min_volume": args.min_volume,
                                  "window_hours": args.window_hours})
    raw_markets = fetch_weather_markets(min_volume=args.min_volume)
    log_event("discovery_markets_fetched", {"count": len(raw_markets)})
    if not raw_markets:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=args.window_hours)
    proposed = 0
    # Diagnostic counters
    skipped = {
        "no_end_date": 0, "outside_window": 0, "parser_failed": 0,
        "parser_low_confidence": 0, "no_token_ids": 0,
        "orderbook_unavailable": 0, "no_implied_prices": 0,
        "price_band_miss": 0, "forecast_unavailable": 0,
        "no_forecast_for_target_date": 0, "low_edge": 0,
        "duplicate_pending": 0, "opposite_side_held": 0,
        "market_exposure_full": 0,
    }

    # Phase 1: build candidate proposals using HTTP-only work (no DB lock held).
    # Phase 2: snapshot already-proposed (slug, side) pairs with a brief read.
    # Phase 3: batch-insert proposals, committing per-row so the judge can
    # interleave its writes between iterations.

    candidates: list[dict] = []
    for m in raw_markets:
        slug = m.get("slug", "")
        question = m.get("question", "")
        end_date_str = m.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            skipped["no_end_date"] += 1
            continue

        if end_date < now or end_date > cutoff:
            skipped["outside_window"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                                              "reason": "outside_window",
                                              "end_date": end_date_str,
                                              "ttr_h": round((end_date - now).total_seconds() / 3600, 1)})
            continue
        ttr_hours = (end_date - now).total_seconds() / 3600.0

        if m.get("acceptingOrders") is False:
            skipped["orderbook_unavailable"] = skipped.get("orderbook_unavailable", 0) + 1
            continue

        text_for_parser = m.get("_combined_text") or question
        spec = parse_market(text_for_parser, end_date_str, cities)
        if not spec:
            skipped["parser_failed"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "parser_failed",
                                          "question": question[:100]})
            continue
        if spec.confidence < 0.5:
            skipped["parser_low_confidence"] += 1
            log_event("market_skipped", {"slug": slug,
                                          "reason": "parser_low_confidence",
                                          "confidence": spec.confidence})
            continue

        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if len(token_ids) < 2:
                skipped["no_token_ids"] += 1
                continue
            token_id_yes, token_id_no = str(token_ids[0]), str(token_ids[1])
        except (json.JSONDecodeError, TypeError):
            skipped["no_token_ids"] += 1
            continue

        # HTTP — no DB lock held
        book_yes = fetch_orderbook(token_id_yes)
        book_no = fetch_orderbook(token_id_no)
        if not book_yes or not book_no:
            skipped["orderbook_unavailable"] += 1
            continue
        implied = implied_probabilities(book_yes, book_no)
        if implied["yes_ask"] is None or implied["no_ask"] is None:
            skipped["no_implied_prices"] += 1
            continue

        if not (args.min_price <= implied["yes_ask"] <= args.max_price or
                args.min_price <= implied["no_ask"] <= args.max_price):
            skipped["price_band_miss"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug, "reason": "price_band_miss",
                                              "yes_ask": implied["yes_ask"],
                                              "no_ask": implied["no_ask"]})
            continue

        forecast = fetch_forecast(spec.city, days=5)
        if not forecast:
            skipped["forecast_unavailable"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "forecast_unavailable",
                                          "city": spec.city})
            continue

        forecast_prob = forecast_probability(spec, forecast)
        if forecast_prob is None:
            skipped["no_forecast_for_target_date"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "no_forecast_for_target_date"})
            continue

        edge = compute_edge(forecast_prob, implied)
        if edge["best_side"] is None or edge["edge_pp_at_best"] < args.min_edge_pp:
            skipped["low_edge"] += 1
            log_event("market_evaluated", {
                "slug": slug, "side": edge["best_side"],
                "edge_pp": edge["edge_pp_at_best"],
                "decision": "skipped_low_edge",
                "forecast_prob": forecast_prob,
                "yes_ask": implied["yes_ask"],
                "no_ask": implied["no_ask"],
            })
            continue

        side = edge["best_side"]
        entry_price = implied["yes_ask"] if side == "YES" else implied["no_ask"]
        if not (args.min_price <= entry_price <= args.max_price):
            skipped["price_band_miss"] += 1
            continue

        candidates.append({
            "slug": slug, "question": question,
            "end_date_str": end_date_str, "side": side,
            "entry_price": entry_price, "forecast_prob": forecast_prob,
            "edge_pp": edge["edge_pp_at_best"],
            "forecast": forecast, "spec": spec, "ttr_hours": ttr_hours,
            "token_id_yes": token_id_yes, "token_id_no": token_id_no,
            "implied": implied,
            "condition_id": m.get("conditionId", ""),
        })

    # Phase 2 + 3: snapshot existing (slug, side) pairs, then insert candidates
    # one-by-one with per-row commits. Each commit releases the writer lock
    # immediately so the judge can interleave its updates between proposals.
    market_cap_usd = float(args.max_market_exposure_usd)

    with db.connect() as conn:
        # Sets of currently-relevant (slug, side) pairs and per-slug $ exposure.
        # pending  = proposed/approved/adjusted, not yet executed (wait — no
        #            duplicate while one is in flight)
        # open     = executed and NOT cashed out (counts toward exposure cap)
        pending: set[tuple[str, str]] = set()
        open_sides: set[tuple[str, str]] = set()
        exposure: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.market_slug, e.side, e.status, "
            "       COALESCE(e.size_usd, 0) AS size_usd, "
            "       c.cashout_id AS cashout_id "
            "FROM entries e "
            "LEFT JOIN cashouts c ON c.entry_id = e.entry_id"
        ):
            slug_, side_, status_, size_, co = (
                r["market_slug"], r["side"], r["status"],
                float(r["size_usd"] or 0), r["cashout_id"])
            if status_ in ("PROPOSED", "APPROVED", "ADJUSTED"):
                pending.add((slug_, side_))
            elif status_ in ("EXECUTED", "FAST_PATH") and co is None:
                open_sides.add((slug_, side_))
                exposure[slug_] = exposure.get(slug_, 0.0) + size_

        for c in candidates:
            slug = c["slug"]
            side = c["side"]
            opposite = "NO" if side == "YES" else "YES"

            # Block opposite-side entries — destructive unless true arb (not
            # detected by compute_edge). Counts open + pending opposite.
            if (slug, opposite) in open_sides or (slug, opposite) in pending:
                skipped["opposite_side_held"] += 1
                log_event("market_skipped", {
                    "slug": slug, "reason": "opposite_side_held",
                    "would_propose": side, "already_have": opposite,
                })
                continue

            # Block duplicate while a previous proposal on (slug, side) is
            # still in flight (not yet executed) — avoids wasting judge cycles
            # on a trade that will hit the exposure cap at execute time anyway.
            if (slug, side) in pending:
                skipped["duplicate_pending"] += 1
                continue

            # Allow re-entry on same (slug, side) AFTER execution as long as
            # there's room under the per-market exposure cap.
            current_exposure = exposure.get(slug, 0.0)
            if current_exposure >= market_cap_usd:
                skipped["market_exposure_full"] += 1
                log_event("market_skipped", {
                    "slug": slug, "reason": "market_exposure_full",
                    "current_exposure_usd": round(current_exposure, 2),
                    "cap_usd": market_cap_usd,
                })
                continue

            spec = c["spec"]
            entry_id = db.insert_entry(
                conn,
                ts=_now_iso(),
                market_slug=c["slug"],
                market_question=c["question"],
                condition_id=c["condition_id"],
                token_id_yes=c["token_id_yes"],
                token_id_no=c["token_id_no"],
                end_date=c["end_date_str"],
                side=c["side"],
                entry_price=c["entry_price"],
                forecast_prob_at_entry=(c["forecast_prob"] if c["side"] == "YES"
                                        else 1.0 - c["forecast_prob"]),
                implied_prob_at_entry=c["entry_price"],
                edge_pp_at_entry=c["edge_pp"],
                forecast_snapshot_json=c["forecast"],
                parser_confidence=spec.confidence,
                city_resolved=spec.city,
                threshold_value=spec.threshold_value,
                threshold_unit=spec.threshold_unit,
                comparison=spec.comparison,
                ttr_hours_at_entry=c["ttr_hours"],
                status="PROPOSED",
            )
            conn.commit()  # release writer lock so judge can interleave
            # Update in-memory state so subsequent iterations in this same
            # discovery cycle see the new proposal as already-pending.
            pending.add((slug, side))
            proposed += 1
            log_event("entry_proposed", {
                "entry_id": entry_id, "slug": c["slug"], "side": c["side"],
                "entry_price": c["entry_price"],
                "forecast_prob": c["forecast_prob"],
                "edge_pp": c["edge_pp"],
                "city": spec.city, "ttr_h": round(c["ttr_hours"], 1),
            })

    log_event("discovery_end", {"proposed": proposed,
                                 "fetched": len(raw_markets),
                                 "candidates_after_filters": len(candidates),
                                 "skipped_breakdown": skipped})
    return proposed


# ---------------------------------------------------------------------------
# Execute cycle: pick up APPROVED entries from judge, execute paper trade
# ---------------------------------------------------------------------------


def run_execute(args) -> int:
    """Pick up APPROVED entries and execute them via paper_engine.

    The weather_edge.db connection is opened briefly per-entry rather than
    held across HTTP calls, so the judge daemon can write between executions.
    """
    executed = 0

    # Read approved entries with a short-lived connection
    with db.connect() as conn:
        rows = db.query_approved_unexecuted(conn)
    if not rows:
        return 0

    # Lazy import paper_engine
    try:
        from paper_engine import PaperEngine, DEFAULT_FEE_RATE
    except ImportError as e:
        log_event("error", {"where": "execute", "err": f"paper_engine import: {e}"},
                  level="ERROR")
        return 0

    engine = PaperEngine(portfolio=args.portfolio)

    for row in rows:
        entry_id = row["entry_id"]
        status = row["status"]
        # ADJUST verdict can override side — if the judge thought the bot
        # picked the wrong direction, use the judge's adjusted_side instead.
        adjusted_side = row["judge_adjusted_side"] if status == "ADJUSTED" else None
        side = adjusted_side or row["side"]
        token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
        # HTTP — no DB lock held here
        book = fetch_orderbook(token_id)
        if not book or not book.get("asks"):
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "no_orderbook"})
            continue

        sizing = compute_max_size_for_slippage(book, "BUY",
                                               max_slippage=args.max_slippage)
        if sizing["max_shares"] == 0:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "zero_max_size"})
            continue

        # Re-validate edge at current market prices. The entry was approved
        # at proposal time T0; by the time we get here (queued behind the
        # position-limit cap, judge backlog, etc.) the market may have moved.
        # forecast_prob_at_entry is P(YES) per forecast_probability(); the
        # current implied prob for our side is sizing["avg_fill"] (volume-
        # weighted fill price for target size).
        fill_price = float(sizing["avg_fill"])
        forecast_prob = row["forecast_prob_at_entry"]
        if forecast_prob is None:
            current_edge_pp = None
        elif side == "YES":
            current_edge_pp = round(
                (float(forecast_prob) - fill_price) * 100.0, 4)
        else:
            current_edge_pp = round(
                ((1.0 - float(forecast_prob)) - fill_price) * 100.0, 4)

        min_edge_pp_for_execute = getattr(args, "execute_min_edge_pp", None)
        if min_edge_pp_for_execute is None:
            min_edge_pp_for_execute = args.min_edge_pp

        if (current_edge_pp is not None
                and current_edge_pp < min_edge_pp_for_execute):
            log_event("execute_skipped", {
                "entry_id": entry_id,
                "reason": "edge_stale",
                "original_edge_pp": row["edge_pp_at_entry"],
                "current_edge_pp": round(current_edge_pp, 2),
                "fill_price": round(fill_price, 4),
                "forecast_prob": round(float(forecast_prob), 4),
                "min_edge_pp_threshold": min_edge_pp_for_execute,
                "side": side,
            })
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "SKIPPED",
                                        skip_reason="edge_stale")
            continue

        # Per-trade size is driven by orderbook slippage cap (volume/depth)
        # and the per-market exposure cap below. The 10% portfolio cap was
        # removed at operator request — only paper-engine's internal risk
        # checks (insufficient balance) still gate at the engine layer.
        target_usd = float(sizing["max_usd"])
        # Honor judge's size cap for ADJUSTED entries — usually half or a
        # third of the full size when judge has medium confidence.
        judge_size_cap = row["judge_adjusted_size_usd"] if status == "ADJUSTED" else None
        if judge_size_cap is not None and judge_size_cap > 0:
            target_usd = min(target_usd, float(judge_size_cap))
            log_event("execute_size_adjusted", {
                "entry_id": entry_id,
                "judge_size_cap_usd": float(judge_size_cap),
                "applied_target_usd": target_usd,
                "adjusted_side": adjusted_side,
            })

        # Per-market exposure cap: sum YES+NO open positions on this slug,
        # leave at most args.max_market_exposure_usd in any single market.
        market_slug = row["market_slug"]
        with db.connect() as conn2:
            current_exposure = db.current_market_exposure_usd(conn2, market_slug)
        remaining_cap = float(args.max_market_exposure_usd) - current_exposure
        if remaining_cap <= 0:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "market_exposure_cap_reached",
                                          "market_slug": market_slug,
                                          "current_exposure_usd": round(current_exposure, 2),
                                          "cap_usd": float(args.max_market_exposure_usd)})
            continue
        if remaining_cap < target_usd:
            log_event("execute_market_exposure_clamp", {
                "entry_id": entry_id, "market_slug": market_slug,
                "original_target_usd": round(target_usd, 2),
                "current_exposure_usd": round(current_exposure, 2),
                "cap_usd": float(args.max_market_exposure_usd),
                "clamped_target_usd": round(remaining_cap, 2),
            })
            target_usd = remaining_cap

        if target_usd < 10:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "size_below_min_$10"})
            continue

        if args.dry_run:
            log_event("execute_dry_run", {"entry_id": entry_id,
                                          "side": side, "target_usd": target_usd,
                                          "avg_fill": sizing["avg_fill"],
                                          "slippage_pct": sizing["slippage_pct"]})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "EXECUTED",
                                       size_usd=target_usd,
                                       size_shares=sizing["max_shares"],
                                       entry_price=sizing["avg_fill"])
            executed += 1
            continue

        # Real paper execution via PaperEngine — touches portfolio.db, not weather_edge.db
        try:
            result = engine.open_position(
                token_id=token_id,
                side=side,
                size_usd=target_usd,
                market_question=row["market_question"][:200],
                fee_rate=DEFAULT_FEE_RATE,
                confidence=0.65,
                reasoning=f"weather_edge_bot entry_id={entry_id}",
            )
            # Brief weather_edge.db connection just for the status update
            with db.connect() as conn2:
                if result.get("status") == "executed":
                    db.update_entry_status(conn2, entry_id, "EXECUTED",
                                           size_usd=result.get("cost_usd"),
                                           size_shares=result.get("shares_filled"),
                                           entry_price=result.get("avg_price"))
                    log_event("entry_executed", {"entry_id": entry_id,
                                                 "shares": result.get("shares_filled"),
                                                 "avg_price": result.get("avg_price")})
                    executed += 1
                else:
                    log_event("execute_rejected", {"entry_id": entry_id,
                                                   "reason": result.get("reason")})
                    db.update_entry_status(conn2, entry_id, "SKIPPED",
                                           skip_reason=str(result.get("reason"))[:200])
        except Exception as e:
            log_event("error", {"where": "open_position",
                                "entry_id": entry_id, "err": str(e)})

    return executed


# ---------------------------------------------------------------------------
# Monitor cycle: per-position adaptive cashout check
# ---------------------------------------------------------------------------


def _ttr_hours(end_date_str: str) -> float:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0


def _monitor_interval_for_ttr(ttr_h: float) -> int:
    if ttr_h < 24:
        return 30 * 60   # 30 min
    return 60 * 60       # 60 min


_last_monitor_per_entry: dict[int, float] = {}


def run_monitor_tick(args, cities: dict) -> None:
    """Check each open position; if its adaptive interval elapsed, do a check.

    Reads the open-position list with a short-lived connection, then iterates
    without holding any DB lock. Each _do_monitor_check opens its own brief
    connection only when it needs to write — letting the judge daemon write
    in the gaps.
    """
    now_mono = time.monotonic()
    with db.connect() as conn:
        rows = db.query_open_positions(conn)
        # Materialize the rows now; the connection will be closed below.
        rows = [dict(r) for r in rows]

    for row in rows:
        entry_id = row["entry_id"]
        ttr_h = _ttr_hours(row["end_date"] or "")
        interval = _monitor_interval_for_ttr(ttr_h)
        last = _last_monitor_per_entry.get(entry_id, 0)
        if now_mono - last < interval:
            continue
        _last_monitor_per_entry[entry_id] = now_mono
        _do_monitor_check(None, row, cities, args)


def _do_monitor_check(conn, row, cities: dict, args) -> None:
    """Run one monitor check for an entry. The `conn` arg is ignored —
    HTTP calls happen first without holding any DB lock, then a brief
    connection is opened for the writes at the end."""
    entry_id = row["entry_id"]
    side = row["side"]
    spec = parse_market(row["market_question"], row["end_date"], cities)
    # HTTP — no DB lock
    forecast = fetch_forecast(spec.city) if spec else None
    if not spec or not forecast:
        log_event("monitor_check", {"entry_id": entry_id, "decision": "HOLD",
                                    "reason": "no_forecast_or_spec"})
        return
    forecast_prob_yes = forecast_probability(spec, forecast)
    if forecast_prob_yes is None:
        return

    token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
    book = fetch_orderbook(token_id)  # HTTP
    if not book:
        return
    bid = book["bids"][0]["price"] if book.get("bids") else 0.0
    ask = book["asks"][0]["price"] if book.get("asks") else 0.0
    entry_price = float(row["entry_price"])

    prev_peak = float(row["peak_bid_seen"] or 0.0)
    peak = max(prev_peak, bid)

    verdict = evaluate_cashout_triggers(
        side=side,
        entry_price=entry_price,
        current_bid=bid,
        peak_bid_seen=peak,
        forecast_prob_yes=forecast_prob_yes,
        profit_lock_pp=args.profit_lock_pp,
        trailing_drawdown_pct=args.trailing_drawdown_pct,
        convergence_pp=args.convergence_pp,
    )
    decision = verdict["decision"]
    trigger = verdict["trigger"]
    reason = f"{trigger}: {verdict['reason']}"
    forecast_prob_now = forecast_prob_yes if side == "YES" else 1.0 - forecast_prob_yes

    # Brief write-only connection
    with db.connect() as conn2:
        if bid > prev_peak:
            conn2.execute(
                "UPDATE entries SET peak_bid_seen = ?, peak_bid_seen_at = ? "
                "WHERE entry_id = ?",
                (bid, _now_iso(), entry_id),
            )
        db.insert_monitor_check(
            conn2, entry_id=entry_id,
            ts=_now_iso(),
            forecast_prob_now=forecast_prob_now,
            forecast_snapshot_json=forecast,
            market_best_bid=bid,
            market_best_ask=ask,
            decision=decision,
            decision_reason=reason,
        )
    log_event("monitor_check", {"entry_id": entry_id, "decision": decision,
                                "trigger": trigger,
                                "forecast_prob_now": forecast_prob_now,
                                "entry_price": entry_price,
                                "bid": bid, "peak": peak,
                                "reason": verdict["reason"]})

    if decision == "CASHOUT":
        _do_cashout(None, row, bid, forecast, forecast_prob_now, args, reason)


def _do_cashout(conn, row, bid: float, forecast: dict,
                forecast_prob_now: float, args, reason: str) -> None:
    entry_id = row["entry_id"]
    side = row["side"]
    token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]

    if args.dry_run:
        log_event("cashout_dry_run", {"entry_id": entry_id, "bid": bid})
        return

    try:
        from paper_engine import PaperEngine
        engine = PaperEngine(portfolio=args.portfolio)
        result = engine.close_position(token_id=token_id, side=side,
                                        reasoning=f"weather_edge_bot: {reason}")
        if result.get("status") == "closed":
            exit_price = result.get("avg_sell_price") or result.get("avg_price")
            with db.connect() as conn2:
                db.insert_cashout(
                    conn2, entry_id=entry_id,
                    ts=_now_iso(),
                    exit_price=exit_price,
                    exit_shares=result.get("shares_sold"),
                    realized_pnl_usd=result.get("realized_pnl"),
                    forecast_prob_at_exit=forecast_prob_now,
                    forecast_snapshot_json=forecast,
                    reason=reason[:200],
                )
            log_event("cashout_executed", {"entry_id": entry_id,
                                            "exit_price": exit_price,
                                            "pnl": result.get("realized_pnl")})
        else:
            log_event("cashout_rejected", {"entry_id": entry_id,
                                            "reason": result.get("reason")})
    except Exception as e:
        log_event("error", {"where": "cashout", "entry_id": entry_id, "err": str(e)})


# ---------------------------------------------------------------------------
# Resolution sweep
# ---------------------------------------------------------------------------


def run_resolution_sweep() -> int:
    """For each EXECUTED position past end_date, fetch outcomePrices and persist.
    On resolution, also close the paper-portfolio position at payout so the
    slot is freed for new bets."""
    resolved = 0
    try:
        import paper_engine
    except ImportError as e:
        log_event("error", {"where": "resolution_sweep",
                            "err": f"paper_engine import: {e}"},
                  level="ERROR")
        return 0
    with db.connect() as conn:
        rows = db.query_unresolved_past_end(conn, _now_iso())
        for row in rows:
            slug = row["market_slug"]
            try:
                r = requests.get(f"{GAMMA_API}/markets",
                                 params={"slug": slug}, timeout=15)
                r.raise_for_status()
                results = r.json()
                if not isinstance(results, list) or not results:
                    continue
                m = results[0]
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
                if not outcomes or not prices or len(outcomes) != len(prices):
                    continue
                # YES is index 0, NO is index 1 (Polymarket convention)
                final_outcome = "YES" if prices[0] >= 0.99 else \
                                "NO" if prices[1] >= 0.99 else "VOID"
                payout = 1.0 if (final_outcome == row["side"]) else 0.0
                if final_outcome == "VOID":
                    payout = float(row["entry_price"] or 0)  # neutral
                db.insert_resolution(
                    conn, entry_id=row["entry_id"],
                    ts_resolved=_now_iso(),
                    final_outcome=final_outcome,
                    payout_per_share=payout,
                )
                resolved += 1
                log_event("resolution_observed", {"entry_id": row["entry_id"],
                                                   "slug": slug,
                                                   "outcome": final_outcome,
                                                   "payout": payout})

                # Close the corresponding paper-portfolio position at the
                # resolution payout. This credits proceeds + frees the
                # max_concurrent_positions slot. If the position was already
                # cashed out earlier (no paper position open), skip silently.
                token_id = (row["token_id_yes"] if row["side"] == "YES"
                            else row["token_id_no"])
                if token_id:
                    try:
                        close_result = paper_engine.close_position(
                            token_id=token_id, side=row["side"],
                            reasoning=f"resolution:{final_outcome}",
                            force_exit_price=payout,
                        )
                        log_event("resolution_closed", {
                            "entry_id": row["entry_id"],
                            "token_id": token_id,
                            "side": row["side"],
                            "payout": payout,
                            "realized_pnl": close_result.get("realized_pnl"),
                            "new_balance": close_result.get("new_balance"),
                        })
                    except RuntimeError as ce:
                        # Most common: "No open position for token..."
                        # Means the position was already closed via cashout
                        # before resolution. Not an error.
                        log_event("resolution_close_skipped", {
                            "entry_id": row["entry_id"],
                            "reason": str(ce),
                        })
                    except Exception as ce:
                        log_event("error", {
                            "where": "resolution_close_position",
                            "entry_id": row["entry_id"], "err": str(ce),
                        }, level="WARN")
            except Exception as e:
                log_event("error", {"where": "resolution_sweep",
                                    "slug": slug, "err": str(e)}, level="WARN")
    return resolved


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------


_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    log_event("shutdown_signal", {"signal": signum})
    _shutdown = True


def _check_portfolio_thresholds() -> None:
    """v6 Tier 4A: emit notifications when portfolio crosses risk thresholds.

    Lazy-imports the dashboard's portfolio service + notifier so this is a
    no-op when neither is available (e.g. during pure-bot deploys without
    the dashboard installed).

    Thresholds (CLAUDE.md §2):
      - drawdown > 10% → warning
      - drawdown > 15% → critical
      - daily realized loss > 3% of starting → warning
      - daily realized loss > 5% of starting → critical
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from dashboard.services import portfolio as pf_svc, notifier
    except ImportError:
        return  # dashboard not on this host, skip silently

    if not notifier.is_configured():
        return

    try:
        kpis = pf_svc.get_kpis()
    except Exception:
        return

    dd = kpis.get("drawdown_pct_from_peak", 0)
    starting = kpis.get("starting_balance_usd", 0) or 0
    realized = kpis.get("realized_pnl_today_usd", 0) or 0
    daily_loss_pct = -(realized / starting * 100) if starting > 0 else 0
    total = kpis.get("portfolio_total_usd", 0)

    body_template = (
        f"Portfolio: ${total:.2f}\n"
        f"Drawdown from peak: {dd:.2f}%\n"
        f"Realized today: ${realized:+.2f} ({-daily_loss_pct:+.2f}%)"
    )

    if dd <= -15:
        notifier.send("critical", "Drawdown CRITICAL > 15%", body_template,
                       rate_limit_key="dd_critical")
    elif dd <= -10:
        notifier.send("warning", "Drawdown warning > 10%", body_template,
                       rate_limit_key="dd_warning")

    if daily_loss_pct >= 5:
        notifier.send("critical",
                       "Daily loss CRITICAL > 5% — entries blocked",
                       body_template, rate_limit_key="dl_critical")
    elif daily_loss_pct >= 3:
        notifier.send("warning", "Daily loss warning > 3%",
                       body_template, rate_limit_key="dl_warning")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--min-edge-pp", type=float, default=10.0)
    p.add_argument("--execute-min-edge-pp", type=float, default=None,
                   help="Minimum edge_pp required at EXECUTION time. "
                        "Defaults to --min-edge-pp (same threshold as "
                        "discovery). Set higher to enforce stricter "
                        "re-check on queued entries that may have aged "
                        "while waiting on position-limit or judge backlog.")
    p.add_argument("--min-volume", type=float, default=100,
                   help="Min market USD volume; sub-bracket markets have low volume each")
    p.add_argument("--min-price", type=float, default=0.05,
                   help="Min entry-side price (0.05 default; tail brackets often "
                        "have biggest payout asymmetry and biggest edge)")
    p.add_argument("--max-price", type=float, default=0.95,
                   help="Max entry-side price (0.95 default)")
    p.add_argument("--max-slippage", type=float, default=0.20)
    p.add_argument("--window-hours", type=float, default=48)
    p.add_argument("--daemon", action="store_true", default=True)
    p.add_argument("--once", action="store_true",
                   help="Run discovery+monitor once and exit")
    p.add_argument("--discovery-interval-min", type=float, default=60,
                   help="Minutes between discovery cycles (default 60). "
                        "Discovery scans ~1249 markets which takes 5-15 min; "
                        "10 min was too tight and starved the judge of DB writes.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--judge-mode", choices=("sync", "off"), default="sync")
    p.add_argument("--fast-path-ttr-min", type=int, default=60)
    p.add_argument("--profit-lock-pp", type=float, default=50.0,
                   help="Cashout when bid >= entry + X pp (default 50pp = +$0.50)")
    p.add_argument("--trailing-drawdown-pct", type=float, default=15.0,
                   help="Cashout if bid falls X%% below peak (default 15%% — "
                        "was 30%% but operator analysis 2026-05-15 showed "
                        "84%% of trades held to resolution where they lost)")
    p.add_argument("--convergence-pp", type=float, default=5.0,
                   help="Cashout when bid within X pp of forecast fair value (default 5pp)")
    p.add_argument("--max-market-exposure-usd", type=float, default=50.0,
                   help="Total $ exposure cap per market_slug (YES+NO summed, "
                        "across all open positions). Default $50.")
    p.add_argument("--portfolio", default="default")
    p.add_argument("--log-file", default=None,
                   help="Write JSONL log here (default ~/.polymarket-paper/weather_edge.jsonl)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    # Override log file if user asked
    global LOG_FILE
    if args.log_file:
        LOG_FILE = Path(args.log_file).resolve()
        # If user gave a relative path, make it absolute and ensure parent exists
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on --once for a clean per-run file
        if args.once:
            LOG_FILE.write_text("")
        print(f"Logging JSONL to: {LOG_FILE}", flush=True)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    db.init_db()
    # Write PID file so the dashboard can find + restart us on apply.
    # Atomic write (tmp → rename) + best-effort cleanup at shutdown.
    pid_file = Path.home() / ".polymarket-paper" / "bot.pid.json"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "argv": [sys.executable] + sys.argv,
            "cwd": str(Path.cwd()),
            "started_at": _now_iso(),
        }))
        tmp.replace(pid_file)
    except OSError as e:
        log_event("warn", {"where": "pidfile_write", "err": str(e)}, level="WARN")

    cities = load_cities()
    log_event("startup", {"args": vars(args), "cities_loaded": len(cities.get("us_top50", [])) +
                          len(cities.get("world", [])) + len(cities.get("europe_top30", [])) +
                          len(cities.get("north_america_extra", []))})

    if args.once:
        run_discovery(args, cities)
        if args.judge_mode == "off":
            run_execute(args)
        run_monitor_tick(args, cities)
        run_resolution_sweep()
        try:
            size = LOG_FILE.stat().st_size
            print(f"\n=== Full log: {LOG_FILE} ({size:,} bytes) ===", flush=True)
        except OSError:
            pass
        return

    discovery_int = args.discovery_interval_min * 60
    last_discovery = 0.0
    last_heartbeat = 0.0
    last_resolution = 0.0
    last_execute = 0.0

    while not _shutdown:
        now_mono = time.monotonic()
        try:
            if now_mono - last_discovery >= discovery_int:
                run_discovery(args, cities)
                last_discovery = now_mono
            if now_mono - last_execute >= EXECUTE_INTERVAL:
                # Bot only auto-executes if judge is off OR judge already approved.
                # In sync mode, query_approved_unexecuted picks up only judge-vetted.
                run_execute(args)
                last_execute = now_mono
            run_monitor_tick(args, cities)
            if now_mono - last_resolution >= RESOLUTION_SWEEP_INTERVAL:
                run_resolution_sweep()
                last_resolution = now_mono
            if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL:
                with db.connect() as conn:
                    open_count = len(db.query_open_positions(conn))
                    pending_count = len(db.query_pending_proposals(conn))
                log_event("heartbeat", {"open_positions": open_count,
                                         "pending_proposals": pending_count})
                last_heartbeat = now_mono
                # v6 Tier 4A: portfolio threshold checks for notifications.
                # Wrapped in try so notifier failures never crash the bot.
                try:
                    _check_portfolio_thresholds()
                except Exception as e:
                    log_event("error", {"where": "notify_thresholds",
                                         "err": str(e)}, level="WARN")
        except Exception as e:
            log_event("error", {"where": "main_loop", "err": str(e),
                                "type": type(e).__name__}, level="ERROR")

        time.sleep(MONITOR_TICK)

    log_event("shutdown_clean", {})
    try:
        pid_file.unlink(missing_ok=True)
    except (NameError, OSError):
        pass


if __name__ == "__main__":
    main()
