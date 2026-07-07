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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path.home() / ".polymarket-paper" / "weather_edge.db"
SCHEMA_VERSION = 11


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


# v10: persist the judge system prompt by content-hash so every
# judge_reviews row is fully reproducible. The prompt file is mutable
# (operator tunes it via the dashboard); without hashing+dedup we'd
# either lose historical prompts or duplicate ~8KB per review.
SCHEMA_V10_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS judge_prompts (
      sha256 TEXT PRIMARY KEY,
      text TEXT NOT NULL,
      first_seen_ts TEXT NOT NULL,
      char_count INTEGER NOT NULL
    )
    """,
    "ALTER TABLE judge_reviews ADD COLUMN system_prompt_sha256 TEXT",
]


# v11 (2026-07-06): per-strategy tagging. The cheap_convexity strategy buys
# 1-20c tail bins and exits on cashout at model fair; its entries must be
# isolated in every KPI query so they don't contaminate the tuned
# weather_edge P&L/win-rate. NULL == legacy 'weather_edge' (backfilled here;
# all readers use COALESCE(strategy,'weather_edge') for residual NULLs).
SCHEMA_V11_MIGRATIONS = [
    "ALTER TABLE entries ADD COLUMN strategy TEXT",
    "UPDATE entries SET strategy = 'weather_edge' WHERE strategy IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_entries_strategy ON entries(strategy)",
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
        if current < 10:
            for stmt in SCHEMA_V10_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 10
        if current < 11:
            for stmt in SCHEMA_V11_MIGRATIONS:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
            current = 11
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


def upsert_judge_prompt(conn, text: str) -> str:
    """Persist a system prompt by SHA-256 (idempotent). Returns the hash so
    callers can store it as a foreign-key-ish reference on judge_reviews."""
    import hashlib
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO judge_prompts "
        "(sha256, text, first_seen_ts, char_count) VALUES (?, ?, ?, ?)",
        (sha, text, datetime.now(timezone.utc).isoformat(), len(text)),
    )
    return sha


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
    """Entries awaiting judge review, oldest first by ttr.

    v15 F3: ladder_group_id as secondary key makes SIBLINGS CONTIGUOUS —
    legs of one group share an identical ttr_hours_at_entry (one `now` per
    discovery cycle + shared event end_date), so the tiebreak groups them
    within a batch and the judge's per-batch dead-set/sweep sees the whole
    group at once. NULLs (non-ladder rows) sort first in ASC, keeping their
    position among ttr ties; single-bin decisions are order-independent.
    Caveat: a group can still straddle the LIMIT boundary and ordering does
    nothing across polls — the F1 live-DB viability check remains the
    correctness backstop; this is a hit-rate optimization."""
    return conn.execute(
        "SELECT * FROM entries WHERE status = 'PROPOSED' "
        "ORDER BY ttr_hours_at_entry ASC, ladder_group_id ASC, ts ASC "
        "LIMIT ?",
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


def query_ladder_group_open(conn, ladder_group_id: str) -> list[sqlite3.Row]:
    """Legs of a ladder group that are genuinely still open: EXECUTED or
    FAST_PATH, with NO cashout row AND NO resolution row (mirrors
    query_open_positions' definition of "open").

    Post-mortem 2026-07-06: _do_ladder_cashout used to filter open legs by
    the cashouts table alone — no status filter, no resolutions check. It
    would try to close legs already settled by the resolution sweep (or
    never executed at all), failing "No open position" on every monitor
    tick (74 ladder_leg_close_rejected in one day). This helper is the
    single source of truth for "leg still needs closing".

    No judge JOIN: cashout phase 1 only reads entry_id/side/token_ids.
    """
    return conn.execute(
        "SELECT e.* FROM entries e "
        "LEFT JOIN cashouts    c ON c.entry_id = e.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ladder_group_id = ? "
        "AND e.status IN ('EXECUTED','FAST_PATH') "
        "AND c.cashout_id IS NULL "
        "AND r.resolution_id IS NULL "
        "ORDER BY CASE e.ladder_position WHEN 'central' THEN 0 "
        "WHEN 'below' THEN 1 WHEN 'above' THEN 2 ELSE 3 END, e.entry_id",
        (ladder_group_id,),
    ).fetchall()


# Statuses that permanently kill atomic execution of a ladder group. One
# terminal leg in this set means the group can NEVER execute (v9 strict
# atomicity) — shared by the executor's gate and the judge's viability
# pre-check so both processes agree on "dead".
LADDER_DEAD_STATUSES = frozenset({"REJECTED", "SKIPPED"})


def ladder_group_is_dead(statuses) -> bool:
    """Pure predicate: a ladder group can never execute atomically once ANY
    leg is REJECTED or SKIPPED, or when the group is empty. Mirrors the
    DEAD-by-sibling branch of weather_edge_bot._ladder_atomic_gate.

    Post-mortem 2026-07-07 (ladder_sibling_failed waste): the judge had no
    group-awareness and spent LLM reviews on legs whose sibling had already
    died — $2.24 = 18.9% of all judge spend. This predicate lets the judge
    detect group death BEFORE any spend, with semantics identical to the
    executor's gate."""
    s = set(statuses)
    return not s or bool(s & LADDER_DEAD_STATUSES)


def query_ladder_group_statuses(conn, ladder_group_id: str) -> list:
    """Cheap status-only fetch for a ladder group (no judge JOIN — the
    judge's F1 viability gate runs this per pending ladder row, before any
    HTTP or LLM spend)."""
    return [r["status"] for r in conn.execute(
        "SELECT status FROM entries WHERE ladder_group_id = ?",
        (ladder_group_id,)).fetchall()]


def query_ladder_group_dead(conn, ladder_group_id: str) -> bool:
    """True when the group is already unexecutable (see ladder_group_is_dead)."""
    return ladder_group_is_dead(query_ladder_group_statuses(conn, ladder_group_id))


def query_live_tokens(conn) -> set:
    """Outcome tokens "occupied" by a live entry — any strategy.

    Live = PROPOSED/APPROVED/ADJUSTED (in flight), or EXECUTED/FAST_PATH
    with no cashout AND no resolution (open position). Returns BOTH tokens
    (yes+no) of each live entry:

      - blocking the opposite side is already policy (opposite_side_held);
      - a judge ADJUST can flip the executed side without updating
        entries.side, so deriving "the occupied token" from e.side is
        unreliable.

    Post-mortem 2026-07-06: the paper engine keys positions by (portfolio,
    token, side) — entries per ladder leg. Discovery allowed re-entry on the
    same (slug, side) after execution, so ladder groups from different runs
    shared outcome tokens; their buys merged into ONE paper position and
    whichever leg closed first stranded the siblings (74 failed cashout
    retries in one day). Discovery must never propose a candidate whose
    token is in this set.

    Deliberately cross-strategy: paper positions are keyed by token, not
    strategy — a token held by cheap_convexity must block weather_edge
    proposals and vice-versa.
    """
    rows = conn.execute(
        "SELECT e.token_id_yes, e.token_id_no "
        "FROM entries e "
        "LEFT JOIN cashouts    c ON c.entry_id = e.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.status IN ('PROPOSED','APPROVED','ADJUSTED') "
        "   OR (e.status IN ('EXECUTED','FAST_PATH') "
        "       AND c.cashout_id IS NULL AND r.resolution_id IS NULL)"
    ).fetchall()
    return {tok for row in rows for tok in (row[0], row[1]) if tok}


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


def query_open_positions(conn, strategy: Optional[str] = None) -> list[sqlite3.Row]:
    """Entries that were executed and are still open: no cashout, no resolution.
    Resolved positions are excluded so the slot is freed for new bets.

    v11: `strategy` is an OPTIONAL filter. Default None returns ALL strategies
    (the monitor MUST see cheap_convexity positions to cash them out, so it
    calls with no filter). A dashboard/analysis caller can pass
    strategy='weather_edge' to exclude cheap_convexity, or 'cheap_convexity'
    to isolate it. NULL strategy rows count as 'weather_edge'."""
    q = ("SELECT e.* FROM entries e "
         "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
         "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
         "WHERE e.status IN ('EXECUTED','FAST_PATH') "
         "AND c.cashout_id IS NULL "
         "AND r.resolution_id IS NULL")
    params: tuple = ()
    if strategy is not None:
        q += " AND COALESCE(e.strategy, 'weather_edge') = ?"
        params = (strategy,)
    return conn.execute(q, params).fetchall()


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
    """Entries with both a cashout AND a resolution (ready for delta).

    Excludes phantom_shared_close rows: those are bookkeeping markers
    (position already sold via a sibling entry sharing the token — pnl 0,
    shares 0), not real exits; a counterfactual "cashout vs hold" delta
    computed from exit_price=NULL/0 shares would be garbage.
    """
    return conn.execute(
        "SELECT e.entry_id, e.entry_price, e.size_shares, "
        "       c.cashout_id, c.exit_price, c.realized_pnl_usd, "
        "       r.payout_per_share "
        "FROM entries e "
        "JOIN cashouts c ON c.entry_id = e.entry_id "
        "JOIN resolutions r ON r.entry_id = e.entry_id "
        "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
        "WHERE cf.counterfactual_id IS NULL "
        "  AND (c.reason IS NULL OR c.reason NOT LIKE 'phantom_shared_close%')"
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


def _test_query_ladder_group_open():
    """v13.4 regression: query_ladder_group_open must return ONLY legs that
    still need closing — EXECUTED/FAST_PATH with no cashout AND no
    resolution. The pre-v13.4 filter in _do_ladder_cashout checked only
    the cashouts table, so resolved/never-executed legs were re-closed
    (and re-failed) every monitor tick (74 rejections on 2026-07-06)."""
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "ladder_open.db"
    init_db(tmp)
    gid = "test-group-open"
    ts = "2026-07-06T00:00:00+00:00"
    with connect(tmp) as conn:
        def add(pos, status):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, ladder_group_id, ladder_position, entry_price, "
                "strategy) VALUES (?, 's', 'q', 'NO', ?, ?, ?, 0.3, "
                "'weather_edge')", (ts, status, gid, pos))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        clean = add("above", "EXECUTED")          # returned
        cashed = add("central", "EXECUTED")       # excluded: has cashout
        resolved = add("below", "FAST_PATH")      # excluded: has resolution
        add("above", "SKIPPED")                   # excluded: never executed
        conn.execute(
            "INSERT INTO cashouts (entry_id, ts, realized_pnl_usd) "
            "VALUES (?, ?, 1.0)", (cashed, ts))
        conn.execute(
            "INSERT INTO resolutions (entry_id, ts_resolved, final_outcome, "
            "payout_per_share) VALUES (?, ?, 'NO', 1.0)", (resolved, ts))
        conn.commit()

        legs = query_ladder_group_open(conn, gid)
        got = [(leg["entry_id"], leg["ladder_position"]) for leg in legs]
        assert got == [(clean, "above")], got
        print(f"Test PASS: query_ladder_group_open returns only the clean "
              f"EXECUTED leg ({got}) — cashed/resolved/skipped excluded")

        # Ordering: add a clean central leg — must come before 'above'.
        central = add("central", "EXECUTED")
        conn.commit()
        legs = query_ladder_group_open(conn, gid)
        order = [leg["entry_id"] for leg in legs]
        assert order == [central, clean], order
        print("Test PASS: ordering central -> above respected")
    tmp.unlink()


def _test_query_live_tokens():
    """v13.4: query_live_tokens must return the token pairs of live entries
    only — pending (PROPOSED/APPROVED/ADJUSTED) or open (EXECUTED/FAST_PATH
    without cashout AND without resolution) — across ALL strategies."""
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "live_tokens.db"
    init_db(tmp)
    ts = "2026-07-06T00:00:00+00:00"
    with connect(tmp) as conn:
        def add(slug, status, ty, tn, strategy="weather_edge"):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, entry_price, token_id_yes, token_id_no, strategy) "
                "VALUES (?, ?, 'q', 'NO', ?, 0.3, ?, ?, ?)",
                (ts, slug, status, ty, tn, strategy))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        add("s-open", "EXECUTED", "Y1", "N1")               # live: open
        cashed = add("s-cashed", "EXECUTED", "Y2", "N2")    # freed by cashout
        resolved = add("s-resolved", "FAST_PATH", "Y3", "N3")  # freed by resolution
        add("s-pending", "PROPOSED", "Y4", "N4")            # live: in flight
        add("s-skipped", "SKIPPED", "Y5", "N5")             # never live
        add("s-rejected", "REJECTED", "Y6", "N6")           # never live
        add("s-cc", "EXECUTED", "Y7", "N7", "cheap_convexity")  # live: cross-strategy
        conn.execute("INSERT INTO cashouts (entry_id, ts, realized_pnl_usd) "
                     "VALUES (?, ?, 1.0)", (cashed, ts))
        conn.execute("INSERT INTO resolutions (entry_id, ts_resolved, "
                     "final_outcome, payout_per_share) VALUES (?, ?, 'NO', 1.0)",
                     (resolved, ts))
        conn.commit()

        live = query_live_tokens(conn)
        expected = {"Y1", "N1", "Y4", "N4", "Y7", "N7"}
        assert live == expected, f"expected {expected}, got {live}"
    print("Test PASS: query_live_tokens = open+pending (both tokens, "
          "cross-strategy); cashed/resolved/skipped/rejected freed")
    tmp.unlink()


def _test_pending_order():
    """v15 F3: query_pending_proposals must keep ladder siblings CONTIGUOUS
    within a ttr tie (secondary key ladder_group_id), with NULL (non-ladder)
    rows first among the tie, cross-ttr ordering unchanged, LIMIT respected."""
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "pending_order.db"
    init_db(tmp)
    with connect(tmp) as conn:
        def add(ts, ttr, gid):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, entry_price, ttr_hours_at_entry, ladder_group_id, "
                "strategy) VALUES (?, 's', 'q', 'NO', 'PROPOSED', 0.3, ?, ?, "
                "'weather_edge')", (ts, ttr, gid))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # ttr=10.0 tie, tses INTERLEAVED between groups (the old ts-only
        # tiebreak would interleave the legs):
        a1 = add("2026-07-07T00:00:01Z", 10.0, "grp-A")
        b1 = add("2026-07-07T00:00:02Z", 10.0, "grp-B")
        s1 = add("2026-07-07T00:00:03Z", 10.0, None)     # single-bin
        a2 = add("2026-07-07T00:00:04Z", 10.0, "grp-A")
        b2 = add("2026-07-07T00:00:05Z", 10.0, "grp-B")
        # menor ttr vem antes de tudo, mesmo com ts mais tarde:
        c1 = add("2026-07-07T00:00:06Z", 5.0, "grp-C")
        conn.commit()

        got = [r["entry_id"] for r in query_pending_proposals(conn)]
        # ttr 5 primeiro; no tie de ttr=10: NULL primeiro (ASC), depois
        # grp-A contíguo, depois grp-B contíguo.
        assert got == [c1, s1, a1, a2, b1, b2], got
        print(f"Test PASS: ordering [{got}] — ttr primeiro, NULL antes, "
              f"irmãs contíguas (A junto, B junto)")

        got3 = [r["entry_id"] for r in query_pending_proposals(conn, limit=3)]
        assert got3 == [c1, s1, a1], got3
        print("Test PASS: LIMIT respeitado")
    tmp.unlink()


def _test_ladder_group_dead():
    """v15: shared dead-group predicate — pure combos + temp-DB round trip.
    Must mirror _ladder_atomic_gate's DEAD-by-sibling semantics exactly."""
    # Pure predicate
    assert ladder_group_is_dead([]) is True                       # empty
    assert ladder_group_is_dead(["PROPOSED"]) is False
    assert ladder_group_is_dead(["PROPOSED", "PROPOSED"]) is False
    assert ladder_group_is_dead(["APPROVED", "PROPOSED"]) is False
    assert ladder_group_is_dead(["APPROVED", "ADJUSTED"]) is False
    assert ladder_group_is_dead(["PROPOSED", "REJECTED"]) is True
    assert ladder_group_is_dead(["APPROVED", "SKIPPED"]) is True
    assert ladder_group_is_dead(["EXECUTED", "EXECUTED"]) is False  # gate trata à parte
    print("Test PASS: predicado puro (vazio/REJECTED/SKIPPED → morta; "
          "pending/approved/executed → viva)")

    # Temp-DB round trip
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "group_dead.db"
    init_db(tmp)
    ts = "2026-07-07T00:00:00+00:00"
    with connect(tmp) as conn:
        for gid, status in (("g-alive", "PROPOSED"), ("g-alive", "APPROVED"),
                            ("g-dead", "APPROVED"), ("g-dead", "SKIPPED")):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, ladder_group_id, entry_price, strategy) VALUES "
                "(?, 's', 'q', 'NO', ?, ?, 0.3, 'weather_edge')",
                (ts, status, gid))
        conn.commit()
        assert query_ladder_group_statuses(conn, "g-alive") == [
            "PROPOSED", "APPROVED"] or sorted(
            query_ladder_group_statuses(conn, "g-alive")) == [
            "APPROVED", "PROPOSED"]
        assert query_ladder_group_dead(conn, "g-alive") is False
        assert query_ladder_group_dead(conn, "g-dead") is True
        assert query_ladder_group_dead(conn, "g-missing") is True  # vazio = morta
    print("Test PASS: round-trip em DB (g-alive viva, g-dead morta, "
          "inexistente morta)")
    tmp.unlink()


def _test_migration_v11_strategy():
    """v11 regression: migrating a v10 DB must add the `strategy` column,
    backfill legacy rows to 'weather_edge', create the index, bump
    user_version to 11, and be idempotent on a second init_db()."""
    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "migration_v11.db"

    # Build a DB frozen at v10 (run every migration except v11), then insert a
    # legacy entry with no strategy value — exactly the pre-v11 production shape.
    with sqlite3.connect(tmp, timeout=5.0) as conn:
        cur = conn.cursor()
        cur.executescript(SCHEMA_V1)
        for block in (SCHEMA_V2_MIGRATIONS, SCHEMA_V3_MIGRATIONS,
                      SCHEMA_V4_MIGRATIONS, SCHEMA_V5_MIGRATIONS,
                      SCHEMA_V6_MIGRATIONS, SCHEMA_V7_MIGRATIONS,
                      SCHEMA_V8_MIGRATIONS, SCHEMA_V9_MIGRATIONS,
                      SCHEMA_V10_MIGRATIONS):
            for stmt in block:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
        cur.execute("PRAGMA user_version = 10")
        cur.execute(
            "INSERT INTO entries (ts, market_slug, market_question, side, "
            "status, entry_price) VALUES "
            "('2026-07-06T00:00:00+00:00', 's', 'q', 'YES', 'EXECUTED', 0.45)")
        conn.commit()

    # Apply the real migration.
    init_db(tmp)

    with connect(tmp) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
        assert "strategy" in cols, cols
        idx = {r[1] for r in conn.execute("PRAGMA index_list(entries)")}
        assert "idx_entries_strategy" in idx, idx
        # legacy row backfilled
        row = conn.execute("SELECT strategy FROM entries").fetchone()
        assert row["strategy"] == "weather_edge", row["strategy"]

    # Idempotent: a second init_db must not raise and must stay at v11.
    init_db(tmp)
    with connect(tmp) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    print("Test PASS: v11 migration adds strategy col + index, backfills "
          "'weather_edge', bumps user_version, idempotent")

    # Isolation: query_open_positions filter must separate cheap_convexity
    # from weather_edge (the operator's "identify entries for analysis" ask),
    # while default (None) still returns both (so the monitor sees all).
    with connect(tmp) as conn:
        ts = "2026-07-06T01:00:00+00:00"
        for strat in ("weather_edge", "cheap_convexity"):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, entry_price, strategy) VALUES "
                "(?, ?, 'q', 'YES', 'EXECUTED', 0.10, ?)",
                (ts, f"slug-{strat}", strat))
        conn.commit()
        all_open = query_open_positions(conn)
        we_open = query_open_positions(conn, strategy="weather_edge")
        cc_open = query_open_positions(conn, strategy="cheap_convexity")
        # the legacy backfilled EXECUTED row (weather_edge) + 2 inserted = 3
        assert len(all_open) == 3, len(all_open)
        assert len(cc_open) == 1, len(cc_open)
        assert all(r["strategy"] == "cheap_convexity" for r in cc_open)
        assert len(we_open) == 2, len(we_open)  # backfilled + inserted
        assert all(r["strategy"] == "weather_edge" for r in we_open)
    print("Test PASS: query_open_positions isolates cheap_convexity "
          "(all=3, weather_edge=2, cheap_convexity=1)")
    tmp.unlink()


if __name__ == "__main__":
    import sys
    if "--test-judge-join" in sys.argv:
        _test_query_ladder_group_judge_join()
        sys.exit(0)
    if "--test-migration" in sys.argv:
        _test_migration_v11_strategy()
        sys.exit(0)
    if "--test-ladder-open" in sys.argv:
        _test_query_ladder_group_open()
        sys.exit(0)
    if "--test-live-tokens" in sys.argv:
        _test_query_live_tokens()
        sys.exit(0)
    if "--test-group-dead" in sys.argv:
        _test_ladder_group_dead()
        sys.exit(0)
    if "--test-pending-order" in sys.argv:
        _test_pending_order()
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
