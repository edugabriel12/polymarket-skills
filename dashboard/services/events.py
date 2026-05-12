"""Event service — read recent JSONL events (for partial refresh) and
yield new events as they arrive (for SSE stream).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import AsyncGenerator, Optional

from .. import settings as S


def read_recent_events(limit: int = 10,
                       jsonl_path: Optional[Path] = None) -> list[dict]:
    """Return the last `limit` valid JSON lines from the event log, most
    recent first. Skips malformed lines silently."""
    path = jsonl_path or S.JSONL_PATH
    if not path.exists():
        return []
    # Efficient last-N: scan from EOF backwards. For files < 1MB just read all.
    buf: deque[str] = deque(maxlen=limit)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size < 1_000_000:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    buf.append(line)
    else:
        # Tail-read for big files
        with open(path, "rb") as f:
            f.seek(max(0, size - 200_000))  # ~200KB window
            f.readline()  # discard partial line
            chunk = f.read().decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            line = line.strip()
            if line:
                buf.append(line)
    out = []
    for raw in list(buf)[-limit:]:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    out.reverse()  # most recent first
    return out


async def tail_jsonl(
    jsonl_path: Optional[Path] = None,
    poll_ms: Optional[int] = None,
    initial_tail: Optional[int] = None,
) -> AsyncGenerator[dict, None]:
    """Async generator: first yields `initial_tail` events already in file
    (oldest-first), then yields each new event appended."""
    path = jsonl_path or S.JSONL_PATH
    poll = (poll_ms or S.SSE_POLL_MS) / 1000.0
    initial_n = initial_tail if initial_tail is not None else S.SSE_INITIAL_TAIL_LINES

    # Initial backlog (oldest-first so client sees natural order)
    backlog = read_recent_events(limit=initial_n, jsonl_path=path)
    for ev in reversed(backlog):
        yield ev

    # Now follow new appends
    last_size = path.stat().st_size if path.exists() else 0
    buf = ""
    while True:
        await asyncio.sleep(poll)
        try:
            cur_size = path.stat().st_size
        except FileNotFoundError:
            continue
        if cur_size < last_size:
            # File was rotated/truncated — reset
            last_size = 0
            buf = ""
        if cur_size > last_size:
            with open(path, "rb") as f:
                f.seek(last_size)
                chunk = f.read(cur_size - last_size).decode("utf-8",
                                                              errors="replace")
            last_size = cur_size
            buf += chunk
            lines = buf.split("\n")
            # Keep incomplete last line in buf
            buf = lines[-1]
            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import os

    tmp = Path(tempfile.mkdtemp())
    jsonl = tmp / "test.jsonl"

    # Empty file → empty list
    jsonl.touch()
    assert read_recent_events(limit=10, jsonl_path=jsonl) == []
    print("Test 1 PASS: empty file → []")

    # Write 5 events, expect 3 most-recent first
    with open(jsonl, "w") as f:
        for i in range(5):
            f.write(json.dumps({"ts": f"2026-01-0{i+1}T00:00Z",
                                "event_type": "test", "i": i}) + "\n")
    events = read_recent_events(limit=3, jsonl_path=jsonl)
    assert len(events) == 3
    assert events[0]["i"] == 4  # most recent
    assert events[2]["i"] == 2
    print(f"Test 2 PASS: last-3 → indices {[e['i'] for e in events]}")

    # Skip malformed line — request limit=3 to allow buffer for the bad one
    with open(jsonl, "a") as f:
        f.write("not json\n")
        f.write(json.dumps({"ts": "2026-01-06T00:00Z", "i": 5}) + "\n")
    events = read_recent_events(limit=3, jsonl_path=jsonl)
    # We grabbed last 3 raw lines: i=4, "not json", i=5. Parsing skips bad line.
    assert len(events) == 2, events
    assert events[0]["i"] == 5  # most recent valid
    print(f"Test 3 PASS: skip malformed → {[e['i'] for e in events]}")

    # Test async tail_jsonl
    async def run_tail():
        jsonl2 = tmp / "tail.jsonl"
        with open(jsonl2, "w") as f:
            f.write(json.dumps({"i": 0}) + "\n")
            f.write(json.dumps({"i": 1}) + "\n")

        collected: list[dict] = []

        async def producer():
            await asyncio.sleep(0.1)  # let initial yields happen first
            with open(jsonl2, "a") as f:
                f.write(json.dumps({"i": 2}) + "\n")
                f.write(json.dumps({"i": 3}) + "\n")

        async def consumer():
            async for ev in tail_jsonl(jsonl_path=jsonl2, poll_ms=50,
                                        initial_tail=5):
                collected.append(ev)
                if len(collected) >= 4:
                    break

        await asyncio.gather(producer(), consumer())
        return collected

    collected = asyncio.run(run_tail())
    assert len(collected) == 4, collected
    indices = [e["i"] for e in collected]
    assert indices == [0, 1, 2, 3], indices
    print(f"Test 4 PASS: tail_jsonl streams initial + new → {indices}")

    print("\nAll events tests PASS")
