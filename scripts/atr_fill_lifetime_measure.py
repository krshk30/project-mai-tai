"""Measure schwab_1m_v2 ATR position LIFETIMES from broker_order_events, to bound the re-arm fix's
fill-and-exit-within-one-poll residual (docs/schwab-1m-v2-reject-signal-release.md §2). Pairs each ATR
OPEN fill with its exit fills (linked by client_order_id = schwab_1m_v2-{symbol}-{open|close}-{hash}).

Result 2026-07-09 (06-24..07-08): 26 fills, 2 with < 5s lifetime (KIDZ 2s, LHAI 4s); 24 lived >=5s
(always poll-caught since polls are 5s apart) -> the poll-miss blind spot is RARE.

Run on the VPS: DSN=<db url, strip +psycopg> python scripts/atr_fill_lifetime_measure.py
"""
import os
import re
from collections import defaultdict

import psycopg

dsn = os.environ["DSN"]
CO = re.compile(r"^schwab_1m_v2-(.+)-(open|close)-[0-9a-f]+$")

with psycopg.connect(dsn) as c, c.cursor() as cur:
    # all v2 fill events (filled + partially_filled), parse client_order_id
    cur.execute("""
        select event_at, event_type, payload
        from broker_order_events
        where event_type in ('filled','partially_filled')
          and event_at >= '2026-06-24'
          and payload->>'client_order_id' like 'schwab_1m_v2-%'
        order by event_at """)
    by_sym = defaultdict(list)
    n_open = n_close = 0
    for at, etype, pl in cur.fetchall():
        coid = (pl or {}).get("client_order_id", "")
        m = CO.match(coid)
        if not m:
            continue
        sym, kind = m.group(1), m.group(2)
        by_sym[sym].append((at, kind, etype, (pl or {}).get("reason", "")))
        n_open += kind == "open"
        n_close += kind == "close"
    print("v2 fill events: opens=%d closes=%d symbols=%d" % (n_open, n_close, len(by_sym)))

    # pair each OPEN fill with the exit fills until the next OPEN on the same symbol
    results = []  # (sym, entry_at, first_close_dt_s, flat_dt_s, exit_reason)
    for sym, evs in by_sym.items():
        evs.sort()
        i = 0
        while i < len(evs):
            at, kind, etype, reason = evs[i]
            if kind == "open":
                first_close = flat = None
                exit_reason = None
                j = i + 1
                while j < len(evs) and evs[j][1] != "open":
                    if evs[j][1] == "close":
                        if first_close is None:
                            first_close, exit_reason = evs[j][0], evs[j][3]
                        flat = evs[j][0]
                    j += 1
                results.append((sym, at, first_close, flat, exit_reason))
                i = j
            else:
                i += 1

    print("\nOPEN fills paired: %d" % len(results))
    print("%-7s %-25s %-9s %-9s %s" % ("sym", "entry(UTC)", "1st_exit", "to_flat", "exit_reason"))
    fast_first = fast_flat = no_exit = 0
    dur_list = []
    for sym, at, fc, fl, er in sorted(results, key=lambda x: x[1]):
        d1 = (fc - at).total_seconds() if fc else None
        df = (fl - at).total_seconds() if fl else None
        if fc is None:
            no_exit += 1
        else:
            dur_list.append(df if df is not None else d1)
            if d1 < 5:
                fast_first += 1
            if df is not None and df < 5:
                fast_flat += 1
        print("%-7s %-25s %-9s %-9s %s" % (
            sym, str(at)[:25],
            f"{d1:.1f}" if d1 is not None else "NONE",
            f"{df:.1f}" if df is not None else "NONE", er or ""))

    dur_list.sort()
    med = dur_list[len(dur_list) // 2] if dur_list else None
    print("\n=== RESULT (of %d paired OPEN fills) ===" % len(results))
    print("no close fill found (still open at query time / unpaired):", no_exit)
    print("first exit fill  < 5s of entry:", fast_first)
    print("FULLY FLAT       < 5s of entry  (the poll-miss danger zone):", fast_flat)
    print("median lifetime (s):", f"{med:.1f}" if med is not None else None)
    if dur_list:
        print("min/max lifetime (s): %.1f / %.1f" % (dur_list[0], dur_list[-1]))
        print("lifetimes < 5s:", [round(d, 1) for d in dur_list if d < 5])
        print("lifetimes 5-15s:", [round(d, 1) for d in dur_list if 5 <= d < 15])
