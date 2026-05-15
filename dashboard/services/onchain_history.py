"""On-chain transaction history — query Polygonscan for the wallet's
recent activity (native tx, USDC transfers, Polymarket contract
interactions). Read-only.

Categorizes each tx so the UI can color-code:
  - usdc_in / usdc_out  : ERC20 USDC.e transfers
  - polymarket          : interaction with CTF/neg-risk/conditional contracts
  - approval            : ERC20 approve() call
  - native              : MATIC transfer (gas refund / funding)
  - other               : anything else (rare)

Env vars:
  POLYGONSCAN_API_KEY  — optional. Without it, free tier (5 req/sec,
                          100k/day shared). With it, much higher caps.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests


POLYGONSCAN_API = "https://api.polygonscan.com/api"

# USDC.e (bridged USDC, the one Polymarket uses on Polygon)
USDC_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

# Polymarket protocol contracts (lowercased for compare)
POLYMARKET_CONTRACTS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": "CTF Exchange",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": "Neg-Risk Exchange",
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045": "Conditional Tokens",
}

# Cache: {(address_lower, limit): (ts, data)}
_cache: dict = {}
_CACHE_TTL_SEC = 180  # 3 min — on-chain doesn't change every refresh


def _api_key() -> str:
    return os.environ.get("POLYGONSCAN_API_KEY", "")


def _categorize(tx: dict, address_lower: str) -> str:
    """Classify a tx into one of the known buckets."""
    to = (tx.get("to") or "").lower()
    contract = (tx.get("contractAddress") or "").lower()
    token_symbol = (tx.get("tokenSymbol") or "").upper()
    input_data = tx.get("input") or ""

    # ERC20 transfer entries have contractAddress + tokenSymbol set
    if token_symbol in ("USDC", "USDC.E") or contract == USDC_CONTRACT:
        return "usdc_in" if to == address_lower else "usdc_out"

    # Direct call to a Polymarket contract
    if to in POLYMARKET_CONTRACTS:
        return "polymarket"

    # ERC20 approve() selector = 0x095ea7b3
    if isinstance(input_data, str) and input_data.startswith("0x095ea7b3"):
        return "approval"

    # Pure MATIC transfer
    if input_data in ("", "0x"):
        value_wei = int(tx.get("value", 0) or 0)
        if value_wei > 0:
            return "native"

    return "other"


def _format_amount(tx: dict, category: str) -> Optional[str]:
    """Format a human-readable amount string for display."""
    if category in ("usdc_in", "usdc_out"):
        # ERC20 entries: value is raw token units, decimals from tokenDecimal
        try:
            decimals = int(tx.get("tokenDecimal", 6))
            raw = int(tx.get("value", 0))
            return f"${raw / (10 ** decimals):.4f}"
        except (ValueError, TypeError):
            return None
    if category == "native":
        try:
            wei = int(tx.get("value", 0))
            return f"{wei / 1e18:.4f} MATIC"
        except (ValueError, TypeError):
            return None
    if category == "approval":
        return "approve()"
    return None


def _fetch_native_txs(address: str, limit: int) -> list[dict]:
    params = {
        "module": "account", "action": "txlist", "address": address,
        "startblock": 0, "endblock": 99999999, "sort": "desc",
        "page": 1, "offset": limit,
    }
    if _api_key():
        params["apikey"] = _api_key()
    try:
        r = requests.get(POLYGONSCAN_API, params=params, timeout=15)
        result = r.json().get("result", [])
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _fetch_erc20_txs(address: str, limit: int) -> list[dict]:
    """Fetch USDC.e transfers specifically — most relevant for Polymarket."""
    params = {
        "module": "account", "action": "tokentx", "address": address,
        "contractaddress": USDC_CONTRACT, "sort": "desc",
        "page": 1, "offset": limit,
    }
    if _api_key():
        params["apikey"] = _api_key()
    try:
        r = requests.get(POLYGONSCAN_API, params=params, timeout=15)
        result = r.json().get("result", [])
        return result if isinstance(result, list) else []
    except Exception:
        return []


def get_transactions(address: str, limit: int = 50,
                      force_refresh: bool = False) -> dict:
    """Return the wallet's recent on-chain activity, categorized.

    Returns:
      {
        "configured": True,
        "address": str,
        "n_total": int,
        "transactions": list[dict],   # most recent first
        "rpc_errors": list[str],
        "cached_at_iso": str,
      }

    Each transaction dict contains:
      hash, ts_iso, category, amount, counterparty, polygonscan_url,
      gas_used, contract_label (if applicable)
    """
    if not address:
        return {"configured": False, "reason": "no wallet address"}

    addr_lower = address.lower()
    cache_key = (addr_lower, limit)
    now = time.time()
    if not force_refresh and cache_key in _cache:
        cached_ts, cached_data = _cache[cache_key]
        if now - cached_ts < _CACHE_TTL_SEC:
            return cached_data

    errors = []
    native = _fetch_native_txs(address, limit)
    if not native:
        errors.append("Polygonscan native tx fetch returned empty (could "
                       "be rate limit, no tx, or transient API error)")
    erc20 = _fetch_erc20_txs(address, limit)

    # Merge — same hash may appear in both lists. Native carries gasUsed
    # and input data; erc20 carries token info.
    merged: dict = {}
    for tx in native:
        h = tx.get("hash")
        if h:
            merged[h] = {**tx, "_kind": "native"}
    for tx in erc20:
        h = tx.get("hash")
        if not h:
            continue
        if h in merged:
            # Annotate with token fields
            merged[h]["tokenSymbol"] = tx.get("tokenSymbol")
            merged[h]["tokenDecimal"] = tx.get("tokenDecimal")
            # Don't overwrite input data; ERC20 entries have value in
            # raw token units which we want for amount formatting.
            merged[h]["_token_value_raw"] = tx.get("value")
        else:
            merged[h] = {**tx, "_kind": "erc20"}

    # Sort by timeStamp DESC, take top `limit`
    txs = sorted(merged.values(),
                  key=lambda t: int(t.get("timeStamp", 0) or 0),
                  reverse=True)[:limit]

    out = []
    for tx in txs:
        try:
            ts = int(tx.get("timeStamp", 0))
            ts_iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        except (ValueError, TypeError):
            ts_iso = ""

        category = _categorize(tx, addr_lower)

        # Counterparty: the other side of the tx
        from_addr = (tx.get("from") or "").lower()
        to_addr = (tx.get("to") or "").lower()
        if from_addr == addr_lower:
            cp = to_addr
        else:
            cp = from_addr
        cp_label = POLYMARKET_CONTRACTS.get(cp)
        if not cp_label and cp == USDC_CONTRACT:
            cp_label = "USDC.e contract"

        # For ERC20 entries we already have token value separately
        amount_tx = tx.copy()
        if "_token_value_raw" in tx:
            amount_tx["value"] = tx["_token_value_raw"]

        out.append({
            "hash": tx.get("hash"),
            "hash_short": (tx.get("hash") or "")[:10] + "…",
            "ts_iso": ts_iso,
            "block_number": tx.get("blockNumber"),
            "category": category,
            "amount": _format_amount(amount_tx, category),
            "counterparty": cp,
            "counterparty_label": cp_label,
            "counterparty_short": (cp[:6] + "…" + cp[-4:]) if cp else "",
            "gas_used": tx.get("gasUsed"),
            "is_error": tx.get("isError") == "1",
            "polygonscan_url": f"https://polygonscan.com/tx/{tx.get('hash')}",
        })

    data = {
        "configured": True,
        "address": address,
        "n_total": len(out),
        "transactions": out,
        "rpc_errors": errors if (not native and not erc20) else [],
        "cached_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "polygonscan_address_url":
            f"https://polygonscan.com/address/{address}",
    }
    _cache[cache_key] = (now, data)
    return data


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test 1: empty address → not configured
    r = get_transactions("")
    assert r["configured"] is False
    print(f"Test 1 PASS: empty address → not configured")

    # Test 2: categorize various tx shapes
    addr = "0x" + "a" * 40
    addr_l = addr.lower()
    cases = [
        ({"to": addr_l, "from": "0xdead", "tokenSymbol": "USDC",
          "contractAddress": USDC_CONTRACT}, "usdc_in"),
        ({"to": "0xdead", "from": addr_l, "tokenSymbol": "USDC.e",
          "contractAddress": USDC_CONTRACT}, "usdc_out"),
        ({"to": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e", "from": addr_l,
          "input": "0x12345678"}, "polymarket"),
        ({"to": USDC_CONTRACT, "from": addr_l,
          "input": "0x095ea7b3000000000000000000000000abcd"},
         "approval"),
        ({"to": "0xdead", "from": addr_l, "input": "", "value": "1000000000000000000"},
         "native"),
        ({"to": "0xrandom", "from": addr_l, "input": "0xfeedface"}, "other"),
    ]
    for tx, expected in cases:
        got = _categorize(tx, addr_l)
        assert got == expected, f"{tx} → expected {expected}, got {got}"
    print(f"Test 2 PASS: categorize {len(cases)} tx shapes correctly")

    # Test 3: format_amount for USDC and MATIC
    usdc_tx = {"value": "5000000", "tokenDecimal": "6"}
    assert _format_amount(usdc_tx, "usdc_in") == "$5.0000"
    matic_tx = {"value": str(int(0.1 * 1e18))}
    assert _format_amount(matic_tx, "native") == "0.1000 MATIC"
    assert _format_amount({}, "approval") == "approve()"
    assert _format_amount({}, "other") is None
    print(f"Test 3 PASS: format_amount USDC + MATIC + edge cases")

    # Test 4: cache hit returns same dict
    import sys as _sys
    _self = _sys.modules[__name__]

    # Mock both fetchers
    fake_native = [{
        "hash": "0xabc123", "timeStamp": "1715000000",
        "from": addr_l, "to": "0xdest",
        "value": "0", "input": "0x12345678",
        "blockNumber": "100", "gasUsed": "21000", "isError": "0",
    }]
    fake_erc20 = [{
        "hash": "0xdef456", "timeStamp": "1715001000",
        "from": "0xdead", "to": addr_l,
        "tokenSymbol": "USDC", "tokenDecimal": "6",
        "contractAddress": USDC_CONTRACT, "value": "10000000",
        "blockNumber": "101",
    }]
    _self._fetch_native_txs = lambda *a, **kw: fake_native
    _self._fetch_erc20_txs = lambda *a, **kw: fake_erc20

    # Clear cache before mocked test
    _cache.clear()
    r = get_transactions(addr, limit=10)
    assert r["configured"] is True
    assert r["n_total"] == 2
    # Most recent first: erc20 ts > native ts
    assert r["transactions"][0]["hash"] == "0xdef456"
    assert r["transactions"][0]["category"] == "usdc_in"
    assert r["transactions"][0]["amount"] == "$10.0000"
    assert r["transactions"][1]["hash"] == "0xabc123"
    print(f"Test 4 PASS: get_transactions merges + sorts + categorizes "
          f"({r['n_total']} tx)")

    # Test 5: cache hit returns same object
    r2 = get_transactions(addr, limit=10)
    assert r2 is r
    print(f"Test 5 PASS: cache hit returns same dict reference")

    # Test 6: force_refresh bypasses cache
    fake_native_2 = []
    fake_erc20_2 = []
    _self._fetch_native_txs = lambda *a, **kw: fake_native_2
    _self._fetch_erc20_txs = lambda *a, **kw: fake_erc20_2
    r3 = get_transactions(addr, limit=10, force_refresh=True)
    assert r3["n_total"] == 0
    assert "rpc_errors" in r3 and len(r3["rpc_errors"]) > 0
    print(f"Test 6 PASS: force_refresh bypasses + empty result handled")

    # Test 7: Polymarket counterparty labeling
    fake_native = [{
        "hash": "0xpm1", "timeStamp": "1715002000",
        "from": addr_l, "to": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        "value": "0", "input": "0xdeadbeef",
        "blockNumber": "200",
    }]
    _self._fetch_native_txs = lambda *a, **kw: fake_native
    _self._fetch_erc20_txs = lambda *a, **kw: []
    _cache.clear()
    r = get_transactions(addr)
    tx = r["transactions"][0]
    assert tx["category"] == "polymarket"
    assert tx["counterparty_label"] == "CTF Exchange"
    print(f"Test 7 PASS: Polymarket contract labeled '{tx['counterparty_label']}'")

    print("\nAll onchain_history tests PASS")
