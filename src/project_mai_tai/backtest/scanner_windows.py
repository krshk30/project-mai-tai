"""Reconstruct each symbol's CONFIRMED windows (when it was in the momentum scanner's tradeable
universe) from the strategy-engine `momentum_confirmed` log events.

The live v2 bot only trades a name while it is CONFIRMED. The scanner confirms a name
(`[CONFIRMED] ✅ SYM`), then prunes it on the fade rule (`[CONFIRMED] removed N faded candidates
below 30.0%: SYM, ...`), and may re-confirm/prune repeatedly (e.g. CLRO flickered ~8x on 07-07).
So a backtest must restrict entries to these windows, NOT scan the whole session. Log timestamps
are UTC (verified: the 08:00 UTC session-roll == 04:00 ET scanner reset).

Usage as an extractor (log is root-readable, so pipe a sudo-grep through this):
    sudo grep momentum_confirmed strategy.log | grep 2026-07-07 \
      | python -m project_mai_tai.backtest.scanner_windows 2026-07-07 > windows_2026-07-07.json

Emits {SYMBOL: [[start_iso, end_iso], ...]} (UTC). `load_windows(path)` reads it back to datetimes.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+)")
_ADD = re.compile(r"✅ ([A-Z0-9.]+)\s*—")                       # "✅ SYM —"
_REM = re.compile(r"removed \d+ faded candidates below [\d.]+%:\s*(.+?)\s*$")


def parse_events(lines):
    """[(dt_utc, symbol, 'add'|'remove')] from raw momentum_confirmed log lines."""
    evs = []
    for ln in lines:
        m = _TS.match(ln)
        if not m:
            continue
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc, microsecond=int(m.group(2)) * 1000)
        a = _ADD.search(ln)
        if a:
            evs.append((dt, a.group(1), "add"))
            continue
        r = _REM.search(ln)
        if r:
            for sym in (s.strip() for s in r.group(1).split(",")):
                if sym:
                    evs.append((dt, sym, "remove"))
    return evs


def build_windows(events, day_end):
    """Per-symbol confirmed intervals. An 'add' opens a window if none is open (a second 'add'
    while open just refreshes); a 'remove' closes it. An open window at day_end is closed there."""
    by = defaultdict(list)
    for dt, sym, kind in sorted(events, key=lambda e: e[0]):
        by[sym].append((dt, kind))
    out = {}
    for sym, seq in by.items():
        wins, open_t = [], None
        for dt, kind in seq:
            if kind == "add":
                if open_t is None:
                    open_t = dt
            elif open_t is not None:
                wins.append((open_t, dt))
                open_t = None
        if open_t is not None:
            wins.append((open_t, day_end))
        out[sym] = wins
    return out


def load_windows(path):
    """Read a windows JSON back into {SYMBOL: [(start_dt, end_dt), ...]} (UTC)."""
    with open(path) as fh:
        raw = json.load(fh)
    return {sym: [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in wins]
            for sym, wins in raw.items()}


def in_any_window(ts, windows) -> bool:
    return any(a <= ts <= b for a, b in windows)


def main():
    date = sys.argv[1]
    y, m, d = (int(x) for x in date.split("-"))
    day_end = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)   # backtest window end
    events = parse_events(sys.stdin)
    windows = build_windows(events, day_end)
    out = {sym: [[a.isoformat(), b.isoformat()] for a, b in wins] for sym, wins in windows.items()}
    print(json.dumps(out, indent=0))


if __name__ == "__main__":
    main()
