# Applying Advisor Suggestions — Operator Guide

This document covers how to apply the strategy advisor's suggestions to the
codebase. As of v5, most categories have an **Apply** button in the
dashboard at `/advisor`. A few categories — and any failed auto-apply —
require manual editing.

---

## TL;DR — coverage table

| Category | Mechanism | UI flow |
|---|---|---|
| `threshold` | Auto | Apply button → file edit + git commit (+ optional bot restart) |
| `mae_constant` | Auto | Apply button → file edit + git commit (+ optional bot+judge restart) |
| `city` | Auto | Apply button → JSON edit + git commit (+ optional bot restart) |
| `risk_limit` | Auto | Apply button → file edit + git commit (+ optional bot restart). Refuses to loosen `max_concurrent_positions` (constitutional guard) |
| `judge_prompt` | Modal | Apply button → editor modal → operator edits full file → Save → git commit (+ optional judge restart) |
| `data_source` | **Manual** | No button — see below |
| Any apply with `status='failed'` | **Manual** | Auto-apply hit an error — see below |

---

## Why some categories are manual

`data_source` suggestions ask for structural changes: switch forecast
provider, add a new API integration, change a parser. No regex pattern
can mechanically apply these — they require operator judgment about
function signatures, error handling, secrets, dependency installs, etc.

Failed auto-applies happen when:

- A flag/constant was renamed since the advisor's prompt was written.
- The proposed value violates a guard (e.g. trying to loosen
  `max_concurrent_positions` against §2 of `CLAUDE.md`).
- A file path moved in a refactor.
- The proposed_value format doesn't match what the applier expects
  (e.g. a `city` suggestion missing the `add:` / `remove:` prefix).

The card surfaces the error message — read it before opening an editor.

---

## Manual apply — 5-step flow

### 1. Read the card

In the dashboard at `/advisor`, click into the run, find the suggestion.
Note these fields:

- `id` (e.g. `sug_005`)
- `category` (e.g. `data_source`)
- `param_path` (e.g. `weather_edge_helpers.py:fetch_forecast`)
- `current_value` / `proposed_value`
- `rationale` + `counterfactual`

The `param_path` is your starting point. Format is usually
`<file>:<function_or_constant_or_section>`.

### 2. Edit the source file

Open the file referenced in `param_path`:

```powershell
code polymarket-analyzer\scripts\weather_edge_helpers.py
# Search for the function/constant named after the ':' part of param_path
```

For `data_source` suggestions, the rationale tells you what to change.
Implement the change carefully — these touch live integrations.

### 3. Commit using the standard message format

Use the same commit message format the auto-apply uses, so
`git log --grep="advisor suggestion"` aggregates everything:

```powershell
git add <touched-file>
git commit -m "Apply advisor suggestion sug_005 (data_source): OpenWeather -> NOAA for US cities"
```

The format is:
```
Apply advisor suggestion <id> (<category>): <previous_value> -> <new_value>
```

### 4. (Optional) Record in the audit table

If you want the dashboard card to flip to ✓ Applied (and prevent
double-application), insert a row into `advisor_suggestion_applies`.
Replace `<RUN_ID>`, `<SUG_ID>`, etc. with values from the card:

```powershell
python -c @"
import sqlite3, os, subprocess
from datetime import datetime, timezone
sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode().strip()
c = sqlite3.connect(os.path.expanduser('~/.polymarket-paper/weather_edge.db'))
c.execute(
    'INSERT INTO advisor_suggestion_applies '
    '(run_id, suggestion_id, ts, category, param_path, '
    ' previous_value, applied_value, git_commit_sha, status) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    (
        <RUN_ID>,                                # int — from the URL or card
        '<SUG_ID>',                              # str — e.g. 'sug_005'
        datetime.now(timezone.utc).isoformat(),
        '<CATEGORY>',                            # str — e.g. 'data_source'
        '<PARAM_PATH>',                          # str — from the card
        '<PREVIOUS_VALUE>',                      # str or None
        '<APPLIED_VALUE>',                       # str or None
        sha,
        'applied',
    ),
)
c.commit()
print(f'Recorded apply for {<SUG_ID>} with sha={sha}')
"@
```

The `UNIQUE(run_id, suggestion_id)` constraint will reject duplicates,
so you can't accidentally record the same suggestion twice.

If you skip step 4, the card still shows the Apply button. Clicking it
will likely fail (since the regex now finds your already-changed value
or no match), but it's harmless — the change is in git either way.

### 5. Restart affected processes

Auto-apply restarts processes via `process_manager`. Manual apply doesn't,
so do it yourself. Use the mapping below.

| Category | Restart |
|---|---|
| `threshold` | bot |
| `mae_constant` | bot + judge |
| `city` | bot |
| `risk_limit` | bot |
| `judge_prompt` | judge |
| `data_source` | depends on which function changed — usually bot (forecast fetch is invoked from the bot's monitor/discovery loops) |

To restart manually:

```powershell
# In the terminal running the bot:
Ctrl+C
# Wait for "shutdown_clean" log line, then relaunch with the same command:
python polymarket-analyzer\scripts\weather_edge_bot.py --daemon --min-edge-pp 25 ...

# Same for judge in its terminal.
```

If you want the dashboard's auto-restart to handle future applies,
make sure the bot/judge **was started after v5 deploy** — only then
do they write `~/.polymarket-paper/{bot,judge}.pid.json`, which the
dashboard reads to find them.

---

## Handling failed auto-applies

When you see a red card with `status='failed'`:

1. **Read `error_msg`** on the card — it tells you exactly what blew up.

2. **Common errors and fixes**:

   | Error | Likely cause | Fix |
   |---|---|---|
   | `flag X not found in weather_edge_bot.py` | Flag renamed | Find the new flag name, edit by hand |
   | `constant X not found in weather_edge_helpers.py` | Constant moved or renamed | grep for the new location, edit by hand |
   | `refusing to loosen max_concurrent_positions from N to M` | Constitutional guard (CLAUDE.md §2) | Don't apply. If you really want to, edit `paper_engine.py` manually — but reconsider whether the advisor is misreading the situation |
   | `non-numeric risk limit value` | Proposed value isn't a number | Sanity check the advisor's suggestion — sometimes Claude proposes a string like `"high"` instead of an integer. Manual edit |
   | `file not found: ...` | File path stale | Locate the new path, manual edit |
   | `<category> proposed_value must be 'add:Name' or 'remove:Name'` | Advisor proposed a city without prefix | Edit `weather-cities.json` manually OR re-run advisor with a stricter prompt |

3. **Manual edit** following the 5-step flow above.

4. **The audit row is already recorded** with `status='failed'` —
   the `UNIQUE(run_id, suggestion_id)` constraint will block another
   automated attempt. To re-attempt after fixing the underlying issue,
   delete the failed row first:

   ```powershell
   python -c "import sqlite3, os; c = sqlite3.connect(os.path.expanduser('~/.polymarket-paper/weather_edge.db')); c.execute(\"DELETE FROM advisor_suggestion_applies WHERE run_id=<RUN_ID> AND suggestion_id='<SUG_ID>'\"); c.commit(); print('cleared')"
   ```

---

## Verifying audit state

To check what's been applied across all advisor runs:

```powershell
python -c @"
import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.polymarket-paper/weather_edge.db'))
rows = c.execute(
    'SELECT apply_id, run_id, suggestion_id, ts, category, status, '
    '       previous_value, applied_value, git_commit_sha '
    'FROM advisor_suggestion_applies ORDER BY ts DESC LIMIT 30'
).fetchall()
for r in rows:
    print(r)
"@
```

Or grep git directly (works even without audit rows):

```powershell
git log --grep="advisor suggestion" --oneline
```

---

## Reverting an applied suggestion

The audit table records the git commit SHA. To revert:

```powershell
git revert <SHA>
```

This creates a new commit undoing the change. The original audit row
stays put (so you have history of what was tried). If you want the
dashboard to stop showing the suggestion as applied, update the row:

```powershell
python -c "import sqlite3, os; c = sqlite3.connect(os.path.expanduser('~/.polymarket-paper/weather_edge.db')); c.execute(\"UPDATE advisor_suggestion_applies SET status='reverted' WHERE git_commit_sha='<SHA>'\"); c.commit(); print('marked reverted')"
```

Then restart the affected processes.

---

## When to skip a suggestion entirely

The advisor is one input. You're allowed to ignore suggestions, especially:

- Low-confidence suggestions in categories with structural impact
  (`data_source`, `judge_prompt`).
- Threshold suggestions whose backtest delta is small (< $20 or < 10%
  relative). Variance in 30-50 trades is high — the advisor is
  instructed to demote these to `low` confidence, but it doesn't always.
- Suggestions that conflict with `CLAUDE.md` constitution. The
  constitution wins, always.

There's no "dismiss" button in v5. To skip, simply don't click Apply.
The card stays in the report; the next advisor run may or may not
re-surface it. If you want to suppress permanently, file an issue and
let the next advisor know via the `analyzer_report_md` context.

---

## Quick reference — commands you'll repeat

```powershell
# View a run's full report
# (Open dashboard at /advisor and click "View")

# Manual apply: edit + commit
git add <file>
git commit -m "Apply advisor suggestion <id> (<category>): <prev> -> <new>"

# Restart bot
# (Ctrl+C in bot terminal, then re-run launch command)

# Check git history of all advisor-driven changes
git log --grep="advisor suggestion" --oneline

# Check audit table
python -c "import sqlite3, os; c = sqlite3.connect(os.path.expanduser('~/.polymarket-paper/weather_edge.db')); print(*c.execute('SELECT * FROM advisor_suggestion_applies ORDER BY ts DESC LIMIT 10'), sep='\n')"

# Run advisor on-demand
# (Click "Run Advisor Now" at /advisor, OR via CLI:)
python polymarket-analyzer\scripts\weather_strategy_advisor.py --once --since-days 30

# Run backtest with friction
python polymarket-analyzer\scripts\weather_edge_backtest.py --since-days 30 --slippage-pct 2 --fee-rate 0.02 --top-k 10
```

---

## Related docs

- `CLAUDE.md` — constitutional risk rules (overrides all)
- `polymarket-analyzer/references/strategy-advisor-prompt.md` — what the advisor LLM is told
- `polymarket-analyzer/references/weather-judge-prompt.md` — the judge's prompt (target of `judge_prompt` suggestions)
- `polymarket-analyzer/references/weather-edge-strategy.md` — the trading strategy the advisor is tuning
