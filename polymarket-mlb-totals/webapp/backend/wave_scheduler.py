#!/usr/bin/env python3
"""Per-game recalc scheduler: recompute the model ~1 hour before each game starts.

Replaces the fixed-time sharp-close schedule. The model can only predict PREGAME games
(in-progress games are filtered), and Polymarket volume builds toward first pitch — so the
best moment to evaluate a game is shortly before it starts. This schedules a recompute for
each game at `commence_time - lead_min`, grouping near-simultaneous starts into one "wave"
(MLB games launch in blocks, and one Odds-API fetch covers the whole slate) to stay well
within the free quota.

Pure schedule math (waves_from_commences) is offline-testable; the loop is best-effort and
never raises out of itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


def waves_from_commences(commences, now=None, *, lead_min: int = 10,
                         bucket_min: int = 10) -> list[datetime]:
    """Trigger times (UTC) for the day's recalcs, from game start times.

    Each game's trigger is `commence - lead_min`. Triggers within `bucket_min` of an earlier
    one are merged into a single wave (its earliest trigger), so a block of simultaneous
    games costs ONE recompute. Past triggers (<= now) are dropped. Sorted, de-duplicated.
    """
    now = now or datetime.now(timezone.utc)
    raw = sorted(c - timedelta(minutes=lead_min) for c in commences)
    waves: list[datetime] = []
    for t in raw:
        if t <= now:
            continue
        if waves and (t - waves[-1]).total_seconds() <= bucket_min * 60:
            continue   # within the previous wave's window -> same wave
        waves.append(t)
    return waves


def next_wave(waves, fired, now) -> datetime | None:
    """The soonest scheduled wave that is still upcoming and unfired, or None."""
    future = [w for w in waves if w > now and w not in fired]
    return min(future) if future else None


async def run_wave_loop(today_fn, get_commences, do_wave, vlog, *,
                        lead_min: int = 10, bucket_min: int = 10, poll_sec: int = 300,
                        now_fn=None, sleep=asyncio.sleep, on_update=None) -> None:
    """Poll loop that fires one recompute wave as each game's lead window arrives.

    - `today_fn()` -> the current target date string (for logging/day rollover).
    - `get_commences()` -> awaitable list of upcoming UTC start times (refetched per day).
    - `do_wave()` -> awaitable that recomputes the slate (fetch + model + capture + cache).
    - `on_update(info)` -> optional callback with the live schedule for the UI:
      `{date, waves: [iso...], next_wave: iso|None}`, called each tick.

    Refetches the schedule when the day rolls over. Fires at most one wave per poll tick
    (a wave covers every pregame game), so missed triggers after a late start don't burst.
    Never raises; a failing fetch/wave is logged and retried next tick.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    state = {"date": None, "waves": [], "fired": set()}

    def _report(now):
        if on_update is None:
            return
        nxt = next_wave(state["waves"], state["fired"], now)
        on_update({"date": state["date"],
                   "waves": [w.isoformat() for w in state["waves"]],
                   "next_wave": nxt.isoformat() if nxt else None})

    while True:
        try:
            now, today = now_fn(), today_fn()
            if state["date"] != today or not state["waves"]:
                commences = await get_commences()
                state["date"] = today
                state["waves"] = waves_from_commences(commences, now, lead_min=lead_min,
                                                      bucket_min=bucket_min)
                state["fired"] = set()
                vlog(f"[waves] {today}: {len(state['waves'])} recalc wave(s) scheduled "
                     + ", ".join(w.strftime('%H:%MZ') for w in state["waves"]))
            due = [w for w in state["waves"] if w <= now and w not in state["fired"]]
            if due:
                for w in due:
                    state["fired"].add(w)   # one wave covers all due games
                vlog(f"[waves] firing recompute for {len(due)} due wave(s) at {now.strftime('%H:%MZ')}")
                await do_wave()
            _report(now)
        except Exception as e:  # noqa: BLE001 - the loop must survive any single failure
            vlog(f"[waves] loop error: {e}")
        await sleep(poll_sec)
