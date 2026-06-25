#!/usr/bin/env python3
"""tennis-data.co.uk match feed — a GitHub-independent source of surface-tagged matches.

Motivation: the Sackmann CSV mirrors (raw.githubusercontent / jsDelivr / statically) are all
GitHub-hosted and get egress-blocked on some networks. tennis-data.co.uk is a wholly separate
host that publishes one spreadsheet per season for ATP (men) and WTA (women), each row carrying
a `Surface` column plus the match winner/loser — exactly what the walk-forward Elo engine needs.

This module ONLY produces the same `{date, surface, winner, loser}` match dicts that
`ratings_source.fetch_sackmann_matches` returns; the Elo computation and surname indexing are
shared (`ratings_source.build_elo_from_matches` + `index_by_surname`). It is pure stdlib — the
season files are `.xlsx` (a zip of XML), read here with `zipfile`+`xml.etree`, no openpyxl/pandas.

Name format caveat: tennis-data writes players as "Surname I." (e.g. "Alcaraz C."), NOT full
names like Sackmann. `index_by_surname` keys each player by the last surname token (dropping the
trailing initial), so the resolver still matches Polymarket's full-name labels and surname slugs.

License/ToS: tennis-data.co.uk data is free for personal use; it aggregates official results and
betting odds. Treat like the other feeds (non-commercial). See references/deep-research.md.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

# Per-season workbooks. ATP lives under /{year}/, WTA under /{year}w/. Each path serves a
# zipped workbook ({year}.zip -> {year}.xlsx); some years also expose the .xlsx directly.
_BASE = "http://www.tennis-data.co.uk"
_TOUR_SUFFIX = {"atp": "", "wta": "w"}
_SURFACE_MAP = {"hard": "hard", "clay": "clay", "grass": "grass", "carpet": "hard"}
# Excel's 1900 date system, with the historical 1900-is-leap bug -> anchor at 1899-12-30.
_EXCEL_EPOCH = datetime(1899, 12, 30)


# ---------------------------------------------------------------------------
# Minimal stdlib .xlsx reader (zip of XML; no third-party deps)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Strip the XML namespace: '{...spreadsheetml...}c' -> 'c'."""
    return tag.rsplit("}", 1)[-1]


def _col_index(cell_ref: str) -> int:
    """Excel cell ref -> 0-based column index ('A1'->0, 'B2'->1, 'AA9'->26)."""
    col = 0
    for ch in cell_ref:
        if ch.isalpha():
            col = col * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return col - 1


def _extract_xlsx_bytes(data: bytes) -> bytes | None:
    """Return the workbook bytes: `data` is either the .xlsx itself or a .zip wrapping one."""
    if not data or data[:2] != b"PK":
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    names = zf.namelist()
    if "[Content_Types].xml" in names:
        return data                                  # already an .xlsx
    inner = next((n for n in names if n.lower().endswith(".xlsx")), None)
    if inner:                                        # a .zip wrapping the workbook
        try:
            return zf.read(inner)
        except KeyError:
            return None
    return None


def read_xlsx(data: bytes) -> list[list[str]]:
    """Parse an .xlsx (or a .zip wrapping one) into a list of rows, each a list of cell strings.

    Shared strings are resolved; gaps from skipped empty cells are filled so every row indexes
    by true column position. Returns [] when the bytes are not a readable workbook.
    """
    xlsx = _extract_xlsx_bytes(data)
    if xlsx is None:
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(xlsx))
    except zipfile.BadZipFile:
        return []

    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in sst:                               # <si> may hold one <t> or rich-text runs
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))

    sheet_name = next((n for n in ("xl/worksheets/sheet1.xml",)
                       if n in zf.namelist()), None)
    if sheet_name is None:
        sheet_name = next((n for n in zf.namelist()
                           if n.startswith("xl/worksheets/") and n.endswith(".xml")), None)
    if sheet_name is None:
        return []

    rows: list[list[str]] = []
    sheet = ET.fromstring(zf.read(sheet_name))
    for row_el in sheet.iter():
        if _local(row_el.tag) != "row":
            continue
        cells: dict[int, str] = {}
        for c in row_el:
            if _local(c.tag) != "c":
                continue
            ci = _col_index(c.get("r") or "A")
            ctype = c.get("t")
            text = ""
            if ctype == "inlineStr":
                text = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
            else:
                v = next((e for e in c if _local(e.tag) == "v"), None)
                raw = v.text if v is not None else None
                if raw is None:
                    text = ""
                elif ctype == "s":                   # shared-string index
                    try:
                        text = shared[int(raw)]
                    except (ValueError, IndexError):
                        text = ""
                else:
                    text = raw
            cells[ci] = text
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


# ---------------------------------------------------------------------------
# Row -> match parsing
# ---------------------------------------------------------------------------

def _normalize_date(raw: str) -> str:
    """tennis-data Date cell -> a chronologically sortable 'YYYYMMDD' string.

    .xlsx stores dates as serial numbers; we also tolerate DD/MM/YYYY and ISO just in case.
    Falls back to the raw value (still a stable sort key within one feed) if unrecognized.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.replace(".", "", 1).isdigit() and "/" not in raw and "-" not in raw:
        try:
            serial = int(float(raw))
            if 20000 <= serial <= 80000:             # ~1954..2119, the plausible match range
                return (_EXCEL_EPOCH + timedelta(days=serial)).strftime("%Y%m%d")
        except ValueError:
            pass
    if "/" in raw:                                    # DD/MM/YYYY (tennis-data's text form)
        p = raw.split("/")
        if len(p) == 3 and len(p[2]) == 4:
            return f"{p[2]}{int(p[1]):02d}{int(p[0]):02d}"
    if "-" in raw and len(raw) >= 10:                 # YYYY-MM-DD
        return raw[:10].replace("-", "")
    return raw


def rows_to_matches(rows: list[list[str]]) -> list[dict]:
    """Map tennis-data rows (header + data) to {date, surface, winner, loser} dicts.

    Columns are located by HEADER NAME (Winner/Loser/Surface/Date) so column-order changes
    across seasons don't break parsing. Rows missing a winner/loser name are dropped.
    """
    if not rows:
        return []
    header = [(_h or "").strip().lower() for _h in rows[0]]
    idx = {name: header.index(name) for name in ("winner", "loser", "surface", "date")
           if name in header}
    if "winner" not in idx or "loser" not in idx:
        return []

    def cell(row, name):
        i = idx.get(name)
        return row[i].strip() if (i is not None and i < len(row)) else ""

    out: list[dict] = []
    for row in rows[1:]:
        w, l = cell(row, "winner"), cell(row, "loser")
        if not w or not l:
            continue
        out.append({"date": _normalize_date(cell(row, "date")),
                    "surface": _SURFACE_MAP.get(cell(row, "surface").lower()),
                    "winner": w, "loser": l})
    return out


# ---------------------------------------------------------------------------
# Network layer (best-effort; mirrors ratings_source.fetch_sackmann_matches)
# ---------------------------------------------------------------------------

def _season_url_candidates(tour: str, year: int) -> list[str]:
    suffix = _TOUR_SUFFIX.get(tour, "")
    stem = f"{_BASE}/{year}{suffix}/{year}"
    return [f"{stem}.xlsx", f"{stem}.zip"]            # direct workbook, then zipped


def fetch_tennisdata_matches(tour: str, years: list[int], timeout: int = 15,
                             debug: bool = False) -> list[dict]:
    """Fetch + parse tennis-data.co.uk season workbooks for the given years. [] on failure.

    Emits ALWAYS-ON access logs (stderr) so reachability of www.tennis-data.co.uk is
    verifiable from the terminal: one line per season with the HTTP status, host, payload
    size and parsed-match count (or the concrete failure), plus a final summary. `debug`
    adds a trace of every individual URL attempt.
    """
    def _log(msg: str) -> None:
        print(f"[tennis_data_source] {msg}", file=sys.stderr, flush=True)

    try:
        import requests  # lazy, like the Sackmann path
    except Exception as e:  # noqa: BLE001
        _log(f"'requests' not importable: {e}")
        return []

    host = _BASE.split("/")[2]
    _log(f"{tour}: fetching {len(years)} season(s) {years} from {host} ...")
    rows_out: list[dict] = []
    errors: list[str] = []
    ok_seasons = 0
    for y in years:
        got = False
        last = "no response"
        for url in _season_url_candidates(tour, y):
            try:
                resp = requests.get(url, timeout=timeout)
                size = len(resp.content or b"")
                if debug:
                    _log(f"  GET {url} -> HTTP {resp.status_code} ({size/1024:.0f} KB)")
                if resp.status_code == 200 and resp.content:
                    parsed = rows_to_matches(read_xlsx(resp.content))
                    if parsed:
                        rows_out.extend(parsed)
                        ok_seasons += 1
                        got = True
                        _log(f"  {tour} {y}: HTTP 200 @ {host} ({size/1024:.0f} KB) "
                             f"-> {len(parsed)} matches  [{url.rsplit('/', 1)[-1]}]")
                        break
                    last = f"HTTP 200 but unparseable workbook ({size/1024:.0f} KB) @ {host}"
                else:
                    last = f"HTTP {resp.status_code} @ {host}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__} @ {host}"
        if not got:
            errors.append(f"{y}: {last}")
            _log(f"  {tour} {y}: UNREACHABLE -> {last}")
    rows_out.sort(key=lambda m: m["date"])           # chronological -> no look-ahead
    if rows_out:
        _log(f"{tour}: OK — {len(rows_out)} matches from {ok_seasons}/{len(years)} season(s) "
             f"@ {host}")
    else:
        _log(f"{tour}: FAILED — 0 matches from {host} -> {'; '.join(errors) or 'no seasons tried'}. "
             f"Is {host} on the network egress allowlist?")
    return rows_out


def default_years() -> list[int]:
    """Recent-form window: the last three seasons (UTC)."""
    y = datetime.now(timezone.utc).year
    return [y - 2, y - 1, y]
