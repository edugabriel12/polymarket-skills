"""Settings service — read/write the 5 critical live trading env vars
in `agent/.env`. Preserves comment + ordering; chmod 600 after write.

Editable vars (only — see ALLOWED below):
  POLYMARKET_PRIVATE_KEY    — burner wallet, write-only via UI
  POLYMARKET_CONFIRM        — true|false safety gate
  POLYMARKET_MAX_SIZE       — per-trade USD cap
  POLYMARKET_DAILY_LOSS_LIMIT — daily spend cap
  POLYMARKET_AUTO_CONFIRM   — true|false autonomous mode

Anything else is rejected. Operator edits agent/.env directly for
non-live vars.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / "agent" / ".env"
ENV_EXAMPLE = REPO_ROOT / "agent" / ".env.example"


ALLOWED_VARS = {
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_CONFIRM",
    "POLYMARKET_MAX_SIZE",
    "POLYMARKET_DAILY_LOSS_LIMIT",
    "POLYMARKET_AUTO_CONFIRM",
    # v9: ladder strategy env vars (bot picks these up at startup,
    # overriding argparse defaults).
    "LADDER_MODE",
    "LADDER_STAKE_SPLIT",
    "LADDER_MIN_LEG_PRICE",
    "LADDER_MIN_LEG_EDGE_PP",
    "LADDER_EXECUTE_MIN_LEG_EDGE_PP",
    "LADDER_MIN_TTR_HOURS",
}


SECRET_VARS = {"POLYMARKET_PRIVATE_KEY"}

# v9: ladder vars need restart of weather_edge_bot to take effect.
RESTART_REQUIRED_VARS = {
    "LADDER_MODE",
    "LADDER_STAKE_SPLIT",
    "LADDER_MIN_LEG_PRICE",
    "LADDER_MIN_LEG_EDGE_PP",
    "LADDER_EXECUTE_MIN_LEG_EDGE_PP",
    "LADDER_MIN_TTR_HOURS",
}


def mask_secret(value: Optional[str]) -> str:
    """Return safe display: '0x****<last4>' for keys, '(unset)' if None."""
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "****"
    return f"{value[:2]}****{value[-4:]}"


def _validate(key: str, value: str) -> None:
    """Raise ValueError if (key, value) is invalid."""
    if key not in ALLOWED_VARS:
        raise ValueError(f"Var {key!r} is not editable via UI. "
                         f"Allowed: {sorted(ALLOWED_VARS)}")
    if value == "":
        # Allow empty (unset) for booleans / unlinking key
        return
    if key == "POLYMARKET_PRIVATE_KEY":
        if not re.match(r"^0x[a-fA-F0-9]{64}$", value):
            raise ValueError("POLYMARKET_PRIVATE_KEY must match "
                             "^0x[a-fA-F0-9]{64}$ (0x + 64 hex chars)")
    elif key in ("POLYMARKET_CONFIRM", "POLYMARKET_AUTO_CONFIRM"):
        if value.lower() not in ("true", "false"):
            raise ValueError(f"{key} must be 'true' or 'false'")
    elif key in ("POLYMARKET_MAX_SIZE", "POLYMARKET_DAILY_LOSS_LIMIT"):
        try:
            f = float(value)
            if f <= 0:
                raise ValueError("must be > 0")
        except ValueError as e:
            raise ValueError(f"{key} must be a positive number ({e})")
    # v9 ladder vars
    elif key == "LADDER_MODE":
        if value not in ("off", "3bin"):
            raise ValueError("LADDER_MODE must be 'off' or '3bin'")
    elif key == "LADDER_STAKE_SPLIT":
        if value not in ("kelly", "equal"):
            raise ValueError("LADDER_STAKE_SPLIT must be 'kelly' or 'equal'")
    elif key == "LADDER_MIN_LEG_PRICE":
        try:
            f = float(value)
            if not (0 <= f <= 1):
                raise ValueError("must be in [0, 1]")
        except ValueError as e:
            raise ValueError(f"{key} must be a number in [0, 1] ({e})")
    elif key in ("LADDER_MIN_LEG_EDGE_PP",
                  "LADDER_EXECUTE_MIN_LEG_EDGE_PP",
                  "LADDER_MIN_TTR_HOURS"):
        try:
            f = float(value)
            if f < 0:
                raise ValueError("must be >= 0")
        except ValueError as e:
            raise ValueError(f"{key} must be a non-negative number ({e})")


def read_env_file(path: Path = None) -> dict[str, str]:
    """Parse the .env file (or .env.example if .env missing).
    Returns key→value dict. Comments and blank lines ignored."""
    p = path or ENV_FILE
    if not p.exists():
        # Fall back to example so the UI shows defaults
        p = ENV_EXAMPLE
        if not p.exists():
            return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def get_displayable_settings() -> dict:
    """Return current state of all editable vars, with secrets masked."""
    env = read_env_file()
    out = {}
    # Order: live trading vars first, then ladder vars
    live_vars = sorted(v for v in ALLOWED_VARS if v.startswith("POLYMARKET_"))
    ladder_vars = sorted(v for v in ALLOWED_VARS if v.startswith("LADDER_"))
    for k in live_vars + ladder_vars:
        v = env.get(k, "")
        out[k] = {
            "key": k,
            "value": v,
            "is_secret": k in SECRET_VARS,
            "display": mask_secret(v) if k in SECRET_VARS else (v or "(unset)"),
            "is_set": bool(v),
            "restart_required": k in RESTART_REQUIRED_VARS,
        }
    return out


def update_env_var(key: str, value: str) -> dict:
    """Validate + write a single var to agent/.env. Preserves order +
    comments. Updates in-place if key exists, appends otherwise.
    Sets chmod 0o600 after write.

    Returns {status: 'ok', key, masked_value} on success.
    Raises ValueError on validation failure.
    """
    _validate(key, value)

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    else:
        # Bootstrap from .env.example if available, else empty
        if ENV_EXAMPLE.exists():
            lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        else:
            lines = []

    new_line = f"{key}={value}"
    found = False
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith("#") or "=" not in s:
            continue
        k = s.partition("=")[0].strip()
        if k == key:
            lines[i] = new_line
            found = True
            break
    if not found:
        # Append with a leading blank for readability
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new_line)

    # Atomic write: tmp → rename
    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # chmod 600 BEFORE rename so the protected perms are atomic too
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(ENV_FILE)

    return {
        "status": "ok",
        "key": key,
        "masked_value": mask_secret(value) if key in SECRET_VARS else value,
    }


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import sys as _sys

    tmp = Path(tempfile.mkdtemp())
    test_env = tmp / ".env"
    test_env.write_text(
        "# leading comment\n"
        "POLYMARKET_MAX_SIZE=5.0\n"
        "\n"
        "# inline section\n"
        "ANTHROPIC_API_KEY=existing-key\n"
    )

    _self = _sys.modules[__name__]
    _self.ENV_FILE = test_env
    _self.ENV_EXAMPLE = test_env  # alias for fallback

    # Test 1: read_env_file parses key=value, ignores comments
    parsed = read_env_file(test_env)
    assert parsed == {"POLYMARKET_MAX_SIZE": "5.0",
                      "ANTHROPIC_API_KEY": "existing-key"}, parsed
    print(f"Test 1 PASS: read_env_file → {parsed}")

    # Test 2: update_env_var adds new key if missing
    update_env_var("POLYMARKET_AUTO_CONFIRM", "true")
    after = read_env_file(test_env)
    assert after.get("POLYMARKET_AUTO_CONFIRM") == "true"
    # Existing keys preserved
    assert after.get("POLYMARKET_MAX_SIZE") == "5.0"
    assert after.get("ANTHROPIC_API_KEY") == "existing-key"
    print(f"Test 2 PASS: added POLYMARKET_AUTO_CONFIRM=true; existing preserved")

    # Test 3: update existing key in place + comments preserved
    update_env_var("POLYMARKET_MAX_SIZE", "10.0")
    text = test_env.read_text()
    assert "POLYMARKET_MAX_SIZE=10.0" in text
    assert "# leading comment" in text
    assert "# inline section" in text
    # And only one line for the key (no duplication)
    assert text.count("POLYMARKET_MAX_SIZE=") == 1
    print(f"Test 3 PASS: updated in place, comments preserved, no dupes")

    # Test 4: mask_secret
    assert mask_secret(None) == "(unset)"
    assert mask_secret("") == "(unset)"
    assert mask_secret("0x12345abcd6789") == "0x****6789"
    assert mask_secret("short") == "****"
    print("Test 4 PASS: mask_secret variants")

    # Test 5: validation rejects bad inputs
    bad_cases = [
        ("POLYMARKET_PRIVATE_KEY", "0xdeadbeef"),  # too short
        ("POLYMARKET_PRIVATE_KEY", "deadbeef" * 8),  # missing 0x
        ("POLYMARKET_CONFIRM", "yes"),  # not true/false
        ("POLYMARKET_MAX_SIZE", "-5"),  # negative
        ("POLYMARKET_DAILY_LOSS_LIMIT", "abc"),  # non-numeric
        ("ANTHROPIC_API_KEY", "anything"),  # not in ALLOWED
    ]
    for k, v in bad_cases:
        try:
            update_env_var(k, v)
            assert False, f"should have raised: {k}={v}"
        except ValueError:
            pass
    print(f"Test 5 PASS: rejected {len(bad_cases)} invalid inputs")

    # Test 6: chmod 600 on write
    import os as _os
    mode = _os.stat(test_env).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    print(f"Test 6 PASS: file mode is {oct(mode)} (owner-only)")

    print("\nAll settings_service tests PASS")
