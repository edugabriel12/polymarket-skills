#!/usr/bin/env python3
"""Offline tests for the tennis-data.co.uk match feed (no network).

Builds real (minimal) .xlsx bytes in-memory to exercise the pure-stdlib reader, then the
row->match mapping, the 'Surname I.' name handling, surname aliasing, and the date parsing.

Run: python polymarket-tennis/scripts/test_tennis_data_source.py
"""

from __future__ import annotations

import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_data_source as tds  # noqa: E402
import ratings_source as rs  # noqa: E402
import ratings as rmod  # noqa: E402

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _make_xlsx(grid: list[list]) -> bytes:
    """Minimal but real .xlsx: shared strings for str cells, numeric for int/float cells.

    Only the parts our reader touches are emitted ([Content_Types].xml marker,
    xl/sharedStrings.xml, xl/worksheets/sheet1.xml) — enough for read_xlsx, not Excel.
    """
    strings: list[str] = []
    sidx: dict[str, int] = {}

    def col_letter(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(ord("A") + r) + s
        return s

    rows_xml = []
    for ri, row in enumerate(grid, start=1):
        cells = []
        for ci, val in enumerate(row):
            ref = f"{col_letter(ci)}{ri}"
            if isinstance(val, str):
                if val not in sidx:
                    sidx[val] = len(strings)
                    strings.append(val)
                cells.append(f'<c r="{ref}" t="s"><v>{sidx[val]}</v></c>')
            else:  # numeric (e.g. an Excel date serial)
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
        rows_xml.append(f"<row r=\"{ri}\">{''.join(cells)}</row>")

    sst_items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared_xml = f'<sst xmlns="{_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">{sst_items}</sst>'
    sheet_xml = f'<worksheet xmlns="{_NS}"><sheetData>{"".join(rows_xml)}</sheetData></worksheet>'

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


# A realistic tennis-data layout: the modeled columns are NOT first, so header-name lookup
# (not fixed position) is what must locate them. Names are 'Surname I.' as the site writes them.
_HEADER = ["ATP", "Location", "Tournament", "Date", "Series", "Court", "Surface",
           "Round", "Best of", "Winner", "Loser", "WRank", "LRank"]


def _row(date, surface, winner, loser):
    return [1, "London", "Champ", date, "ATP500", "Outdoor", surface,
            "Final", 3, winner, loser, 1, 2]


class TestXlsxReader(unittest.TestCase):
    def test_reads_shared_strings_and_numbers(self):
        grid = [_HEADER, _row(45450, "Clay", "Alcaraz C.", "Sinner J.")]
        rows = tds.read_xlsx(_make_xlsx(grid))
        self.assertEqual(rows[0], _HEADER)
        self.assertEqual(rows[1][9], "Alcaraz C.")     # Winner column resolved from shared strings
        self.assertEqual(rows[1][6], "Clay")
        self.assertEqual(rows[1][3], "45450")          # numeric date serial preserved as text

    def test_handles_zip_wrapping_xlsx(self):
        # tennis-data serves {year}.zip -> {year}.xlsx; the reader must peel the outer zip.
        inner = _make_xlsx([_HEADER, _row(45450, "Hard", "Nadal R.", "Federer R.")])
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as zf:
            zf.writestr("2024.xlsx", inner)
        rows = tds.read_xlsx(outer.getvalue())
        self.assertEqual(rows[1][9], "Nadal R.")

    def test_non_workbook_bytes_return_empty(self):
        self.assertEqual(tds.read_xlsx(b"not a zip"), [])
        self.assertEqual(tds.read_xlsx(b""), [])


class TestRowMapping(unittest.TestCase):
    def test_locates_columns_by_header_name(self):
        grid = [_HEADER,
                _row("07/06/2024", "Clay", "Alcaraz C.", "Sinner J."),
                _row("08/06/2024", "Grass", "Sinner J.", "Alcaraz C.")]
        matches = tds.rows_to_matches(tds.read_xlsx(_make_xlsx(grid)))
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["winner"], "Alcaraz C.")
        self.assertEqual(matches[0]["surface"], "clay")
        self.assertEqual(matches[0]["date"], "20240607")

    def test_drops_rows_without_players_and_maps_carpet_to_hard(self):
        grid = [_HEADER,
                _row("07/06/2024", "Carpet", "Player A.", "Player B."),
                _row("08/06/2024", "Hard", "", "Player B.")]   # missing winner -> dropped
        matches = tds.rows_to_matches(tds.read_xlsx(_make_xlsx(grid)))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["surface"], "hard")        # carpet folded into hard

    def test_empty_and_headerless(self):
        self.assertEqual(tds.rows_to_matches([]), [])
        self.assertEqual(tds.rows_to_matches([["foo", "bar"]]), [])  # no Winner/Loser header


class TestDateNormalization(unittest.TestCase):
    def test_dd_mm_yyyy_and_iso(self):
        self.assertEqual(tds._normalize_date("07/06/2024"), "20240607")
        self.assertEqual(tds._normalize_date("2024-06-07"), "20240607")

    def test_excel_serial(self):
        out = tds._normalize_date("45450")
        self.assertEqual(len(out), 8)
        self.assertTrue("20200000" < out < "20300000")        # a plausible recent season
        # Serials must sort chronologically the same as calendar dates.
        self.assertLess(tds._normalize_date("45450"), tds._normalize_date("45460"))

    def test_blank(self):
        self.assertEqual(tds._normalize_date(""), "")


class TestSurnameIndexing(unittest.TestCase):
    def test_surname_key_drops_trailing_initial(self):
        self.assertEqual(rs._surname_key("alcaraz c"), "alcaraz")     # tennis-data style
        self.assertEqual(rs._surname_key("rafael nadal"), "nadal")    # Sackmann full name
        self.assertEqual(rs._surname_key("bautista agut r"), "agut")  # multi-token surname

    def test_unambiguous_alias_added_ambiguous_skipped(self):
        ratings = {"alcaraz c": {"elo": 2200}, "williams s": {"elo": 2000},
                   "williams v": {"elo": 1900}}
        out = rs.index_by_surname(ratings)
        self.assertIn("alcaraz", out)            # unique surname -> aliased
        self.assertEqual(out["alcaraz"]["elo"], 2200)
        self.assertNotIn("williams", out)        # shared surname -> left ambiguous (no guess)


class TestEndToEndResolve(unittest.TestCase):
    def test_tennisdata_names_resolve_by_full_label_and_surname(self):
        # 'Alcaraz C.' beats 'Sinner J.' repeatedly on clay -> higher clay Elo, and the rating
        # must resolve from BOTH a Polymarket full-name label and a bare surname slug.
        grid = [_HEADER]
        for d in range(1, 8):
            grid.append(_row(f"0{d}/06/2024", "Clay", "Alcaraz C.", "Sinner J."))
        matches = tds.rows_to_matches(tds.read_xlsx(_make_xlsx(grid)))
        rt = rs.index_by_surname(rs.build_elo_from_matches(matches))

        by_label = rmod.resolve("Carlos Alcaraz", rt)     # market outcome label
        by_slug = rmod.resolve("alcaraz", rt)             # slug surname token
        self.assertIsNotNone(by_label)
        self.assertIs(by_label, by_slug)
        self.assertGreater(by_label["elo"], 1500.0)
        self.assertGreater(by_label["clay"], 1500.0)
        self.assertIsNone(by_label["grass"])              # never played grass


class TestFetchLogging(unittest.TestCase):
    """The network fetcher must emit always-on access logs so reachability is verifiable."""

    def setUp(self):
        self._saved = sys.modules.get("requests")

    def tearDown(self):
        if self._saved is not None:
            sys.modules["requests"] = self._saved
        else:
            sys.modules.pop("requests", None)

    def _install_fake_requests(self, status, content):
        import types
        fake = types.ModuleType("requests")

        class _Resp:
            def __init__(self):
                self.status_code = status
                self.content = content
        fake.get = lambda url, timeout=15: _Resp()
        sys.modules["requests"] = fake

    def _run_capturing_stderr(self, *args, **kwargs):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = tds.fetch_tennisdata_matches(*args, **kwargs)
        return out, buf.getvalue()

    def test_success_logs_status_size_and_count(self):
        xlsx = _make_xlsx([_HEADER, _row("07/06/2024", "Clay", "Alcaraz C.", "Sinner J.")])
        self._install_fake_requests(200, xlsx)
        matches, log = self._run_capturing_stderr("atp", [2024])
        self.assertEqual(len(matches), 1)
        self.assertIn("HTTP 200 @ www.tennis-data.co.uk", log)
        self.assertIn("1 matches", log)
        self.assertIn("OK — 1 matches from 1/1 season(s)", log)

    def test_failure_logs_unreachable_and_egress_hint(self):
        self._install_fake_requests(403, b"Host not in allowlist")
        matches, log = self._run_capturing_stderr("atp", [2024])
        self.assertEqual(matches, [])
        self.assertIn("UNREACHABLE -> HTTP 403 @ www.tennis-data.co.uk", log)
        self.assertIn("FAILED", log)
        self.assertIn("egress allowlist", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
