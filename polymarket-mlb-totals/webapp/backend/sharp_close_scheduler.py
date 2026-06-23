#!/usr/bin/env python3
"""Background scheduler: snapshot the SHARP closing line daily while the backend is up.

CLV vs the sharp close needs the closing line near first pitch, but The Odds API serves
only the CURRENT line — so the close has to be captured at the right moment. Rather than
an external cron, this runs capture_close.capture() at configured UTC times each day
(default 23:00, ≈ evening first pitches) for as long as the FastAPI process is alive.

Quota-conscious by design: the default 5 captures/day is ~150 Odds-API calls/month, well
within the free tier. The schedule (UTC) is injected by the caller.

Pure time math (parse_times / seconds_until_next) is offline-testable; the loop is
best-effort and never raises out of itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


def parse_times(spec: str) -> list[tuple[int, int]]:
    """'23:00,01:30' -> [(23,0),(1,30)]. Ignores blanks/garbage; validates ranges."""
    out: list[tuple[int, int]] = []
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            hh_s, mm_s = tok.split(":")
            hh, mm = int(hh_s), int(mm_s)
        except ValueError:
            continue
        if 0 <= hh < 24 and 0 <= mm < 60:
            out.append((hh, mm))
    return out


def seconds_until_next(times: list[tuple[int, int]], now: datetime | None = None) -> float:
    """Seconds from `now` (UTC) to the soonest upcoming scheduled time (next day if past)."""
    if not times:
        return 24 * 3600.0
    now = now or datetime.now(timezone.utc)
    best: float | None = None
    for hh, mm in times:
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        s = (t - now).total_seconds()
        best = s if best is None else min(best, s)
    return best if best is not None else 24 * 3600.0


async def run_loop(times, do_capture, vlog, sleep=asyncio.sleep) -> None:
    """Sleep until each scheduled UTC time, then await do_capture() (an async callable).

    Best-effort: a failing capture is logged, not raised, so the loop survives. The extra
    60s sleep after firing prevents a double-trigger within the same minute. `sleep` is
    injectable for tests.
    """
    while True:
        wait = seconds_until_next(times)
        vlog(f"[sharp-close] next capture in {wait / 3600:.1f}h")
        await sleep(wait)
        try:
            await do_capture()
        except Exception as e:  # noqa: BLE001 - a bad capture must not kill the scheduler
            vlog(f"[sharp-close] capture failed: {e}")
        await sleep(60)
