#!/usr/bin/env python3
"""END-OF-SESSION COUNTS — the figures that are allowed to be called results.

⛔⭐ WHY THIS EXISTS. No intraday count is a result until the session closes. On 2026-08-04 two
mid-session readings were falsified by the same day's own tape: "19 resting placements / ZERO
fills" (2 filled by 10:46) and "tape collapsed 17 -> 2" (6 by 12:05). Both had already been
reported. Everything here is taken AFTER the entry window shuts at 18:00 ET.

⛔ DENOMINATORS ARE THE POINT. Five separate defects this week came from reusing a signal that was
authoritative for one job as the source of truth for another:
  - economic entry slot is explicit `cw_entry_slot=first|reclaim`; `resting_entry` is order style,
    and a reclaim can itself rest (#821)
  - first-slot fill rate is per LIVE ARM, never per placement intent (one cross = many placements
    on the 1-2 min reprice cadence, and #625 changed that very cadence)
  - crosses are LIVE arms only -- warmup replay emits one ARM per HISTORICAL flip and inflated the
    count ~14x (98 -> 7 on 08-04)
  - round trips are pairs, never fill counts
[[feedback_authoritative_for_a_is_not_for_b]]

Read-only. Touches no order path.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/trader/entry_fix_watch")
from check import ET, CAP_ACCT, parse_segments, q  # noqa: E402


def pct(a, b):
    return (b / a - 1.0) * 100.0 if a else None


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def format_slot_coverage(attributed: int, total: int) -> str:
    """Name whether economic-slot classification is usable, never defaulting unknown to zero."""
    if attributed < 0 or total < 0 or attributed > total:
        raise ValueError("slot coverage counts must satisfy 0 <= attributed <= total")
    if total == 0:
        return (
            "cw_entry_slot coverage=0/0 -- COULD_NOT_TELL "
            "(denominator=0; no filled-entry population)"
        )
    unknown = total - attributed
    if unknown:
        return (
            f"cw_entry_slot coverage={attributed}/{total} -- COULD_NOT_TELL "
            f"({unknown} filled entr{'y has' if unknown == 1 else 'ies have'} unknown "
            "classification; percentage withheld)"
        )
    return f"cw_entry_slot coverage={attributed}/{total} = 100.0% -- GRADEABLE"


def format_first_slot_rate(
    first: int,
    live_arms: int,
    *,
    attributed: int,
    total: int,
) -> str:
    """Render a numeric zero only when both its denominator and slot evidence are gradeable."""
    coverage = format_slot_coverage(attributed, total)
    prefix = f"{first} attributed first-slot fills / {live_arms} live in-window arms"
    if live_arms == 0:
        return f"{prefix} -- COULD_NOT_TELL (denominator=0; rate is not zero)"
    if total == 0 or attributed != total:
        return f"{prefix} -- COULD_NOT_TELL ({coverage}; numeric rate withheld)"
    return f"{prefix} = {100.0 * first / live_arms:.1f}%"


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

    # ---------- entries by explicit economic slot ----------
    rows = q("""
        SELECT COALESCE(ti.payload->'metadata'->>'cw_entry_slot','') AS entry_slot, COUNT(*)
        FROM trade_intents ti JOIN strategies s ON s.id=ti.strategy_id
        JOIN broker_accounts ba ON ba.id=ti.broker_account_id
        WHERE s.code='schwab_1m_v2' AND ti.intent_type='open' AND ti.status='filled'
          AND ba.name=%s AND ti.created_at>=%s AND ti.created_at<%s
        GROUP BY 1
    """, (CAP_ACCT, s_utc, e_utc))
    by_slot = {str(r[0]).strip().lower(): int(r[1]) for r in rows}
    first_n, reclaim_n = by_slot.get("first", 0), by_slot.get("reclaim", 0)
    unattributed_n = sum(n for slot, n in by_slot.items() if slot not in {"first", "reclaim"})
    attributed_n = first_n + reclaim_n
    total_entries = first_n + reclaim_n + unattributed_n
    slot_gradeable = total_entries > 0 and attributed_n == total_entries
    print("\n-- SCHWAB ENTRIES BY ECONOMIC SLOT --")
    print("   first=%d  reclaim=%d  unattributed=%d  total=%d"
          % (first_n, reclaim_n, unattributed_n, total_entries))
    print("   " + format_slot_coverage(attributed_n, total_entries))

    # ---------- the corrected first-slot rate ----------
    print("\n-- FIRST-SLOT FILL RATE, PER LIVE ARM (not per placement) --")
    print("   " + format_first_slot_rate(
        first_n,
        len(enterable),
        attributed=attributed_n,
        total=total_entries,
    ))

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
    print("VERDICT eod day=%s live_arms=%d replay_excluded=%d entries=%d "
          "(first=%d reclaim=%d unattributed=%d) slot_coverage=%d/%d "
          "slot_verdict=%s first_rate_verdict=%s no_entry=%d trips=%d"
          % (a.day, len(segs), replay, total_entries, first_n, reclaim_n, unattributed_n,
             attributed_n, total_entries,
             "GRADEABLE" if slot_gradeable else "COULD_NOT_TELL",
             "GRADEABLE" if slot_gradeable and enterable else "COULD_NOT_TELL",
             len(no_entry), len(trips)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
