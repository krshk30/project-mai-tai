"""ATR Trailing-Stop oracle — VENDORED from analysis/atr_flip.py::compute_atr_trail.

Vendored (not imported) because `analysis/` is not an installed package and atr_flip.py imports
the Schwab REST client at module level — neither works in CI. This is the SAME pattern the live
v2 uses (schwab_1m_v2.py ports the oracle, pinned by a determinism test). The copy is kept
provably identical by tests/backtest/test_v2_golden.py::test_atr_oracle_parity (compares this to
the original analysis/atr_flip on the golden bars) — so it cannot drift undetected.

Pure function; TOS "ATR Trailing Stop" replica (ATRPeriod=5, ATRFactor=3.5, Wilders, seed=sma5).
flip='BUY' = short->long (entry signal); 'SELL' = long->short.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ATR_PERIOD = 5
ATR_FACTOR = 3.5


@dataclass
class Bar:
    ts: int  # bar-start epoch ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: int


def _row(b, tr, loss, trail, state, flip, age=None) -> dict:
    et = datetime.fromtimestamp(b.ts / 1000, timezone.utc).astimezone(ET).strftime("%H:%M")
    return {"et": et, "ts": b.ts, "close": round(b.close, 4),
            "tr": round(tr, 4) if tr is not None else None,
            "loss": round(loss, 4) if loss is not None else None,
            "trail": round(trail, 4) if trail is not None else None,
            "state": state, "flip": flip, "state_age": age, "vol": b.volume}


def compute_atr_trail(bars, *, seed: str = "sma5", period: int = ATR_PERIOD,
                      factor: float = ATR_FACTOR) -> list[dict]:
    """Per-bar rows: modified true range, Wilders loss, trail, state, flip. `bars` = one
    ascending session (state inits at bars[0]'s neighborhood)."""
    n = len(bars)
    hl = [b.high - b.low for b in bars]

    def sma(arr, i, p):
        return sum(arr[i - p + 1:i + 1]) / p if i >= p - 1 else None

    tr: list[float | None] = [None] * n
    for i in range(1, n):
        s = sma(hl, i, period)
        if s is None:
            continue
        prev, cur = bars[i - 1], bars[i]
        hilo = min(hl[i], 1.5 * s)
        href = (cur.high - prev.close) if cur.low <= prev.high \
            else (cur.high - prev.close) - 0.5 * (cur.low - prev.high)
        lref = (prev.close - cur.low) if cur.high >= prev.low \
            else (prev.close - cur.low) - 0.5 * (prev.low - cur.high)
        tr[i] = max(hilo, href, lref)

    valid = [i for i in range(n) if tr[i] is not None]
    w: list[float | None] = [None] * n
    if len(valid) >= (period if seed == "sma5" else 1):
        if seed == "sma5":
            seed_idx = valid[:period]
            prev_w = sum(tr[i] for i in seed_idx) / period
            start = seed_idx[-1]
        else:
            start = valid[0]
            prev_w = tr[start]
        w[start] = prev_w
        for i in range(start + 1, n):
            if tr[i] is None:
                continue
            prev_w = prev_w + (tr[i] - prev_w) / period
            w[i] = prev_w
    loss = [factor * w[i] if w[i] is not None else None for i in range(n)]

    rows: list[dict] = []
    cur_state: str | None = None
    cur_trail: float | None = None
    age = 0
    for i in range(n):
        b = bars[i]
        flip = None
        if loss[i] is None:
            rows.append(_row(b, tr[i], loss[i], None, None, None))
            continue
        if cur_state is None:
            cur_state, cur_trail, age = "long", b.close - loss[i], 0
        else:
            age += 1
            if cur_state == "long":
                if b.close > cur_trail:
                    cur_trail = max(cur_trail, b.close - loss[i])
                else:
                    cur_state, cur_trail, flip, age = "short", b.close + loss[i], "SELL", 0
            else:
                if b.close < cur_trail:
                    cur_trail = min(cur_trail, b.close + loss[i])
                else:
                    cur_state, cur_trail, flip, age = "long", b.close - loss[i], "BUY", 0
        rows.append(_row(b, tr[i], loss[i], cur_trail, cur_state, flip, age))
    return rows
