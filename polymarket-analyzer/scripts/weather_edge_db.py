"""SQLite persistence for the weather edge bot.

Schema versioned via PRAGMA user_version. Migrations are idempotent — calling
init_db() on an existing DB is safe and brings it up to the current version.

Tables: entries, monitor_checks, cashouts, resolutions, counterfactuals,
judge_reviews. See plan in /root/.claude/plans/ for column definitions.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path.home() / ".polymarket-paper" / "weather_edge.db"
SCHEMA_VERSION = 9


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS entries (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  market_question TEXT NOT NULL,
  condition_id TEXT,
  token_id_yes TEXT,
  token_id_no TEXT,
  end_date TEXT,
  side TEXT CHECK(side IN ('YES','NO','SKIP')),
  entry_price REAL,
  size_shares REAL,
  size_usd REAL,
  forecast_prob_at_entry REAL,
  implied_prob_at_entry REAL,
  edge_pp_at_entry REAL,
  forecast_snapshot_json TEXT,
  parser_confidence REAL,
  city_resolved TEXT,
  threshold_value REAL,
  threshold_unit TEXT,
  comparison TEXT,
  ttr_hours_at_entry REAL,
  skip_reason TEXT,
  status TEXT NOT NULL DEFAULT 'PROPOSED'
    CHECK(status IN ('PROPOSED','APPROVED','REJECTED','ADJUSTED','EXECUTED','SKIPPED','FAST_PATH')),
  judge_skipped_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries(ts);
CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_market ON entries(market_slug);

CREATE TABLE IF NOT EXISTS monitor_checks (
  check_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  ts TEXT NOT NULL,
  forecast_prob_now REAL,
  forecast_snapshot_json TEXT,
  market_best_bid REAL,
  market_best_ask REAL,
  decision TEXT CHECK(decision IN ('HOLD','CASHOUT','TRY_CASHOUT_BLOCKED')),
  decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_entry ON monitor_checks(entry_id);

CREATE TABLE IF NOT EXISTS cashouts (
  cashout_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  ts TEXT NOT NULL,
  exit_price REAL,
  exit_shares REAL,
  realized_pnl_usd REAL,
  forecast_prob_at_exit REAL,
  forecast_snapshot_json TEXT,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_cashouts_entry ON cashouts(entry_id);

CREATE TABLE IF NOT EXISTS resolutions (
  resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  ts_resolved TEXT NOT NULL,
  final_outcome TEXT CHECK(final_outcome IN ('YES','NO','VOID')),
  payout_per_share REAL,
  observed_value REAL
);
CREATE INDEX IF NOT EXISTS idx_resolutions_entry ON resolutions(entry_id);

CREATE TABLE IF NOT EXISTS counterfactuals (
  counterfactual_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER UNIQUE NOT NULL REFERENCES entries(entry_id),
  cashout_id INTEGER REFERENCES cashouts(cashout_id),
  realized_pnl REAL,
  hypothetical_hold_pnl REAL,
  delta REAL,
  computed_at TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS judge_reviews (
  review_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  ts TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('APPROVE','REJECT','ADJUST')),
  confidence REAL,
  judge_prob REAL,
  bot_prob REAL,
  prob_delta REAL,
  rationale TEXT,
  evidence_json TEXT,
  adjusted_side TEXT,
  adjusted_size_usd REAL,
  llm_model TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cache_read_tokens INTEGER,
  cost_usd REAL,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_judge_entry ON judge_reviews(entry_id);
CREATE INDEX IF NOT EXISTS idx_judge_verdict ON judge_reviews(verdict);
"""


SCHEMA_V2_MIGRATIONS = [
    # Peak bid tracking for trailing-stop cashout policy.
    "ALTER TABLE entries ADD COLUMN peak_bid_seen REAL",
    "ALTER TABLE entries ADD COLUMN peak_bid_seen_at TEXT",
]


SCHEMA_V3_MIGRATIONS = [
    # Strategy advisor run history.
    """
    CREATE TABLE IF NOT EXISTS advisor_runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      trigger TEXT NOT NULL,
      since_iso TEXT NOT NULL,
      report_path TEXT NOT NULL,
      json_path TEXT NOT NULL,
      n_suggestions INTEGER NOT NULL,
      llm_model TEXT NOT NULL,
      cost_usd REAL,
      tokens_in INTEGER,
      tokens_out INTEGER,
      cache_read_tokens INTEGER,
      status TEXT NOT NULL,
      error_msg TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_advisor_ts ON advisor_runs(ts)",
]


SCHEMA_V4_MIGRATIONS = [
    # Audit trail for advisor suggestions applied via the dashboard.
    """
    CREATE TABLE IF NOT EXISTS advisor_suggestion_applies (
      apply_id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id INTEGER NOT NULL REFERENCES advisor_runs(run_id),
      suggestion_id TEXT NOT NULL,
      ts TEXT NOT NULL,
      category TEXT NOT NULL,
      param_path TEXT NOT NULL,
      previous_value TEXT,
      applied_value TEXT,
      git_commit_sha TEXT,
      status TEXT NOT NULL CHECK(status IN ('applied','failed','unsupported','reverted')),
      error_msg TEXT,
      UNIQUE(run_id, suggestion_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_apply_run ON advisor_suggestion_applies(run_id)",
]


SCHEMA_V5_MIGRATIONS = [
    # Async advisor jobs spawned from the dashboard "Run Advisor Now" UI.
    """
    CREATE TABLE IF NOT EXISTS advisor_jobs (
      job_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_started TEXT NOT NULL,
      ts_finished TEXT,
      trigger TEXT NOT NULL,
      since_days INTEGER NOT NULL,
      per_trade_limit INTEGER,
      status TEXT NOT NULL CHECK(status IN
          ('pending','running','done','failed','cancelled')),
      pid INTEGER,
      exit_code INTEGER,
      resulting_run_id INTEGER,
      log_path TEXT,
      error_msg TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_status ON advisor_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_job_ts ON advisor_jobs(ts_started)",
]


# Schema v6: persist the full input context the judge saw + the raw
# LLM response. This is what the advisor needs to diagnose judge
# hallucination — without it we only have a 1500-char truncated rationale.
SCHEMA_V6_MIGRATIONS = [
    "ALTER TABLE judge_reviews ADD COLUMN input_context_json TEXT",
    "ALTER TABLE judge_reviews ADD COLUMN raw_response_json TEXT",
]


# Schema v7: per-(city, target_date, metric, source) forecast snapshots.
# Used by the bot's discovery loop to (1) compute dynamic MAE from
# forecast volatility history, (2) cross-check OpenWeather vs Visual
# Crossing during proposal — instead of waiting for the judge to filter.
# See plan in /root/.claude/plans/quero-configurar-para-que-rustling-dragonfly.md
SCHEMA_V7_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS forecast_history (
      history_id INTEGER PRIMARY KEY AUTOINCREMENT,
      city TEXT NOT NULL,
      target_date TEXT NOT NULL,
      metric TEXT NOT NULL,
      source TEXT NOT NULL,
      predicted_value REAL NOT NULL,
      ts TEXT NOT NULL,
      UNIQUE(city, target_date, metric, source, ts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fh_lookup "
    "ON forecast_history(city, target_date, metric)",
]


# Schema v8: observability for v7+v8+v9 features. entries gains a
# `discovery_meta_json` column carrying the mae_meta dict captured at
# proposal time (mae_dynamic, bias, station code, OW/VC/Open-Meteo
# values, om_spread_penalty etc). discovery_skips persists per-market
# skip events that previously only incremented in-memory counters
# (ttr_below_min, outside_window, etc) so the advisor can analyze
# WHY proposals were filtered, not just count them.
SCHEMA_V8_MIGRATIONS = [
    "ALTER TABLE entries ADD COLUMN discovery_meta_json TEXT",
    """
    CREATE TABLE IF NOT EXISTS discovery_skips (
      skip_id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      slug TEXT,
      city TEXT,
      reason TEXT NOT NULL,
      meta_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ds_ts ON discovery_skips(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ds_reason ON discovery_skips(reason)",
]

# v9: 3-bin laddering. A ladder is a group of 1-3 entries sharing the same
# event_slug from Gamma /events. Atomic execution + atomic cashout
# coordinated by ladder_group_id.
SCHEMA_V9_MIGRATIONS = [
    "ALTER TABLE entries ADD COLUMN ladder_group_id TEXT",
    "ALTER TABLE entries ADD COLUMN ladder_position TEXT",
    "ALTER TABLE entries ADD COLUMN ladder_event_slug TEXT",
    # Target stake for this leg (per Kelly proportional split at discovery
    # time). Executor honors this as size cap, applying slippage limits
    # and market exposure cap on top. NULL on legacy single-bin entries.
    "ALTER TABLE entries ADD COLUMN ladder_stake_usd REAL",
    "CREATE INDEX IF NOT EXISTS idx_entries_ladder_group ON entries(ladder_group_id)",
]


def init_db(path: Path = DB_PATH) -> None:
    """Create the DB and tables if missing. Idempotent. Bumps user_version.
    Enables WAL mode so readers + writers don't block each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=5.0) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.execute("PRAGMA journal_mode = WAL")
        current = cur.execute("PRAGMA user_version").fetchone()[0]
        if current < 1:
            cur.executescript(SCHEMA_V1)
            current = 1
        if current < 2:
            for stmt in SCHEMA_V2_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    # Column may already exist if a partial migration happened.
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 2
        if current < 3:
            for stmt in SCHEMA_V3_MIGRATIONS:
                cur.execute(stmt)
            current = 3
        if current < 4:
            for stmt in SCHEMA_V4_MIGRATIONS:
                cur.execute(stmt)
            current = 4
        if current < 5:
            for stmt in SCHEMA_V5_MIGRATIONS:
                cur.execute(stmt)
            current = 5
        if current < 6:
            for stmt in SCHEMA_V6_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    # Column may already exist if a partial migration happened.
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 6
        if current < 7:
            for stmt in SCHEMA_V7_MIGRATIONS:
                cur.execute(stmt)
            current = 7
        if current < 8:
            for stmt in SCHEMA_V8_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    # ALTER TABLE ADD COLUMN tolerates "already exists" in
                    # case of a partially-applied migration.
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 8
        if current < 9:
            for stmt in SCHEMA_V9_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 9
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


@contextmanager
def connect(path: Path = DB_PATH):
    """Context manager yielding a sqlite3 connection with foreign_keys ON,
    WAL mode, busy_timeout, and Row factory. WAL lets readers and writers
    coexist without blocking each other; busy_timeout retries the lock
    for up to 5s instead of failing immediately."""
    init_db(path)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def insert_entry(conn, **kwargs) -> int:
    """Insert a row into entries. Returns entry_id.

    Required: ts, market_slug, market_question. Other fields optional.
    JSON-serializable fields (forecast_snapshot_json) are dumped if dict/list.
    """
    if isinstance(kwargs.get("forecast_snapshot_json"), (dict, list)):
        kwargs["forecast_snapshot_json"] = json.dumps(kwargs["forecast_snapshot_json"])
    # v8: same treatment for discovery_meta_json (mae_meta from bot)
    if isinstance(kwargs.get("discovery_meta_json"), (dict, list)):
        kwargs["discovery_meta_json"] = json.dumps(kwargs["discovery_meta_json"])
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO entries ({','.join(cols)}) VALUES ({placeholders})"
    cur = conn.execute(q, vals)
    return cur.lastrowid


def insert_forecast_history(conn, **kwargs) -> Optional[int]:
    """v7: store one (city, target_date, metric, source, ts) snapshot.
    UNIQUE(city, target_date, metric, source, ts) → silent no-op on dupes.
    Returns last_insert_rowid or None if dupe was rejected."""
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = (f"INSERT OR IGNORE INTO forecast_history "
         f"({','.join(cols)}) VALUES ({placeholders})")
    cur = conn.execute(q, vals)
    return cur.lastrowid if cur.rowcount else None


def query_forecast_history(conn, city: str, target_date: str,
                            metric: str, limit: int = 5) -> list[sqlite3.Row]:
    """v7: most recent N forecast snapshots for (city, target_date, metric).
    Used to compute dynamic MAE from forecast volatility."""
    return conn.execute(
        "SELECT predicted_value, source, ts FROM forecast_history "
        "WHERE city = ? AND target_date = ? AND metric = ? "
        "ORDER BY ts DESC LIMIT ?",
        (city, target_date, metric, limit),
    ).fetchall()


def insert_discovery_skip(conn, **kwargs) -> int:
    """v8: persist a discovery-phase skip event (e.g. ttr_below_min,
    outside_window) so the advisor can analyze why proposals were
    filtered, not just count them. Auto-serializes dict meta_json."""
    if isinstance(kwargs.get("meta_json"), (dict, list)):
        kwargs["meta_json"] = json.dumps(kwargs["meta_json"])
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = (f"INSERT INTO discovery_skips ({','.join(cols)}) "
         f"VALUES ({placeholders})")
    return conn.execute(q, vals).lastrowid


def insert_monitor_check(conn, entry_id: int, **kwargs) -> int:
    if isinstance(kwargs.get("forecast_snapshot_json"), (dict, list)):
        kwargs["forecast_snapshot_json"] = json.dumps(kwargs["forecast_snapshot_json"])
    kwargs["entry_id"] = entry_id
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO monitor_checks ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def insert_cashout(conn, entry_id: int, **kwargs) -> int:
    if isinstance(kwargs.get("forecast_snapshot_json"), (dict, list)):
        kwargs["forecast_snapshot_json"] = json.dumps(kwargs["forecast_snapshot_json"])
    kwargs["entry_id"] = entry_id
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO cashouts ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def insert_resolution(conn, entry_id: int, **kwargs) -> int:
    kwargs["entry_id"] = entry_id
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO resolutions ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def insert_judge_review(conn, entry_id: int, **kwargs) -> int:
    if isinstance(kwargs.get("evidence_json"), (dict, list)):
        kwargs["evidence_json"] = json.dumps(kwargs["evidence_json"])
    kwargs["entry_id"] = entry_id
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO judge_reviews ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def insert_advisor_run(conn, **kwargs) -> int:
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO advisor_runs ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def insert_suggestion_apply(conn, **kwargs) -> int:
    """Record an attempt to apply an advisor suggestion. UNIQUE constraint
    on (run_id, suggestion_id) means this raises on duplicate."""
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = (f"INSERT INTO advisor_suggestion_applies ({','.join(cols)}) "
         f"VALUES ({placeholders})")
    return conn.execute(q, vals).lastrowid


def query_applies_for_run(conn, run_id: int) -> list[sqlite3.Row]:
    """Return all prior apply attempts for a given advisor run."""
    return conn.execute(
        "SELECT * FROM advisor_suggestion_applies "
        "WHERE run_id = ? ORDER BY ts ASC",
        (run_id,),
    ).fetchall()


def insert_advisor_job(conn, **kwargs) -> int:
    """Insert a new advisor job row (status usually 'pending' on creation).
    Returns the new job_id."""
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = f"INSERT INTO advisor_jobs ({','.join(cols)}) VALUES ({placeholders})"
    return conn.execute(q, vals).lastrowid


def update_advisor_job(conn, job_id: int, **fields) -> int:
    """Patch fields on an advisor_jobs row. Returns rows affected."""
    if not fields:
        return 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    q = f"UPDATE advisor_jobs SET {set_clause} WHERE job_id = ?"
    return conn.execute(q, vals).rowcount


def get_advisor_job(conn, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM advisor_jobs WHERE job_id = ?", (job_id,),
    ).fetchone()


def upsert_counterfactual(conn, entry_id: int, **kwargs) -> int:
    kwargs["entry_id"] = entry_id
    cols = list(kwargs.keys())
    vals = [kwargs[c] for c in cols]
    placeholders = ",".join("?" * len(cols))
    q = (
        f"INSERT INTO counterfactuals ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(entry_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "entry_id")
    )
    return conn.execute(q, vals).lastrowid


def update_entry_status(conn, entry_id: int, status: str, **extras) -> None:
    sets = ["status = ?"]
    vals: list[Any] = [status]
    for k, v in extras.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(entry_id)
    conn.execute(f"UPDATE entries SET {', '.join(sets)} WHERE entry_id = ?", vals)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def query_pending_proposals(conn, limit: int = 50) -> list[sqlite3.Row]:
    """Entries awaiting judge review, oldest first by ttr."""
    return conn.execute(
        "SELECT * FROM entries WHERE status = 'PROPOSED' "
        "ORDER BY ttr_hours_at_entry ASC, ts ASC LIMIT ?",
        (limit,),
    ).fetchall()


def query_ladder_group(conn, ladder_group_id: str) -> list[sqlite3.Row]:
    """v9: all entries belonging to one ladder group, ordered by ladder
    position (central, below, above). Used by atomic executor and atomic
    cashout to coordinate across legs.

    v9.11 (2026-05-24): joins judge_reviews to expose judge_adjusted_side
    and judge_adjusted_size_usd. Atomic executor needs these for ADJUSTED
    legs (mirrors query_approved_unexecuted shape). Without the JOIN,
    accessing leg["judge_adjusted_side"] raised IndexError every 60s
    starting 2026-05-22 23:00, blocking monitor and cashout for 15h+.
    """
    return conn.execute(
        "SELECT e.*, "
        "       j.adjusted_size_usd AS judge_adjusted_size_usd, "
        "       j.adjusted_side     AS judge_adjusted_side "
        "FROM entries e "
        "LEFT JOIN judge_reviews j ON j.entry_id = e.entry_id "
        "WHERE e.ladder_group_id = ? "
        "ORDER BY CASE e.ladder_position WHEN 'central' THEN 0 "
        "WHEN 'below' THEN 1 WHEN 'above' THEN 2 ELSE 3 END, e.entry_id",
        (ladder_group_id,),
    ).fetchall()


def query_approved_unexecuted(conn) -> list[sqlite3.Row]:
    """Entries APPROVED or ADJUSTED but not yet executed (bot picks these up).

    Pulls in the latest judge review's adjusted_size_usd / adjusted_side so
    the executor can honor judge's size cap when status='ADJUSTED'.
    """
    return conn.execute(
        "SELECT e.*, "
        "       j.adjusted_size_usd AS judge_adjusted_size_usd, "
        "       j.adjusted_side     AS judge_adjusted_side "
        "FROM entries e "
        "LEFT JOIN judge_reviews j ON j.entry_id = e.entry_id "
        "WHERE e.status IN ('APPROVED','ADJUSTED') "
        "ORDER BY e.ts ASC"
    ).fetchall()


def query_open_positions(conn) -> list[sqlite3.Row]:
    """Entries that were executed and are still open: no cashout, no resolution.
    Resolved positions are excluded so the slot is freed for new bets."""
    return conn.execute(
        "SELECT e.* FROM entries e "
        "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.status IN ('EXECUTED','FAST_PATH') "
        "AND c.cashout_id IS NULL "
        "AND r.resolution_id IS NULL"
    ).fetchall()


def query_unresolved_past_end(conn, now_iso: str) -> list[sqlite3.Row]:
    """Entries whose end_date < now and no resolution row yet."""
    return conn.execute(
        "SELECT e.* FROM entries e "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.status IN ('EXECUTED','FAST_PATH') "
        "AND e.end_date IS NOT NULL AND e.end_date < ? "
        "AND r.resolution_id IS NULL",
        (now_iso,),
    ).fetchall()


def query_per_trade_details(conn, since_iso: str,
                             limit: int = 200) -> list[sqlite3.Row]:
    """One row per executed entry in the time window, joining all related
    tables and pulling the final monitor_check's decision_reason so the
    caller can classify the exit_strategy (profit_lock / trailing_stop /
    convergence / forecast_reversal / hold_to_resolution / still_open).

    Used by weather_strategy_advisor to feed per-trade data to Claude for
    winner/loser pattern analysis.
    """
    return conn.execute(
        "SELECT "
        "  e.entry_id, e.ts, e.city_resolved, e.side, e.entry_price, "
        "  e.size_usd, e.size_shares, e.edge_pp_at_entry, "
        "  e.ttr_hours_at_entry, e.forecast_prob_at_entry, "
        "  e.parser_confidence, e.status, e.market_slug, "
        "  j.verdict AS judge_verdict, j.judge_prob, "
        "  j.confidence AS judge_confidence, "
        "  SUBSTR(j.rationale, 1, 200) AS judge_rationale_short, "
        "  c.cashout_id, c.exit_price, c.realized_pnl_usd, "
        "  c.ts AS cashout_ts, "
        "  r.final_outcome, r.payout_per_share, r.observed_value, "
        "  cf.delta AS counterfactual_delta_usd, "
        "  cf.hypothetical_hold_pnl AS hold_pnl_usd, "
        "  (SELECT m.decision_reason FROM monitor_checks m "
        "   WHERE m.entry_id = e.entry_id AND m.decision='CASHOUT' "
        "   ORDER BY m.ts DESC LIMIT 1) AS exit_decision_reason "
        "FROM entries e "
        "LEFT JOIN judge_reviews j ON j.entry_id = e.entry_id "
        "LEFT JOIN cashouts c       ON c.entry_id = e.entry_id "
        "LEFT JOIN resolutions r    ON r.entry_id = e.entry_id "
        "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
        "WHERE e.status IN ('EXECUTED', 'FAST_PATH') "
        "  AND e.ts >= ? "
        "ORDER BY e.ts DESC LIMIT ?",
        (since_iso, limit),
    ).fetchall()


def query_for_counterfactual(conn) -> list[sqlite3.Row]:
    """Entries with both a cashout AND a resolution (ready for delta)."""
    return conn.execute(
        "SELECT e.entry_id, e.entry_price, e.size_shares, "
        "       c.cashout_id, c.exit_price, c.realized_pnl_usd, "
        "       r.payout_per_share "
        "FROM entries e "
        "JOIN cashouts c ON c.entry_id = e.entry_id "
        "JOIN resolutions r ON r.entry_id = e.entry_id "
        "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
        "WHERE cf.counterfactual_id IS NULL"
    ).fetchall()


def current_market_exposure_usd(conn, market_slug: str) -> float:
    """Sum of size_usd for all currently-open positions on this market
    (both YES and NO sides), excluding entries that already have a cashout.

    Used by run_execute to cap total $ exposure per market.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(e.size_usd), 0) "
        "FROM entries e "
        "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
        "WHERE e.market_slug = ? "
        "  AND e.status IN ('EXECUTED','FAST_PATH') "
        "  AND c.cashout_id IS NULL",
        (market_slug,),
    ).fetchone()
    return float(row[0] or 0.0)


def market_already_proposed(conn, market_slug: str, side: str) -> bool:
    """True if an entry for (market, side) is already PROPOSED/APPROVED/EXECUTED.

    Avoids the bot re-proposing the same trade every 10 min.
    """
    row = conn.execute(
        "SELECT 1 FROM entries WHERE market_slug = ? AND side = ? "
        "AND status IN ('PROPOSED','APPROVED','EXECUTED','FAST_PATH','ADJUSTED') LIMIT 1",
        (market_slug, side),
    ).fetchone()
    return row is not None


def _test_query_ladder_group_judge_join():
    """v9.11 regression: query_ladder_group must expose judge_adjusted_side
    so _execute_ladder_group_atomic can read it for ADJUSTED legs without
    raising IndexError. The pre-v9.11 query did SELECT * with no JOIN —
    leg["judge_adjusted_side"] crashed the main loop every 60s starting
    2026-05-22 23:00 until detected on 2026-05-24."""
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "regression_judge_join.db"
    init_db(tmp)
    gid = "test-group-001"
    with connect(tmp) as conn:
        # Insert a ladder leg with status=ADJUSTED + a judge_reviews row
        # with adjusted_side set (the exact production shape).
        ts = "2026-05-24T00:00:00+00:00"
        conn.execute(
            "INSERT INTO entries (ts, market_slug, market_question, side, "
            "status, ladder_group_id, ladder_position, entry_price) "
            "VALUES (?, 's', 'q', 'YES', 'ADJUSTED', ?, 'central', 0.45)",
            (ts, gid))
        eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO judge_reviews (entry_id, ts, verdict, confidence, "
            "judge_prob, bot_prob, rationale, cost_usd, adjusted_side, "
            "adjusted_size_usd) VALUES (?, ?, 'ADJUST', 0.6, 0.6, 0.7, '', "
            "0.02, 'NO', 25.0)",
            (eid, ts))
        conn.commit()
        legs = query_ladder_group(conn, gid)
        assert len(legs) == 1, legs
        leg = legs[0]
        # The bug: accessing this raised IndexError. Now should work.
        adj_side = leg["judge_adjusted_side"]
        adj_size = leg["judge_adjusted_size_usd"]
        assert adj_side == "NO", f"expected 'NO', got {adj_side!r}"
        assert adj_size == 25.0, f"expected 25.0, got {adj_size!r}"
        # status still accessible
        assert leg["status"] == "ADJUSTED"
        print(f"Test PASS: query_ladder_group exposes judge_adjusted_side='{adj_side}' "
              f"size=${adj_size} (no IndexError)")
    tmp.unlink()


if __name__ == "__main__":
    import sys
    if "--test-judge-join" in sys.argv:
        _test_query_ladder_group_judge_join()
        sys.exit(0)
    # Smoke test: init DB and print version
    init_db()
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"DB: {DB_PATH}")
        print(f"Schema version: {version}")
        print(f"Tables: {[t['name'] for t in tables]}")
