#!/usr/bin/env python3
"""Offline tests for clear_tennis.py — deletes ONLY tennis rows, dry-run safe, backs up."""

import glob
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clear_tennis as ct  # noqa: E402


def _seed(db: str, table: str, rows: list[tuple]) -> None:
    con = sqlite3.connect(db)
    with con:
        con.execute(f"CREATE TABLE {table} (key TEXT PRIMARY KEY, category TEXT)")
        con.executemany(f"INSERT INTO {table}(key, category) VALUES(?,?)", rows)
    con.close()


def _categories(db: str, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in con.execute(f"SELECT category FROM {table}"))
    finally:
        con.close()


class TestClearTennis(unittest.TestCase):
    def _db(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "entries.db")
        _seed(db, "entries", [
            ("k1", "Tennis"), ("k2", "Tênis"), ("k3", "Soccer"),
            ("k4", "Basketball"), ("k5", "Tennis")])
        return db

    def test_dry_run_changes_nothing(self):
        db = self._db()
        ct._clean_one("Sports", db, "entries", apply=False)
        self.assertEqual(_categories(db, "entries"),
                         ["Basketball", "Soccer", "Tennis", "Tennis", "Tênis"])
        self.assertEqual(glob.glob(f"{db}.*.bak"), [])          # no backup on dry-run

    def test_apply_deletes_only_tennis_and_backs_up(self):
        db = self._db()
        ct._clean_one("Sports", db, "entries", apply=True)
        self.assertEqual(_categories(db, "entries"), ["Basketball", "Soccer"])  # tennis gone (both spellings)
        self.assertEqual(len(glob.glob(f"{db}.*.bak")), 1)      # backup made
        # the backup still holds the original 5 rows
        bak = glob.glob(f"{db}.*.bak")[0]
        self.assertEqual(len(_categories(bak, "entries")), 5)

    def test_missing_db_is_noop(self):
        ct._clean_one("Sports", "/no/such/entries.db", "entries", apply=True)   # must not raise

    def test_missing_table_skipped(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "x.db")
        sqlite3.connect(db).close()                            # empty DB, no wallet_bets table
        ct._clean_one("Wallets", db, "wallet_bets", apply=True)  # must not raise
        self.assertEqual(glob.glob(f"{db}.*.bak"), [])

    def test_wallet_bets_table(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "wallets.db")
        _seed(db, "wallet_bets", [("a", "Tennis"), ("b", "Soccer")])
        ct._clean_one("Wallets", db, "wallet_bets", apply=True)
        self.assertEqual(_categories(db, "wallet_bets"), ["Soccer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
