"""THE bug's true cost (one number, not an estimate). Of the graze-first BUY flips, model the LIVE
hold-confirm verdict on the FIRST graze of each segment (the one that claims the guard), using Polygon
trades for the 20s net_delta window — exactly as _resolve_hold does:
    n_ticks < min_ticks(5)  -> fallback_thin -> ENTERS (Path-B; NOT a miss, excluded)
    net_bps  >= bps(5)      -> confirm       -> ENTERS early (NOT a miss)
    else                    -> skip (REJECT) -> under the current guard, the real flip is un-enterable = MISS
Counts confirm / fallback_thin / MISS across the ~10-day confirmed-name sample. Polygon bars (accurate
flips) + Polygon trades (accurate net_delta), per name-day, memory freed between names.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar as OBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
DATES = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
         "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
MIN_TICKS, BPS, HOLD_S, BAR_MS = 5, 5.0, 20, 60_000


def _ts_ns(o):
    for a in ("sip_timestamp", "participant_timestamp", "timestamp"):
        v = getattr(o, a, None)
        if v:
            return v
    return None


def main():
    from massive import RESTClient
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=25)
    n_flips = n_graze = confirm = thin = miss = 0
    for date in DATES:
        wins = load_windows(f"/home/trader/wt-atr-study/windows/windows_{date}.json")
        for sym in sorted(wins.keys()):
            try:
                mins = list(c.list_aggs(sym, 1, "minute", date, date, limit=50000))
            except Exception:
                continue
            if len(mins) < 30:
                continue
            mins.sort(key=lambda m: m.timestamp)
            bars = [OBar(m.timestamp, m.open, m.high, m.low, m.close, int(m.volume or 0)) for m in mins]
            rows = compute_atr_trail(bars, period=5, factor=3.5)
            # first graze per short segment + whether the segment ends in a BUY flip
            first_graze = None       # (graze_bar_idx, touch_price)
            seg_grazes = []          # collected (graze_idx, touch_price) for segments ending in BUY
            for i in range(1, len(bars)):
                prev = rows[i - 1]
                if (prev["state"] == "short" and prev["trail"] is not None
                        and bars[i].high >= prev["trail"] and rows[i]["flip"] != "BUY" and first_graze is None):
                    first_graze = (i, prev["trail"])
                if rows[i]["flip"] == "BUY":
                    n_flips += 1
                    if first_graze is not None:
                        seg_grazes.append(first_graze)
                    first_graze = None
                if rows[i]["flip"] == "SELL":
                    first_graze = None
            if not seg_grazes:
                continue
            # fetch trades once for the day (memory freed after)
            y, mo, d = (int(x) for x in date.split("-"))
            lo = int(datetime(y, mo, d, 7, 0, tzinfo=_ET).astimezone(timezone.utc).timestamp() * 1e9)
            hi = int(datetime(y, mo, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc).timestamp() * 1e9)
            try:
                trs = [(int(_ts_ns(t) // 1_000_000), float(t.price)) for t in
                       c.list_trades(sym, timestamp_gte=lo, timestamp_lte=hi, limit=50000) if _ts_ns(t) and getattr(t, "price", None)]
            except Exception:
                continue
            trs.sort()
            tms = [x[0] for x in trs]
            from bisect import bisect_left, bisect_right
            for gi, tp in seg_grazes:
                n_graze += 1
                bstart = bars[gi].ts
                bend = bstart + BAR_MS
                # touch instant = first trade in the graze bar with price >= trail
                lo_i = bisect_left(tms, bstart)
                touch_ms = None
                for j in range(lo_i, len(trs)):
                    if trs[j][0] > bend:
                        break
                    if trs[j][1] >= tp:
                        touch_ms = trs[j][0]
                        break
                if touch_ms is None:
                    touch_ms = bstart              # fallback: bar start
                w_lo = bisect_left(tms, touch_ms)
                w_hi = bisect_right(tms, touch_ms + HOLD_S * 1000)
                window = trs[w_lo:w_hi]
                n_ticks = len(window)
                last_px = window[-1][1] if window else tp
                net_bps = (last_px - tp) / tp * 1e4 if tp else 0.0
                if n_ticks < MIN_TICKS:
                    thin += 1                      # fallback_thin -> ENTERS (excluded)
                elif net_bps >= BPS:
                    confirm += 1                   # confirm -> early entry
                else:
                    miss += 1                      # skip/reject -> real flip MISSED
            del trs, tms

    print("ATR re-arm — the bug's TRUE cost (Polygon bars+trades, hold-confirm modeled)\n")
    print(f"  real BUY flips (sample)          : {n_flips}")
    print(f"  graze-first flips evaluated      : {n_graze}")
    print(f"    -> confirm (entered early)     : {confirm}  ({confirm/n_graze*100:.0f}%)")
    print(f"    -> fallback_thin (Path-B enter): {thin}  ({thin/n_graze*100:.0f}%)  [EXCLUDED per operator]")
    print(f"    -> REJECT => real flip MISSED  : {miss}  ({miss/n_graze*100:.0f}% of grazes, "
          f"{miss/n_flips*100:.0f}% of ALL flips)")
    print(f"\n  >>> TRUE COST = {miss} missed real flips over {len(DATES)} days "
          f"({miss/n_flips*100:.1f}% of all ATR BUY flips) <<<")


if __name__ == "__main__":
    main()
