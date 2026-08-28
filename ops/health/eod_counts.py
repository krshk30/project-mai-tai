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
from project_mai_tai.settings import get_settings  # noqa: E402
from project_mai_tai.strategy_core.entry_gate import resolve_entry_window  # noqa: E402


ENTRY_CLOSE_SOURCE = (
    "project_mai_tai.strategy_core.entry_gate.resolve_entry_window(get_settings())"
)
TRIP_ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
_VERDICT_EMITTED = False
_CURRENT_DAY = "unknown"

_FILLED_INTENTS_CTE = """
    WITH filled_intents AS (
        SELECT ti.id AS intent_id,
               LOWER(BTRIM(COALESCE(ti.payload->'metadata'->>'cw_entry_slot','')))
                   AS entry_slot,
               ti.symbol AS symbol,
               MIN(f.filled_at) AS filled_at
        FROM trade_intents ti
        JOIN strategies s ON s.id=ti.strategy_id
        JOIN broker_accounts ba ON ba.id=ti.broker_account_id
        JOIN broker_orders bo ON bo.intent_id=ti.id
        JOIN fills f ON f.order_id=bo.id AND f.side='buy'
        WHERE s.code='schwab_1m_v2' AND ti.intent_type='open' AND ti.status='filled'
          AND ba.name=%s AND f.filled_at>=%s AND f.filled_at<%s
        GROUP BY ti.id,
                 LOWER(BTRIM(COALESCE(ti.payload->'metadata'->>'cw_entry_slot',''))),
                 ti.symbol
    )
"""


def pct(a, b):
    return (b / a - 1.0) * 100.0 if a else None


def median(xs):
    return statistics.median(xs) if xs else None


def _emit_verdict(line: str) -> None:
    global _VERDICT_EMITTED
    print(line)
    _VERDICT_EMITTED = True


def format_slot_coverage(
    attributed: int,
    total: int,
    *,
    reconciled_total: int | None = None,
) -> str:
    """Name whether economic-slot classification is usable, never defaulting unknown to zero."""
    if attributed < 0 or total < 0 or attributed > total:
        return (
            f"cw_entry_slot coverage={attributed}/{total} -- COULD_NOT_TELL "
            "(invalid counts: numerator must not exceed denominator)"
        )
    if reconciled_total is not None and reconciled_total != total:
        return (
            f"cw_entry_slot coverage={attributed}/{total} -- COULD_NOT_TELL "
            f"(filled-intent populations disagree: slot_counts={total} "
            f"detail_rows={reconciled_total})"
        )
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
    reconciled_total: int | None = None,
) -> str:
    """Render a numeric zero only when both its denominator and slot evidence are gradeable."""
    coverage = format_slot_coverage(
        attributed,
        total,
        reconciled_total=reconciled_total,
    )
    prefix = f"{first} attributed first-slot fills / {live_arms} live in-window arms"
    if first < 0 or live_arms < 0 or first > live_arms:
        return (
            f"{prefix} -- COULD_NOT_TELL "
            "(invalid rate: numerator must not exceed denominator)"
        )
    if live_arms == 0:
        return f"{prefix} -- COULD_NOT_TELL (denominator=0; rate is not zero)"
    if (
        total == 0
        or attributed != total
        or (reconciled_total is not None and reconciled_total != total)
    ):
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

    # This is deliberately inside main(): invalid/missing runtime configuration must still pass
    # through _run_cli()'s terminal-verdict protection. resolve_entry_window is the exact source
    # used by the live v2 entry gate, so this report cannot drift to a second close-time default.
    settings = get_settings()
    _, _, entry_close_hour, entry_close_minute = resolve_entry_window(settings)
    if not (0 <= entry_close_hour <= 23 and 0 <= entry_close_minute <= 59):
        raise ValueError(
            "invalid v2 entry close from runtime gate: "
            f"{entry_close_hour:02d}:{entry_close_minute:02d} ET"
        )
    entry_close_min = entry_close_hour * 60 + entry_close_minute
    entry_close_label = f"{entry_close_hour:02d}:{entry_close_minute:02d}"

    et_now = datetime.now(ET)
    provisional = (
        a.day == et_now.strftime("%Y-%m-%d")
        and (et_now.hour * 60 + et_now.minute) < entry_close_min
    )
    print("=" * 78)
    print("END-OF-SESSION COUNTS | day %s ET | taken %s" % (a.day, et_now.strftime("%F %H:%M:%S %Z")))
    print(f"entry close={entry_close_label} ET | source={ENTRY_CLOSE_SOURCE}")
    if provisional:
        print(
            "!! PROVISIONAL -- taken BEFORE configured entry close %02d:%02d ET. "
            "These are NOT results. Re-run after the close."
            % (entry_close_min // 60, entry_close_min % 60)
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
    filled_params = (CAP_ACCT, s_utc, e_utc)
    rows = q(
        _FILLED_INTENTS_CTE
        + """
        /* eod:slot-counts */
        SELECT entry_slot, COUNT(*)
        FROM filled_intents
        GROUP BY entry_slot
        """,
        filled_params,
    )
    by_slot: dict[str, int] = {}
    for raw_slot, raw_count in rows:
        slot = str(raw_slot).strip().lower()
        by_slot[slot] = by_slot.get(slot, 0) + int(raw_count)
    first_n, reclaim_n = by_slot.get("first", 0), by_slot.get("reclaim", 0)
    unattributed_n = sum(n for slot, n in by_slot.items() if slot not in {"first", "reclaim"})
    attributed_n = first_n + reclaim_n
    total_entries = first_n + reclaim_n + unattributed_n

    # The no-entry view uses the identical CTE and filters. Two reads remain deliberate: a fill can
    # land between them. That race invalidates the slot section only; it must not abort the EOD
    # report or erase independently gradeable no-entry/round-trip evidence.
    filled_rows = q(
        _FILLED_INTENTS_CTE
        + """
        /* eod:filled-intents */
        SELECT symbol, filled_at, intent_id
        FROM filled_intents
        ORDER BY filled_at, intent_id
        """,
        filled_params,
    )
    fills_by_sym = {}
    fill_intent_ids = set()
    for symbol, filled_at, intent_id in filled_rows:
        fills_by_sym.setdefault(symbol, []).append(filled_at)
        if intent_id is not None:
            fill_intent_ids.add(intent_id)
    detailed_entries = len(fill_intent_ids)
    populations_reconcile = detailed_entries == total_entries
    slot_gradeable = (
        populations_reconcile
        and total_entries > 0
        and attributed_n == total_entries
    )
    print("\n-- SCHWAB ENTRIES BY ECONOMIC SLOT --")
    print("   first=%d  reclaim=%d  unattributed=%d  total=%d"
          % (first_n, reclaim_n, unattributed_n, total_entries))
    print("   " + format_slot_coverage(
        attributed_n,
        total_entries,
        reconciled_total=detailed_entries,
    ))
    print(
        "   filled-intent population slot_counts=%d detail_rows=%d verdict=%s"
        % (
            total_entries,
            detailed_entries,
            "RECONCILED" if populations_reconcile else "COULD_NOT_TELL",
        )
    )

    # ---------- the corrected first-slot rate ----------
    print("\n-- FIRST-SLOT FILL RATE, PER LIVE ARM (not per placement) --")
    print("   " + format_first_slot_rate(
        first_n,
        len(enterable),
        attributed=attributed_n,
        total=total_entries,
        reconciled_total=detailed_entries,
    ))

    # ---------- no-entry crosses ----------
    no_entry = [s for s in enterable
                if not any(s["start"] - timedelta(minutes=15) <= t < s["end"]
                           for t in fills_by_sym.get(s["sym"], []))]
    no_entry_gradeable = bool(enterable) and replay is not None
    print("\n-- NO-ENTRY CROSSES (live, in-window) --")
    if no_entry_gradeable:
        print(
            "   %d of %d live in-window crosses produced no Schwab entry"
            % (len(no_entry), len(enterable))
        )
    elif enterable:
        print(
            "   %d of %d apparent live in-window crosses -- COULD_NOT_TELL "
            "(warmup-replay exclusion counter absent)"
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
    trips = []
    ambiguous_by_account = {account: 0 for account in TRIP_ACCOUNTS}
    invalid_by_account = {account: 0 for account in TRIP_ACCOUNTS}
    for (acct, sym), sides in book.items():
        b, s_ = sides.get("buy"), sides.get("sell")
        if not b or not s_:
            continue
        if b[0] != 1 or s_[0] != 1:
            # >1 fill per side -> pairing needs inference; refuse it, per account.
            ambiguous_by_account[acct] = ambiguous_by_account.get(acct, 0) + 1
            continue
        try:
            buy_price, sell_price = float(b[1]), float(s_[1])
        except (TypeError, ValueError):
            invalid_by_account[acct] = invalid_by_account.get(acct, 0) + 1
            continue
        if buy_price <= 0 or sell_price <= 0:
            invalid_by_account[acct] = invalid_by_account.get(acct, 0) + 1
            continue
        trips.append((acct, sym, pct(buy_price, sell_price)))
    trip_verdicts = {}
    for account in TRIP_ACCOUNTS:
        account_trips = [(sym, value) for acct, sym, value in trips if acct == account]
        ambiguous = ambiguous_by_account.get(account, 0)
        invalid_price = invalid_by_account.get(account, 0)
        if invalid_price or (not account_trips and ambiguous):
            account_verdict = "COULD_NOT_TELL"
        elif account_trips:
            account_verdict = "GRADEABLE"
        else:
            account_verdict = "UNEXERCISED"
        trip_verdicts[account] = account_verdict
        print(
            "   account=%s n=%d ambiguous=%d invalid_price=%d verdict=%s"
            % (account, len(account_trips), ambiguous, invalid_price, account_verdict)
        )
        if not account_trips:
            continue
        vals = [value for _, value in account_trips]
        print("     MEDIAN %+.2f%%" % median(vals))
        for sym, value in sorted(account_trips, key=lambda item: item[1]):
            print("       %-6s %+.2f%%" % (sym, value))
        if len(vals) > 2:
            print("     drop-one by NAME:")
            for dropped_sym, _ in sorted(account_trips):
                rest = [value for sym, value in account_trips if sym != dropped_sym]
                if rest:
                    print(
                        "       without %-6s -> median %+.2f%%"
                        % (dropped_sym, median(rest))
                    )

    if all(verdict == "GRADEABLE" for verdict in trip_verdicts.values()):
        trips_verdict = "GRADEABLE"
    elif all(verdict == "UNEXERCISED" for verdict in trip_verdicts.values()):
        trips_verdict = "UNEXERCISED"
    else:
        trips_verdict = "COULD_NOT_TELL"

    no_entry_verdict = "GRADEABLE" if no_entry_gradeable else "COULD_NOT_TELL"
    first_rate_gradeable = (
        slot_gradeable
        and bool(enterable)
        and replay is not None
        and first_n <= len(enterable)
    )

    print("\n" + "=" * 78)
    _emit_verdict("VERDICT eod day=%s entry_close_et=%s entry_close_source=%s "
          "live_arms=%d replay_excluded=%s entries=%d "
          "(first=%d reclaim=%d unattributed=%d) slot_coverage=%d/%d "
          "slot_population=%d/%d slot_verdict=%s first_rate_verdict=%s "
          "no_entry=%d no_entry_verdict=%s trips=%d trips_verdict=%s"
        % (a.day, entry_close_label, ENTRY_CLOSE_SOURCE, len(segs), replay_label,
             total_entries, first_n, reclaim_n, unattributed_n,
             attributed_n, total_entries,
             total_entries, detailed_entries,
             "GRADEABLE" if slot_gradeable else "COULD_NOT_TELL",
             "GRADEABLE" if first_rate_gradeable else "COULD_NOT_TELL",
             len(no_entry), no_entry_verdict, len(trips), trips_verdict))
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
