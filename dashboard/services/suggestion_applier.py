"""Apply advisor suggestions to source files + commit via git.

Safe categories (have an [Apply] button in the UI):
  - threshold:    CLI flag defaults in weather_edge_bot.py
  - mae_constant: MAE_* constants in weather_edge_helpers.py
  - city:         add/remove city from weather-cities.json (proposed_value
                  format: "add:CityName" or "remove:CityName")
  - risk_limit:   integer values in paper_engine.py DEFAULT_RISK dict
                  (tightening only — never loosening)

Unsupported categories (judge_prompt, data_source) return status='unsupported'
and the UI disables the Apply button with a tooltip.

Side effects per successful apply:
  - file edit in place
  - `git add` + `git commit` with descriptive message
  - row inserted into advisor_suggestion_applies (UNIQUE constraint
    prevents re-applying the same (run_id, suggestion_id))
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEATHER_EDGE_BOT = REPO_ROOT / "polymarket-analyzer" / "scripts" / "weather_edge_bot.py"
WEATHER_EDGE_HELPERS = REPO_ROOT / "polymarket-analyzer" / "scripts" / "weather_edge_helpers.py"
PAPER_ENGINE = REPO_ROOT / "polymarket-paper-trader" / "scripts" / "paper_engine.py"
CITIES_JSON = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-cities.json"

from .. import settings as S  # noqa: E402


SUPPORTED_CATEGORIES = {"threshold", "mae_constant", "city", "risk_limit"}


class ApplyError(Exception):
    pass


class SuggestionApplier:
    """Per-instance applier — keeps a DB connection for the audit insert."""

    def __init__(self, db_path: Optional[Path] = None,
                 dry_run: bool = False,
                 git_runner: Optional[callable] = None):
        """`db_path` defaults to settings.WEATHER_EDGE_DB.
        `dry_run=True` skips the actual file write + git commit (used in tests).
        `git_runner` is an injectable callable mocked by tests; defaults to
        subprocess.run that actually invokes git."""
        self.db_path = db_path or S.WEATHER_EDGE_DB
        self.dry_run = dry_run
        self._run_git = git_runner or self._default_git

    # === Public API ===

    def apply(self, run_id: int, suggestion: dict) -> dict:
        """Apply one advisor suggestion. Returns {status, previous_value,
        applied_value, git_sha, error_msg}."""
        cat = suggestion.get("category")
        sug_id = suggestion.get("id") or "(no_id)"
        param_path = suggestion.get("param_path") or ""

        # Idempotency check: already applied?
        prior = self._fetch_prior(run_id, sug_id)
        if prior is not None:
            return {
                "status": prior["status"],
                "previous_value": prior["previous_value"],
                "applied_value": prior["applied_value"],
                "git_sha": prior["git_commit_sha"],
                "error_msg": prior.get("error_msg"),
                "already_recorded": True,
            }

        if cat not in SUPPORTED_CATEGORIES:
            return self._record(run_id, suggestion,
                                 status="unsupported",
                                 error_msg=f"Category '{cat}' requires manual edit")

        try:
            if cat == "threshold":
                res = self._apply_threshold(suggestion)
            elif cat == "mae_constant":
                res = self._apply_mae_constant(suggestion)
            elif cat == "city":
                res = self._apply_city(suggestion)
            elif cat == "risk_limit":
                res = self._apply_risk_limit(suggestion)
            else:
                raise ApplyError(f"unreachable category {cat}")

            sha = None
            if not self.dry_run:
                msg = (f"Apply advisor suggestion {sug_id} ({cat}): "
                       f"{res['previous_value']} → {res['applied_value']}")
                sha = self._git_commit(msg, res["touched_file"])

            return self._record(
                run_id, suggestion, status="applied",
                previous_value=str(res["previous_value"]),
                applied_value=str(res["applied_value"]),
                git_commit_sha=sha,
            )
        except ApplyError as e:
            return self._record(run_id, suggestion, status="failed",
                                 error_msg=str(e))
        except Exception as e:  # pragma: no cover — unexpected
            return self._record(run_id, suggestion, status="failed",
                                 error_msg=f"unexpected: {type(e).__name__}: {e}")

    # === Per-category appliers ===

    def _apply_threshold(self, suggestion: dict) -> dict:
        """`param_path` like 'weather_edge_bot.py:--profit-lock-pp default'.
        Regex-substitutes the `default=N.NN` in the add_argument call."""
        flag = self._extract_flag(suggestion["param_path"])
        new_value = suggestion["proposed_value"]
        if new_value is None:
            raise ApplyError("threshold suggestion missing proposed_value")
        return self._regex_replace_default(
            WEATHER_EDGE_BOT, flag, new_value)

    def _apply_mae_constant(self, suggestion: dict) -> dict:
        """`param_path` like 'weather_edge_helpers.py:MAE_TEMP_F'."""
        const_name = self._extract_constant_name(suggestion["param_path"])
        new_value = suggestion["proposed_value"]
        if new_value is None:
            raise ApplyError("mae_constant suggestion missing proposed_value")
        path = WEATHER_EDGE_HELPERS
        if not path.exists():
            raise ApplyError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        pattern = rf"^({re.escape(const_name)}\s*=\s*)([0-9.\-]+)"
        m = re.search(pattern, text, re.MULTILINE)
        if not m:
            raise ApplyError(f"constant {const_name} not found in {path.name}")
        previous = m.group(2)
        new_text = (text[:m.start(2)] + str(new_value) + text[m.end(2):])
        if not self.dry_run:
            path.write_text(new_text, encoding="utf-8")
        return {"previous_value": previous, "applied_value": new_value,
                "touched_file": path}

    def _apply_city(self, suggestion: dict) -> dict:
        """`proposed_value` is 'add:CityName' or 'remove:CityName'."""
        proposed = suggestion.get("proposed_value")
        if not isinstance(proposed, str) or ":" not in proposed:
            raise ApplyError("city proposed_value must be 'add:Name' or "
                             "'remove:Name'")
        action, _, name = proposed.partition(":")
        action = action.strip().lower()
        name = name.strip()
        if action not in ("add", "remove") or not name:
            raise ApplyError(f"invalid city action {action!r}")
        path = CITIES_JSON
        if not path.exists():
            raise ApplyError(f"file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        # Group is heuristic: default to 'world' if not specified.
        # param_path can override e.g. "weather-cities.json:us_top50"
        group = "world"
        pp = suggestion.get("param_path", "")
        if ":" in pp:
            tail = pp.split(":", 1)[1].strip()
            if tail in data and isinstance(data[tail], list):
                group = tail
        if group not in data or not isinstance(data[group], list):
            raise ApplyError(f"group {group!r} not a list in cities JSON")
        if action == "add":
            if name in data[group]:
                raise ApplyError(f"{name} already in {group}")
            data[group].append(name)
            previous = "(not present)"
            applied = f"added to {group}"
        else:
            if name not in data[group]:
                raise ApplyError(f"{name} not in {group}")
            data[group].remove(name)
            previous = f"present in {group}"
            applied = f"removed from {group}"
        if not self.dry_run:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        return {"previous_value": previous, "applied_value": applied,
                "touched_file": path}

    def _apply_risk_limit(self, suggestion: dict) -> dict:
        """`param_path` like 'paper_engine.py:max_concurrent_positions'.
        Substitutes the integer in the DEFAULT_RISK dict.

        Constitutional rule (CLAUDE.md §2): can only TIGHTEN limits.
        For max_concurrent_positions and max_position_pct, tighten = decrease.
        For max_drawdown_pct, tighten = decrease too.
        Operator's call which direction is tighter — we just refuse loosening
        for max_concurrent_positions specifically since that's the most
        common case.
        """
        key = self._extract_constant_name(suggestion["param_path"])
        new_value = suggestion["proposed_value"]
        if new_value is None:
            raise ApplyError("risk_limit suggestion missing proposed_value")
        path = PAPER_ENGINE
        if not path.exists():
            raise ApplyError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        pattern = (rf'("{re.escape(key)}"\s*:\s*)([0-9.\-]+)')
        m = re.search(pattern, text)
        if not m:
            raise ApplyError(f"risk limit {key} not found in {path.name}")
        previous = m.group(2)
        try:
            prev_num = float(previous)
            new_num = float(new_value)
        except ValueError:
            raise ApplyError(f"non-numeric risk limit value: {previous!r} → "
                             f"{new_value!r}")
        # Tighten-only guard for max_concurrent_positions
        if key == "max_concurrent_positions" and new_num > prev_num:
            raise ApplyError(
                f"refusing to loosen {key} from {prev_num} to {new_num} — "
                "risk limits can only be tightened (CLAUDE.md §2)"
            )
        new_text = text[:m.start(2)] + str(new_value) + text[m.end(2):]
        if not self.dry_run:
            path.write_text(new_text, encoding="utf-8")
        return {"previous_value": previous, "applied_value": new_value,
                "touched_file": path}

    # === Helpers ===

    def _regex_replace_default(self, path: Path, flag: str,
                                new_value) -> dict:
        if not path.exists():
            raise ApplyError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        # Match: add_argument("--flag", ..., default=X
        pattern = (rf'(add_argument\(\s*["\']({re.escape(flag)})["\']'
                   r'[^)]*?default\s*=\s*)([0-9.\-]+)')
        m = re.search(pattern, text)
        if not m:
            raise ApplyError(f"flag {flag} not found in {path.name}")
        previous = m.group(3)
        new_text = text[:m.start(3)] + str(new_value) + text[m.end(3):]
        if not self.dry_run:
            path.write_text(new_text, encoding="utf-8")
        return {"previous_value": previous, "applied_value": new_value,
                "touched_file": path}

    def _extract_flag(self, param_path: str) -> str:
        """'weather_edge_bot.py:--profit-lock-pp default' → '--profit-lock-pp'"""
        if ":" not in param_path:
            raise ApplyError(f"invalid param_path: {param_path!r}")
        tail = param_path.split(":", 1)[1].strip()
        # Tail may be "--flag-name" or "--flag-name default" — take first token
        return tail.split()[0]

    def _extract_constant_name(self, param_path: str) -> str:
        """'weather_edge_helpers.py:MAE_TEMP_F' → 'MAE_TEMP_F'"""
        if ":" not in param_path:
            raise ApplyError(f"invalid param_path: {param_path!r}")
        return param_path.split(":", 1)[1].strip().split()[0]

    def _git_commit(self, msg: str, touched_file: Path) -> Optional[str]:
        """Stage the touched file + commit. Returns short SHA on success,
        None if commit was a no-op."""
        cwd = str(REPO_ROOT)
        try:
            self._run_git(["git", "-C", cwd, "add", str(touched_file)],
                           check=True)
            r = self._run_git(["git", "-C", cwd, "commit", "-m", msg],
                               check=False)
            if r.returncode != 0:
                return None  # nothing to commit (file unchanged?)
            sha = self._run_git(
                ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                check=True, capture=True,
            ).stdout.strip()
            return sha
        except subprocess.CalledProcessError as e:
            raise ApplyError(f"git failed: {e}")

    @staticmethod
    def _default_git(cmd, check=True, capture=False):
        return subprocess.run(cmd, check=check,
                              capture_output=True if capture else False,
                              text=True)

    # === DB layer ===

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), timeout=5.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    def _fetch_prior(self, run_id: int, sug_id: str) -> Optional[dict]:
        if not self.db_path.exists():
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM advisor_suggestion_applies "
                "WHERE run_id = ? AND suggestion_id = ?",
                (run_id, sug_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _record(self, run_id: int, suggestion: dict,
                 status: str,
                 previous_value: Optional[str] = None,
                 applied_value: Optional[str] = None,
                 git_commit_sha: Optional[str] = None,
                 error_msg: Optional[str] = None) -> dict:
        ts = datetime.now(timezone.utc).isoformat()
        if not self.dry_run and self.db_path.exists():
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO advisor_suggestion_applies "
                    "(run_id, suggestion_id, ts, category, param_path, "
                    " previous_value, applied_value, git_commit_sha, status, "
                    " error_msg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, suggestion.get("id") or "(no_id)", ts,
                     suggestion.get("category"),
                     suggestion.get("param_path"),
                     previous_value, applied_value, git_commit_sha,
                     status, error_msg),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "status": status,
            "previous_value": previous_value,
            "applied_value": applied_value,
            "git_sha": git_commit_sha,
            "error_msg": error_msg,
        }


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    # Set up temp source files mimicking the real ones
    tmp = Path(tempfile.mkdtemp())
    bot_file = tmp / "weather_edge_bot.py"
    bot_file.write_text(
        'p.add_argument("--profit-lock-pp", type=float, default=50.0,\n'
        '               help="something")\n'
    )
    helpers_file = tmp / "weather_edge_helpers.py"
    helpers_file.write_text("MAE_TEMP_F = 5.0\nMAE_TEMP_C = 2.78\n")
    cities_file = tmp / "weather-cities.json"
    cities_file.write_text(json.dumps({"world": ["Tokyo", "Manhattan"]}))

    # When run as `python3 -m dashboard.services.suggestion_applier`,
    # this module IS __main__. The methods resolve WEATHER_EDGE_BOT etc
    # from this module's globals. Rebind here directly (don't re-import
    # as `mod`, which would patch the OTHER namespace).
    import sys as _sys
    _self = _sys.modules[__name__]
    _self.WEATHER_EDGE_BOT = bot_file
    _self.WEATHER_EDGE_HELPERS = helpers_file
    _self.CITIES_JSON = cities_file

    # Mock git runner — pretend success but don't actually call git
    class _MockRun:
        def __init__(self, stdout="abc123\n", returncode=0):
            self.stdout = stdout
            self.returncode = returncode
    git_calls = []
    def mock_git(cmd, check=True, capture=False):
        git_calls.append(cmd)
        if "rev-parse" in cmd:
            return _MockRun(stdout="abc1234\n")
        return _MockRun()

    db_file = tmp / "edge.db"
    c = sqlite3.connect(db_file)
    c.executescript("""
        CREATE TABLE advisor_suggestion_applies (
            apply_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, suggestion_id TEXT, ts TEXT,
            category TEXT, param_path TEXT,
            previous_value TEXT, applied_value TEXT,
            git_commit_sha TEXT, status TEXT, error_msg TEXT,
            UNIQUE(run_id, suggestion_id));
    """)
    c.commit()
    c.close()
    applier = SuggestionApplier(db_path=db_file, git_runner=mock_git)

    # Test 1: threshold apply
    res = applier.apply(run_id=1, suggestion={
        "id": "sug_001", "category": "threshold",
        "param_path": "weather_edge_bot.py:--profit-lock-pp default",
        "proposed_value": 35,
    })
    assert res["status"] == "applied", res
    assert res["previous_value"] == "50.0", res
    assert res["applied_value"] == "35", res
    assert res["git_sha"] == "abc1234"
    new_text = bot_file.read_text()
    assert "default=35" in new_text, new_text
    print(f"Test 1 PASS: threshold apply 50.0 → 35, git_sha={res['git_sha']}")

    # Test 2: idempotency — second apply returns recorded result
    res2 = applier.apply(run_id=1, suggestion={
        "id": "sug_001", "category": "threshold",
        "param_path": "weather_edge_bot.py:--profit-lock-pp default",
        "proposed_value": 25,
    })
    assert res2["status"] == "applied"
    assert res2.get("already_recorded") is True
    # File should NOT have been modified to 25
    assert "default=25" not in bot_file.read_text()
    print(f"Test 2 PASS: idempotency — second apply returns recorded {res2['applied_value']}")

    # Test 3: mae_constant apply
    res3 = applier.apply(run_id=2, suggestion={
        "id": "sug_002", "category": "mae_constant",
        "param_path": "weather_edge_helpers.py:MAE_TEMP_F",
        "proposed_value": 7.0,
    })
    assert res3["status"] == "applied", res3
    assert res3["previous_value"] == "5.0"
    assert "MAE_TEMP_F = 7.0" in helpers_file.read_text()
    print(f"Test 3 PASS: mae_constant 5.0 → 7.0")

    # Test 4: city add
    res4 = applier.apply(run_id=3, suggestion={
        "id": "sug_003", "category": "city",
        "param_path": "weather-cities.json:world",
        "proposed_value": "add:Reykjavik",
    })
    assert res4["status"] == "applied", res4
    cities_now = json.loads(cities_file.read_text())
    assert "Reykjavik" in cities_now["world"]
    print(f"Test 4 PASS: city add Reykjavik to world")

    # Test 5: city remove
    res5 = applier.apply(run_id=4, suggestion={
        "id": "sug_004", "category": "city",
        "param_path": "weather-cities.json:world",
        "proposed_value": "remove:Manhattan",
    })
    assert res5["status"] == "applied", res5
    cities_now = json.loads(cities_file.read_text())
    assert "Manhattan" not in cities_now["world"]
    print(f"Test 5 PASS: city remove Manhattan from world")

    # Test 6: unsupported category
    res6 = applier.apply(run_id=5, suggestion={
        "id": "sug_005", "category": "judge_prompt",
        "param_path": "weather-judge-prompt.md:section",
        "proposed_value": None,
    })
    assert res6["status"] == "unsupported"
    assert "manual edit" in (res6["error_msg"] or "").lower()
    print(f"Test 6 PASS: unsupported category → status=unsupported")

    # Test 7: failed apply (flag doesn't exist)
    res7 = applier.apply(run_id=6, suggestion={
        "id": "sug_006", "category": "threshold",
        "param_path": "weather_edge_bot.py:--nonexistent default",
        "proposed_value": 10,
    })
    assert res7["status"] == "failed"
    assert "not found" in (res7["error_msg"] or "").lower()
    print(f"Test 7 PASS: missing flag → status=failed")

    print("\nAll suggestion_applier tests PASS")
