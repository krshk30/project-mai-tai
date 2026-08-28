#!/usr/bin/env python3
"""END-OF-SESSION COUNTS — the figures that are allowed to be called results.

⛔⭐ WHY THIS EXISTS. No intraday count is a result until the session closes. On 2026-08-04 two
mid-session readings were falsified by the same day's own tape: "19 resting placements / ZERO
fills" (2 filled by 10:46) and "tape collapsed 17 -> 2" (6 by 12:05). Both had already been
reported. Everything here is taken AFTER the entry window shuts at 18:00 ET.

⛔ DENOMINATORS ARE THE POINT. Five separate defects this week came from reusing a signal that was
authoritative for one job as the source of truth for another:
  - resting fill rate is per LIVE ARM, never per placement intent (one cross = many placements on
    the 1-2 min reprice cadence, and #625 changed that very cadence)
  - crosses are LIVE arms only -- warmup replay emits one ARM per HISTORICAL flip and inflated the
    count ~14x (98 -> 7 on 08-04)
  - round trips are pairs, never fill counts
[[feedback_authoritative_for_a_is_not_for_b]]

Read-only. Touches no order path.
"""
import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/trader/entry_fix_watch")
from check import (ET, LIVE_ARM_MAX_AGE_SECS, CAP_ACCT, parse_segments, q)  # noqa: E402


def pct(a, b):
    return (b / a - 1.0) * 100.0 if a else None


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    a = ap.parse_args()
    d0 = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=ET)
    s_utc, e_utc = d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)

    et_now = datetime.now(ET)
    provisional = (a.day == et_now.strftime("%Y-%m-%d")) and (et_now.hour * 60 + et_now.minute) < 1080
    print("=" * 78)
    print("END-OF-SESSION COUNTS | day %s ET | taken %s" % (a.day, et_now.strftime("%F %H:%M:%S %Z")))
    if provisional:
        print("!! PROVISIONAL -- taken BEFORE 18:00 ET. These are NOT results. Re-run after the close.")
    print("=" * 78)

    # ---------- crosses: LIVE arms only ----------
    segs = parse_segments(s_utc, e_utc)
    replay = getattr(parse_segments, "replay_skipped", 0)
    enterable = [s for s in segs if s["enterable"]]
    print("\n-- CROSSES (denominator = LIVE arms; warmup replay excluded) --")
    print("   live arms=%d   warmup-replay arms excluded=%d   in entry window=%d"
          % (len(segs), replay, len(enterable)))

    # ---------- entries by slot ----------
    rows = q("""
        SELECT COALESCE(ti.payload->'metadata'->>'resting_entry','false') AS resting, COUNT(*)
        FROM trade_intents ti JOIN strategies s ON s.id=ti.strategy_id
        JOIN broker_accounts ba ON ba.id=ti.broker_account_id
        WHERE s.code='schwab_1m_v2' AND ti.intent_type='open' AND ti.status='filled'
          AND ba.name=%s AND ti.created_at>=%s AND ti.created_at<%s
        GROUP BY 1
    """, (CAP_ACCT, s_utc, e_utc))
    by_slot = {str(r[0]).lower(): int(r[1]) for r in rows}
    resting_n, reactive_n = by_slot.get("true", 0), by_slot.get("false", 0)
    total_entries = resting_n + reactive_n
    print("\n-- SCHWAB ENTRIES BY SLOT --")
    print("   resting=%d  reactive=%d  total=%d" % (resting_n, reactive_n, total_entries))

    # ---------- the corrected resting rate ----------
    print("\n-- RESTING FILL RATE, PER LIVE ARM (not per placement) --")
    if enterable:
        print("   %d resting fills / %d live in-window arms = %.1f%%"
              % (resting_n, len(enterable), 100.0 * resting_n / len(enterable)))
    else:
        print("   no live in-window arms -- rate undefined (NOT zero)")

    # ---------- no-entry crosses ----------
    fills_by_sym = {}
    for r in q("""SELECT f.symbol, f.filled_at FROM fills f
                  JOIN broker_orders bo ON bo.id=f.order_id
                  JOIN broker_accounts ba ON ba.id=bo.broker_account_id
                  JOIN strategies s ON s.id=bo.strategy_id
                  WHERE s.code='schwab_1m_v2' AND f.side='buy' AND ba.name=%s
                    AND f.filled_at>=%s AND f.filled_at<%s""", (CAP_ACCT, s_utc, e_utc)):
        fills_by_sym.setdefault(r[0], []).append(r[1])
    no_entry = [s for s in enterable
                if not any(s["start"] - timedelta(minutes=15) <= t < s["end"]
                           for t in fills_by_sym.get(s["sym"], []))]
    print("\n-- NO-ENTRY CROSSES (live, in-window) --")
    print("   %d of %d live in-window crosses produced no Schwab entry" % (len(no_entry), len(enterable)))
    for s in no_entry[:10]:
        print("     %-6s arm %s ET  frozen-2bar-high=%.4f  flip=%.4f"
              % (s["sym"], s["start"].astimezone(ET).strftime("%H:%M:%S"), s["trig"], s["flip"]))

    # ---------- round trips, in PERCENT ----------
    print("\n-- CLOSED ROUND TRIPS (percent; median-first; drop-one) --")
    legs = q("""
        SELECT ba.name, f.symbol, f.side, COUNT(*), MIN(f.price), MIN(f.filled_at)
        FROM fills f JOIN broker_orders bo ON bo.id=f.order_id
        JOIN broker_accounts ba ON ba.id=bo.broker_account_id
        WHERE ba.name IN ('live:schwab_1m_v2','live:orb')
          AND f.filled_at>=%s AND f.filled_at<%s
        GROUP BY 1,2,3
    """, (s_utc, e_utc))
    book = {}
    for acct, sym, side, n, px, at in legs:
        book.setdefault((acct, sym), {})[side] = (int(n), float(px), at)
    trips, ambiguous = [], 0
    for (acct, sym), sides in book.items():
        b, s_ = sides.get("buy"), sides.get("sell")
        if not b or not s_:
            continue
        if b[0] != 1 or s_[0] != 1:
            ambiguous += 1        # >1 fill per side -> pairing needs inference; refuse it
            continue
        trips.append((acct, sym, pct(b[1], s_[1])))
    if trips:
        vals = [t[2] for t in trips]
        print("   n=%d unambiguous round trips (%d symbol-legs excluded as ambiguous -- no FIFO)"
              % (len(trips), ambiguous))
        print("   MEDIAN %+.2f%%" % median(vals))
        for acct, sym, v in sorted(trips, key=lambda t: t[2]):
            print("     %-18s %-6s %+.2f%%" % (acct, sym, v))
        if len(vals) > 2:
            print("   drop-one by NAME:")
            for acct, sym, v in sorted(trips, key=lambda t: t[2]):
                rest = [x for x in vals if x is not v]
                print("     without %-6s -> median %+.2f%%" % (sym, median(rest)))
    else:
        print("   no unambiguous round trips (%d ambiguous)" % ambiguous)

    print("\n" + "=" * 78)
    print("VERDICT eod day=%s live_arms=%d replay_excluded=%d entries=%d (resting=%d reactive=%d) "
          "no_entry=%d trips=%d" % (a.day, len(segs), replay, total_entries, resting_n, reactive_n,
                                    len(no_entry), len(trips)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
