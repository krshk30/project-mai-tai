"""Assessment only: unmodified production ATR methods against the own stored bars."""
import json
import logging
import re
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

logging.disable(logging.CRITICAL)
ET = ZoneInfo("America/New_York")
data = json.load(open(sys.argv[1]))
holds = sorted((h for h in data["holds"] if h["at"] >= "2026-09-28"), key=lambda h: h["at"])


def dt(value):
    return datetime.fromisoformat(value)


def stamp(value):
    return int(dt(value).timestamp() * 1000)


def kind(h):
    return "MARKET SILENT" if not (h["market_trade_ticks"]["count"] + h["market_capture_trades"]["count"]) else "BARS MISSING"


def math_rows(raw, events, keep_silent):
    strategy = SchwabV2Strategy(Settings(_env_file=None))
    state = strategy.watchlist_state("REPLAY")
    idx = 0
    active = None
    previous = 0
    contiguous = 0
    output = []
    for b in raw:
        t = stamp(b["bar_time"])
        while idx < len(events) and stamp(events[idx]["at"]) < t + 60000:
            active = events[idx]
            previous = contiguous = 0
            if not (keep_silent and kind(active) == "MARKET SILENT"):
                strategy._reset_atr_indicator_state(state, int(datetime.fromtimestamp(t / 1000, UTC).astimezone(ET).replace(hour=4, minute=0, second=0).timestamp() * 1000))
            idx += 1
        if active is not None:
            if previous and t - previous > strategy._gap_hold_detect_ms:
                # Classification of recovery holes is not reconstructed by this first-pass census.
                strategy._reset_atr_indicator_state(state, state.atr_session_anchor_ms)
                contiguous = 0
            previous = t
            contiguous += 1
            if contiguous >= 2 * strategy._atr_period:
                active = None
        bar = OHLCVBar(t, *(float(b[k]) for k in ("open_price", "high_price", "low_price", "close_price")), int(b["volume"]))
        state.bars.append(bar)
        signal = strategy._update_atr_state(state, bar, state_only=True)
        output.append({"ts": t, "state": state.atr_state, "trail": state.atr_trail,
                       "flip": (signal or {}).get("flip"), "in_wait": active is not None})
    return output


sessions = {}
for key, bars in data["bars"].items():
    date, symbol = key.split(":")
    events = [h for h in holds if h["symbol"] == symbol and h["at"][:10] == date]
    oracle = compute_atr_trail([Bar(stamp(b["bar_time"]), *(float(b[k]) for k in ("open_price", "high_price", "low_price", "close_price")), int(b["volume"])) for b in bars])
    sessions[key] = {"n": len(bars), "oracle": oracle, "today_math": math_rows(bars, events, False),
                     "carry_only_math": math_rows(bars, events, True)}

rows = []
for h in holds:
    key = f"{dt(h['at']).astimezone(ET).date()}:{h['symbol']}"
    series = sessions[key]
    before = (h.get("before") or {}).get("line", "")
    after = (h.get("first_after") or {}).get("line", "")
    matched = re.search(r"ts_ms=(\d+)", after)
    after_ts = int(matched.group(1)) if matched else 0
    result = {"symbol": h["symbol"], "at_et": dt(h["at"]).astimezone(ET).isoformat(), "kind": kind(h),
              "schwab_prints": h["market_trade_ticks"]["count"], "capture_prints": h["market_capture_trades"]["count"],
              "before_log": before, "after_log": after, "after_bar_ms": after_ts}
    for name in ("oracle", "today_math", "carry_only_math"):
        result[name] = next((r for r in series[name] if r["ts"] == after_ts), None)
    rows.append(result)

result = {"population": {"all_holds": len(data["holds"]), "recovery_holds": len(data["recoveries"]),
                         "post0928": len(holds), "silent": sum(kind(h) == "MARKET SILENT" for h in holds)},
          "holds": rows, "sessions": sessions, "entry_rows": data["entry_rows"]}
print(json.dumps(result, indent=2))
