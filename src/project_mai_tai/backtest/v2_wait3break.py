"""RESEARCH variant (not CI-pinned): ATR-flip -> wait 3 candles -> break-of-3-high entry, swept
across hard-stop buffers, tagged by per-name ATR% volatility.

Reuses the VALIDATED engine core untouched: `compute_atr_trail` (ATR oracle, CI-pinned parity),
`detect_atr_touches` + `_Book`/`_px`/`_run_exit` (from v2_sim, the golden-gated exit ladder), and
`DbMarketDataSource` (feeds). Only the ENTRY rule is new, so the exit/ATR/data trust model carries
over. Entry side stays fully on the Schwab feed (bars for the 3-candle high, quotes for the break,
ask for the fill) exactly like `simulate_v2`; exit ladder on the massive bid.

Entry (operator spec, 2026-07-08): at each ATR flip (operationalised as the variant-B ATR touch =
the live v2 entry signal), WAIT 3 one-minute candles, then ENTER intrabar the moment a Schwab quote
price breaks the highest high of those 3 candles. REPLACES the immediate flip entry (no trade if the
high is never broken before the long thesis dies at the next SELL flip / session end).

Exit: the full live ladder stays active (scales +2/+4%, floor peak-ratchet, 3% trail); only the
HARD-STOP buffer is swept: -1% / -1.5% / -2% / -3% / entry-1.0xATR / entry-1.5xATR.

FEED CAVEAT (inherited): Schwab LEVELONE is sparse -> break detection can miss crosses the live tick
stream would catch, and fills are ~1c conservative. Results are SHAPE + DIRECTIONAL, not penny-exact.
We report setups vs entries so the sparse-feed effect is visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import ATR_FACTOR, ATR_PERIOD, compute_atr_trail
from project_mai_tai.backtest.v2_sim import (
    BAR_MS,
    SCHWAB_LATENCY_S,
    VOL_FLOOR,
    _Book,
    _px,
    _run_exit,
    _utc,
    _v2_cfg,
    detect_atr_touches,
)
from project_mai_tai.exit_logic.engine import ExitEngine

ET = ZoneInfo("America/New_York")
WAIT_N = 3

# The 6 hard-stop buckets. ("fixed", pct) => stop = entry*(1-pct/100);
# ("atr", k) => per-trade stop = entry - k*ATR$  (expressed as a per-trade stop_loss_pct).
STOP_BUCKETS = [
    ("-1.0%", ("fixed", 1.0)),
    ("-1.5%", ("fixed", 1.5)),
    ("-2.0%", ("fixed", 2.0)),
    ("-3.0%", ("fixed", 3.0)),
    ("ATR1.0", ("atr", 1.0)),
    ("ATR1.5", ("atr", 1.5)),
]


@dataclass(frozen=True)
class W3Trade:
    entry_ts: datetime
    entry_price: float
    threshold: float          # 3-candle high broken
    exit_ts: datetime | None
    exit_price: float | None
    qty: int
    pnl: float
    exit_reason: str
    stop_pct_used: float      # the effective hard-stop % applied (per-trade for ATR buckets)


def _atr_series(bars):
    """Per-bar ATR$ (Wilders) = loss/factor from the CI-pinned oracle. None where undefined."""
    rows = compute_atr_trail(bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    return [(r["loss"] / ATR_FACTOR if r["loss"] is not None else None) for r in rows]


def _sell_flip_bars(bars):
    rows = compute_atr_trail(bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    return [i for i, r in enumerate(rows) if r["flip"] == "SELL"]


def atr_pct_rth(bars) -> float | None:
    """Per-name volatility tag = median ATR% (ATR$/close*100) over RTH bars [09:30,16:00) ET."""
    atr = _atr_series(bars)
    vals = []
    for i, b in enumerate(bars):
        if atr[i] is None or b.close <= 0:
            continue
        et = datetime.fromtimestamp(b.ts / 1000, timezone.utc).astimezone(ET)
        if et.weekday() >= 5:
            continue
        mins = et.hour * 60 + et.minute
        if 9 * 60 + 30 <= mins < 16 * 60:
            vals.append(atr[i] / b.close * 100.0)
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _find_setups(bars, *, vol_floor):
    """Yield (touch_bar_idx, threshold, first_watch_bar, watch_end_bar) for each ATR flip that has a
    valid 3-candle wait window before the long thesis dies (next SELL flip) / session end. The break
    is watched over bars [first_watch_bar, watch_end_bar)."""
    sells = _sell_flip_bars(bars)
    n = len(bars)
    out = []
    for bar_idx, _touch_ms, _touch_price in detect_atr_touches(bars):
        if bars[bar_idx].volume <= vol_floor:      # liquidity gate (mirror live vol floor)
            continue
        last_wait = bar_idx + WAIT_N
        if last_wait >= n:                          # not enough candles after the flip
            continue
        threshold = max(bars[bar_idx + k].high for k in range(1, WAIT_N + 1))
        # thesis death = first SELL flip strictly after the flip bar
        next_sell = next((j for j in sells if j > bar_idx), None)
        first_watch = last_wait + 1                 # first bar after the 3 wait candles close
        if next_sell is not None:
            if next_sell <= last_wait:              # thesis died during the wait -> dead setup
                continue
            watch_end = next_sell                   # stop watching once ATR flips short
        else:
            watch_end = n                           # to session end
        if first_watch >= watch_end:
            continue
        out.append((bar_idx, threshold, first_watch, watch_end))
    return out


def simulate_wait3break(schwab_bars, schwab_quotes, massive_quotes, *, qty, stop_mode,
                        vol_floor=VOL_FLOOR, latency_s=SCHWAB_LATENCY_S):
    """One full entry+exit pass for a single stop bucket. stop_mode = ("fixed", pct) | ("atr", k).
    Entries recomputed per bucket because the flat-gate couples entry timing to exit timing."""
    cfg = _v2_cfg()
    engine = ExitEngine(cfg)
    sbook = _Book(schwab_quotes)
    atr = _atr_series(schwab_bars)
    setups = _find_setups(schwab_bars, vol_floor=vol_floor)

    trades: list[W3Trade] = []
    n_setups = len(setups)
    n_breaks = 0
    flat_after = None
    for bar_idx, threshold, first_watch, watch_end in setups:
        # Break DETECTION on dense 1-min bar highs (the bot sees every bar close); first bar whose
        # high crosses the 3-candle high is the break bar. TIMING/fill via the intrabar Schwab
        # quote crossing within that bar; if the sparse feed has none, fall back to that bar's close.
        break_bar = next((j for j in range(first_watch, watch_end) if bars[j].high >= threshold), None)
        if break_bar is None:
            continue                                # high never broken before thesis died
        n_breaks += 1
        bwin = sbook.slice(_utc(bars[break_bar].ts), _utc(bars[break_bar].ts + BAR_MS))
        tq = next((q for q in bwin if _px(q) >= threshold), None)
        break_ts = tq.ts if tq is not None else _utc(bars[break_bar].ts + BAR_MS)
        if flat_after is not None and break_ts < flat_after:
            continue                                # still holding a prior position
        entry_ts = break_ts + timedelta(seconds=latency_s)
        fq = sbook.at(entry_ts)
        if fq is None or fq.ask <= 0:
            continue
        entry_price = fq.ask                        # honest Schwab ask (market buy)

        # resolve the hard-stop % for this trade
        if stop_mode[0] == "fixed":
            stop_pct = stop_mode[1]
        else:                                       # ATR-based: entry - k*ATR$ at the entry bar
            entry_ms = int(entry_ts.timestamp() * 1000)
            last_closed = max((i for i, b in enumerate(schwab_bars)
                               if b.ts + BAR_MS <= entry_ms and atr[i] is not None), default=None)
            if last_closed is None:
                continue
            stop_pct = 100.0 * stop_mode[1] * atr[last_closed] / entry_price
        cfg.stop_loss_pct = stop_pct                # engine reads config.stop_loss_pct at call time

        start = _Book(massive_quotes).index_at_or_after(entry_ts)
        exit_ts, wavg, pnl, reason, _legs = _run_exit(massive_quotes, start, entry_price, qty, cfg, engine)
        trades.append(W3Trade(entry_ts, entry_price, threshold, exit_ts, wavg, qty, pnl, reason, stop_pct))
        flat_after = exit_ts
    return trades, n_setups, n_breaks
