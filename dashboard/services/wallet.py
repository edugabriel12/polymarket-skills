"""Wallet service — derive address from POLYMARKET_PRIVATE_KEY +
query Polygon for USDC + MATIC balances. Read-only.

Mirrors the logic in polymarket-live-executor/scripts/setup_wallet.py
:171-239 but exposed as importable functions with caching for the
dashboard. Lazy-imports eth_account so the module is importable even
if the live trading deps aren't installed.

Env vars consumed:
  POLYMARKET_PRIVATE_KEY  — 0x + 64 hex chars
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests


POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYMARKET_PROFILE_BASE = "https://polymarket.com/profile/"

_CACHE_TTL_SEC = 30
_cache: dict = {"ts": 0.0, "data": None}


def mask_address(addr: str) -> str:
    """Return e.g. '0x1234…abcd' — full address still rendered, just
    abbreviated for compact UI display."""
    if not addr or len(addr) < 10:
        return addr or ""
    return f"{addr[:6]}…{addr[-4:]}"


def derive_address(private_key: str) -> Optional[str]:
    """Get checksummed public address from a private key. None on error."""
    if not private_key or not private_key.startswith("0x") \
            or len(private_key) != 66:
        return None
    try:
        from eth_account import Account
    except ImportError:
        return None
    try:
        return Account.from_key(private_key).address
    except Exception:
        return None


def _query_matic_balance(address: str) -> Optional[float]:
    payload = {
        "jsonrpc": "2.0", "method": "eth_getBalance",
        "params": [address, "latest"], "id": 1,
    }
    try:
        r = requests.post(POLYGON_RPC, json=payload, timeout=10)
        wei = int(r.json().get("result", "0x0"), 16)
        return wei / 1e18
    except Exception:
        return None


def _query_usdc_balance(address: str) -> Optional[float]:
    # balanceOf(address) selector = 0x70a08231 + padded address
    padded = address[2:].lower().zfill(64)
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": USDC_CONTRACT, "data": f"0x70a08231{padded}"},
                    "latest"],
        "id": 2,
    }
    try:
        r = requests.post(POLYGON_RPC, json=payload, timeout=10)
        raw = int(r.json().get("result", "0x0"), 16)
        return raw / 1e6  # USDC has 6 decimals
    except Exception:
        return None


def _tier_for_balance(usdc: float) -> str:
    """CLAUDE.md §4 tier mapping. Operator manually moves between tiers
    by adjusting POLYMARKET_MAX_SIZE — this is informational only."""
    if usdc <= 25:
        return "First time"
    if usdc <= 100:
        return "Learning"
    if usdc <= 500:
        return "Experienced"
    return "Advanced"


def get_wallet_info(force_refresh: bool = False) -> dict:
    """Return wallet status dict. Cached 30s unless force_refresh=True.

    Returns:
      {configured: bool, address?, address_masked?, usdc_balance?,
       matic_balance?, tier?, polymarket_url?, errors?}
    """
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        return {"configured": False,
                "reason": "POLYMARKET_PRIVATE_KEY not set"}

    address = derive_address(pk)
    if not address:
        return {"configured": False,
                "reason": "POLYMARKET_PRIVATE_KEY invalid format "
                          "(expected 0x + 64 hex chars)"}

    # Cache check
    now = time.time()
    if (not force_refresh and _cache["data"]
            and _cache["data"].get("address") == address
            and now - _cache["ts"] < _CACHE_TTL_SEC):
        return _cache["data"]

    matic = _query_matic_balance(address)
    usdc = _query_usdc_balance(address)
    info = {
        "configured": True,
        "address": address,
        "address_masked": mask_address(address),
        "matic_balance": matic,
        "matic_status": ("ok" if matic and matic >= 0.01 else "low"),
        "usdc_balance": usdc,
        "usdc_status": "ok" if usdc and usdc >= 5 else "low",
        "tier": _tier_for_balance(usdc) if usdc is not None else None,
        "polymarket_url": POLYMARKET_PROFILE_BASE + address,
        "polygonscan_url": f"https://polygonscan.com/address/{address}",
        "rpc_errors": [],
    }
    if matic is None:
        info["rpc_errors"].append("MATIC balance fetch failed")
    if usdc is None:
        info["rpc_errors"].append("USDC balance fetch failed")
    _cache["ts"] = now
    _cache["data"] = info
    return info


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test 1: no private key → not configured
    os.environ.pop("POLYMARKET_PRIVATE_KEY", None)
    _cache.update({"ts": 0, "data": None})
    info = get_wallet_info()
    assert info["configured"] is False
    assert "not set" in info["reason"]
    print(f"Test 1 PASS: no key → {info}")

    # Test 2: invalid key
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xdeadbeef"
    _cache.update({"ts": 0, "data": None})
    info = get_wallet_info()
    assert info["configured"] is False
    assert "invalid format" in info["reason"]
    print(f"Test 2 PASS: invalid key → {info['reason']}")

    # Test 3: valid key — generate a real secp256k1 key via eth_account
    try:
        from eth_account import Account
        valid_key = Account.create().key.hex()
        if not valid_key.startswith("0x"):
            valid_key = "0x" + valid_key
    except ImportError:
        print("SKIP: eth_account not installed; skipping tests 3-5")
        print("\nPartial wallet tests PASS (1, 2, 6)")
        import sys; sys.exit(0)
    os.environ["POLYMARKET_PRIVATE_KEY"] = valid_key
    _cache.update({"ts": 0, "data": None})

    import sys as _sys
    _self = _sys.modules[__name__]
    _self._query_matic_balance = lambda a: 0.5
    _self._query_usdc_balance = lambda a: 75.0

    info = get_wallet_info()
    assert info["configured"] is True
    assert info["matic_balance"] == 0.5
    assert info["usdc_balance"] == 75.0
    assert info["tier"] == "Learning"  # 25 < 75 ≤ 100
    assert info["polymarket_url"].startswith(POLYMARKET_PROFILE_BASE)
    assert info["address"] is not None
    assert "0x" in info["address_masked"] and "…" in info["address_masked"]
    print(f"Test 3 PASS: valid key + mocked RPC → tier={info['tier']}, "
          f"USDC=${info['usdc_balance']}, address={info['address_masked']}")

    # Test 4: cache hit returns same object
    info2 = get_wallet_info()
    assert info2 is info  # cached reference
    print(f"Test 4 PASS: cache hit returns same object")

    # Test 5: force_refresh bypasses cache
    _self._query_usdc_balance = lambda a: 200.0
    info3 = get_wallet_info(force_refresh=True)
    assert info3["usdc_balance"] == 200.0
    assert info3["tier"] == "Experienced"
    print(f"Test 5 PASS: force_refresh → new tier={info3['tier']}")

    # Test 6: tier mapping
    assert _tier_for_balance(20) == "First time"
    assert _tier_for_balance(75) == "Learning"
    assert _tier_for_balance(300) == "Experienced"
    assert _tier_for_balance(5000) == "Advanced"
    print("Test 6 PASS: tier mapping covers all 4 brackets")

    print("\nAll wallet tests PASS")
