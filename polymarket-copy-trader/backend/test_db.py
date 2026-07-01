"""Schema / CRUD / cascade tests for the copy-trader DB. Run: `python test_db.py`."""
from __future__ import annotations

import os
import tempfile

import db


def _fresh() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    return path


def test_starting_cash():
    path = _fresh()
    assert db.get_cash(path) == db.STARTING_BALANCE
    st = db.get_paper_state(path)
    assert st["starting_balance"] == db.STARTING_BALANCE
    print("ok test_starting_cash")


def test_wallet_crud_and_unique():
    path = _fresh()
    w = db.add_wallet("Whale", "0x" + "b" * 40, baseline_ts=123.0, db_path=path)
    assert w["id"] and w["baseline_ts"] == 123.0
    assert db.get_wallet_by_address("0X" + "B" * 40, path)  # case-insensitive
    dup = None
    try:
        db.add_wallet("Dup", "0x" + "b" * 40, db_path=path)
    except Exception as e:  # noqa: BLE001
        dup = e
    assert dup is not None, "expected UNIQUE violation on duplicate address"
    print("ok test_wallet_crud_and_unique")


def test_entry_fk_cascade():
    path = _fresh()
    wid = db.add_wallet("W", "0x" + "c" * 40, db_path=path)["id"]
    db.insert_entry({
        "wallet_id": wid, "condition_id": "0xc", "copy_action": "BUY",
        "status": "EXECUTED", "result_status": "OPEN", "executed_usd": 42.0,
        "shares": 10.0, "slippage_pct": 0.05, "realized_pnl": None,
    }, path)
    assert db.list_entries(wallet_id=wid, db_path=path)["total"] == 1
    db.delete_wallet(wid, path)
    # Cascade: entries for the wallet are gone.
    assert db.list_entries(db_path=path)["total"] == 0
    print("ok test_entry_fk_cascade")


def test_cash_adjust_and_reset():
    path = _fresh()
    wid = db.add_wallet("W", "0x" + "d" * 40, db_path=path)["id"]
    db.adjust_cash(-100.0, path)
    assert abs(db.get_cash(path) - (db.STARTING_BALANCE - 100.0)) < 1e-6
    db.upsert_paper_position(wid, "0xc", {"shares": 5.0, "avg_entry": 0.5,
                                          "closed": 0}, path)
    db.insert_entry({"wallet_id": wid, "condition_id": "0xc", "copy_action": "BUY",
                     "status": "EXECUTED", "result_status": "OPEN"}, path)
    db.reset_paper(path)
    assert db.get_cash(path) == db.STARTING_BALANCE
    assert db.list_entries(db_path=path)["total"] == 0
    assert len(db.list_open_paper_positions(path)) == 0
    print("ok test_cash_adjust_and_reset")


def test_holdings_upsert():
    path = _fresh()
    wid = db.add_wallet("W", "0x" + "e" * 40, db_path=path)["id"]
    assert db.get_holding(wid, "0xc", path)["shares"] == 0.0
    db.set_holding(wid, "0xc", 100.0, 0.5, path)
    db.set_holding(wid, "0xc", 150.0, 0.55, path)
    h = db.get_holding(wid, "0xc", path)
    assert h["shares"] == 150.0 and abs(h["avg_price"] - 0.55) < 1e-9
    print("ok test_holdings_upsert")


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL DB TESTS PASSED")


if __name__ == "__main__":
    _run_all()
