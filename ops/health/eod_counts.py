#!/usr/bin/env python3
"""END-OF-SESSION COUNTS — the figures that are allowed to be called results.

⛔⭐ WHY THIS EXISTS. No intraday count is a result until the session closes. On 2026-08-04 two
mid-session readings were falsified by the same day's own tape: "19 resting placements / ZERO
fills" (2 filled by 10:46) and "tape collapsed 17 -> 2" (6 by 12:05). Both had already been
reported. Everything here is taken AFTER the configured entry window shuts (16:00 ET on the
production configuration reviewed 2026-08-28).

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

HISTORICAL LIMIT (measured 2026-08-27): cw_entry_slot did not exist on the retained fills from
2026-08-04 through 2026-08-27. Coverage is 0/239 Schwab BUY fills and 0/301 Webull BUY fills.
Every first-vs-reclaim result for that interval is therefore COULD_NOT_TELL by construction, not
zero and not a historical composition grade.

Read-only. Touches no order path.
"""
import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/trader/entry_fix_watch")
from check import ET, CAP_ACCT, parse_segments, q  # noqa: E402
from project_mai_tai.settings import Settings  # noqa: E402


_SETTINGS = Settings()
ENTRY_CLOSE_MIN = (
    int(_SETTINGS.strategy_schwab_1m_v2_entry_window_end_hour_et) * 60
    + int(_SETTINGS.strategy_schwab_1m_v2_entry_window_end_minute_et)
)
_VERDICT_EMITTED = False
_CURRENT_DAY = "unknown"


def pct(a, b):
    return (b / a - 1.0) * 100.0 if a else None


def median(xs):
    return statistics.median(xs) if xs else None


def _emit_verdict(line: str) -> None:
    global _VERDICT_EMITTED
    print(line)
    _VERDICT_EMITTED = True


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
    global _CURRENT_DAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    a = ap.parse_args()
    _CURRENT_DAY = a.day
    d0 = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=ET)
    s_utc, e_utc = d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)

    et_now = datetime.now(ET)
    provisional = (
        a.day == et_now.strftime("%Y-%m-%d")
        and (et_now.hour * 60 + et_now.minute) < ENTRY_CLOSE_MIN
    )
    print("=" * 78)
    print("END-OF-SESSION COUNTS | day %s ET | taken %s" % (a.day, et_now.strftime("%F %H:%M:%S %Z")))
    if provisional:
        print(
            "!! PROVISIONAL -- taken BEFORE configured entry close %02d:%02d ET. "
            "These are NOT results. Re-run after the close."
            % (ENTRY_CLOSE_MIN // 60, ENTRY_CLOSE_MIN % 60)
        )
    print("=" * 78)

    # ---------- crosses: LIVE arms only ----------
    segs = parse_segments(s_utc, e_utc)
    replay_raw = getattr(parse_segments, "replay_skipped", None)
    replay = int(replay_raw) if isinstance(replay_raw, int) and replay_raw >= 0 else None
    replay_label = str(replay) if replay is not None else "COULD_NOT_TELL"
    enterable = [s for s in segs if s["enterable"]]
    print("\n-- CROSSES (denominator = LIVE arms; warmup replay excluded) --")
    print(
        "   live arms=%d   warmup-replay arms excluded=%s   in entry window=%d"
        % (
            len(segs),
            replay if replay is not None else "COULD_NOT_TELL (counter absent)",
            len(enterable),
        )
    )

    # ---------- entries by explicit economic slot ----------
    rows = q("""
        SELECT COALESCE(ti.payload->'metadata'->>'cw_entry_slot','') AS entry_slot,
               COUNT(DISTINCT ti.id)
        FROM trade_intents ti JOIN strategies s ON s.id=ti.strategy_id
        JOIN broker_accounts ba ON ba.id=ti.broker_account_id
        JOIN broker_orders bo ON bo.intent_id=ti.id
        JOIN fills f ON f.order_id=bo.id AND f.side='buy'
        WHERE s.code='schwab_1m_v2' AND ti.intent_type='open' AND ti.status='filled'
          AND ba.name=%s AND f.filled_at>=%s AND f.filled_at<%s
        GROUP BY 1
    """, (CAP_ACCT, s_utc, e_utc))
    by_slot: dict[str, int] = {}
    for raw_slot, raw_count in rows:
        slot = str(raw_slot).strip().lower()
        by_slot[slot] = by_slot.get(slot, 0) + int(raw_count)
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
    fill_intent_ids = set()
    for r in q("""SELECT f.symbol, f.filled_at, bo.intent_id FROM fills f
                  JOIN broker_orders bo ON bo.id=f.order_id
                  JOIN broker_accounts ba ON ba.id=bo.broker_account_id
                  JOIN strategies s ON s.id=bo.strategy_id
                  WHERE s.code='schwab_1m_v2' AND f.side='buy' AND ba.name=%s
                    AND f.filled_at>=%s AND f.filled_at<%s""", (CAP_ACCT, s_utc, e_utc)):
        fills_by_sym.setdefault(r[0], []).append(r[1])
        if r[2] is not None:
            fill_intent_ids.add(r[2])
    if len(fill_intent_ids) != total_entries:
        raise RuntimeError(
            "filled-entry reconciliation failed: "
            f"trade_intents={total_entries} distinct_fill_intents={len(fill_intent_ids)}"
        )
    no_entry = [s for s in enterable
                if not any(s["start"] - timedelta(minutes=15) <= t < s["end"]
                           for t in fills_by_sym.get(s["sym"], []))]
    print("\n-- NO-ENTRY CROSSES (live, in-window) --")
    if enterable:
        print(
            "   %d of %d live in-window crosses produced no Schwab entry"
            % (len(no_entry), len(enterable))
        )
    else:
        print(
            "   0 of 0 live in-window crosses -- COULD_NOT_TELL "
            "(denominator=0; no-entry rate is not zero)"
        )
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
        book.setdefault((acct, sym), {})[side] = (int(n), px, at)
    trips, ambiguous, invalid_price = [], 0, 0
    for (acct, sym), sides in book.items():
        b, s_ = sides.get("buy"), sides.get("sell")
        if not b or not s_:
            continue
        if b[0] != 1 or s_[0] != 1:
            ambiguous += 1        # >1 fill per side -> pairing needs inference; refuse it
            continue
        try:
            buy_price, sell_price = float(b[1]), float(s_[1])
        except (TypeError, ValueError):
            invalid_price += 1
            continue
        if buy_price <= 0 or sell_price <= 0:
            invalid_price += 1
            continue
        trips.append((acct, sym, pct(buy_price, sell_price)))
    if trips:
        vals = [t[2] for t in trips]
        print(
            "   n=%d unambiguous round trips "
            "(%d symbol-legs excluded as ambiguous -- no FIFO; %d invalid-price pairs)"
            % (len(trips), ambiguous, invalid_price)
        )
        print("   MEDIAN %+.2f%%" % median(vals))
        for acct, sym, v in sorted(trips, key=lambda t: t[2]):
            print("     %-18s %-6s %+.2f%%" % (acct, sym, v))
        if len(vals) > 2:
            print("   drop-one by NAME:")
            for acct, sym, v in sorted(trips, key=lambda t: t[2]):
                rest = [x for x in vals if x is not v]
                print("     without %-6s -> median %+.2f%%" % (sym, median(rest)))
    else:
        suffix = " -- COULD_NOT_TELL" if invalid_price else ""
        print(
            "   no unambiguous round trips (%d ambiguous; %d invalid-price pairs)%s"
            % (ambiguous, invalid_price, suffix)
        )

    print("\n" + "=" * 78)
    _emit_verdict("VERDICT eod day=%s live_arms=%d replay_excluded=%s entries=%d "
          "(first=%d reclaim=%d unattributed=%d) slot_coverage=%d/%d "
          "slot_verdict=%s first_rate_verdict=%s no_entry=%d trips=%d"
        % (a.day, len(segs), replay_label, total_entries, first_n, reclaim_n, unattributed_n,
             attributed_n, total_entries,
             "GRADEABLE" if slot_gradeable else "COULD_NOT_TELL",
             "GRADEABLE" if slot_gradeable and enterable and replay is not None else "COULD_NOT_TELL",
             len(no_entry), len(trips)))
    return 0


def _run_cli() -> int:
    try:
        return main()
    finally:
        if not _VERDICT_EMITTED:
            _emit_verdict(
                "VERDICT eod day=%s report_verdict=COULD_NOT_TELL "
                "error=aborted_before_terminal_verdict" % _CURRENT_DAY
            )


if __name__ == "__main__":
    sys.exit(_run_cli())
