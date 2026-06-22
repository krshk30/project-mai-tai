"""Path 3 — ATR Trailing-Stop Flip indicator (TOS "ATR Trailing Stop" replica).

Phase 0 of docs/path3-atr-flip-plan.md: replicate the indicator EXACTLY so the
flip points match the operator's TOS chart, THEN parity-check before any backtest.
Read-only. No production code, no strategy/OMS change.

Script params: ATRPeriod=5, ATRFactor=3.5, average=Wilders, trailType=modified,
firstTrade=long. Evaluated on bar close. State resets at the session start
(04:00 ET / the chart-left), matching the TOS 1-day chart + the VWAP anchor.

⚠️ SEEDING NOTE (the documented choice the parity check validates): the plan
specifies Wilders(trueRange,5) seeded with the **SMA of the first 5 trueRange
values**. Real ThinkScript `WildersAverage` instead seeds with the **first**
value (recursive from bar 1). The two converge within ~15 bars (alpha=1/5), so
only the EARLY-session flips can differ. `--seed sma5|first` lets the parity
check pick whichever matches TOS; default sma5 per the plan.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings

ET = ZoneInfo("America/New_York")
ATR_PERIOD = 5
ATR_FACTOR = 3.5


@dataclass
class Bar:
    ts: int            # bar-start epoch ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: int


def compute_atr_trail(bars: list[Bar], *, seed: str = "sma5",
                      period: int = ATR_PERIOD, factor: float = ATR_FACTOR) -> list[dict]:
    """Return per-bar rows with the modified true range, Wilders loss, trail,
    state, and flip markers. `bars` must be one ascending session (state inits
    at bars[0]'s neighborhood). flip='BUY' = short->long (the entry signal);
    'SELL' = long->short."""
    n = len(bars)
    hl = [b.high - b.low for b in bars]

    def sma(arr, i, p):
        return sum(arr[i - p + 1:i + 1]) / p if i >= p - 1 else None

    # --- modified true range (needs prev bar; HiLo needs SMA(high-low,5)) ---
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

    # --- Wilders(tr, period) -> loss ---
    valid = [i for i in range(n) if tr[i] is not None]
    w: list[float | None] = [None] * n
    if len(valid) >= (period if seed == "sma5" else 1):
        if seed == "sma5":
            seed_idx = valid[:period]
            prev_w = sum(tr[i] for i in seed_idx) / period
            start = seed_idx[-1]
        else:  # first
            start = valid[0]
            prev_w = tr[start]
        w[start] = prev_w
        for i in range(start + 1, n):
            if tr[i] is None:
                continue
            prev_w = prev_w + (tr[i] - prev_w) / period
            w[i] = prev_w
    loss = [factor * w[i] if w[i] is not None else None for i in range(n)]

    # --- flip state machine (compares close vs PRIOR trail) ---
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
        if cur_state is None:                         # init at first valid loss
            cur_state, cur_trail, age = "long", b.close - loss[i], 0
        else:
            age += 1
            if cur_state == "long":
                if b.close > cur_trail:
                    cur_trail = max(cur_trail, b.close - loss[i])  # ratchet up
                else:
                    cur_state, cur_trail, flip, age = "short", b.close + loss[i], "SELL", 0
            else:  # short
                if b.close < cur_trail:
                    cur_trail = min(cur_trail, b.close + loss[i])  # ratchet down
                else:
                    cur_state, cur_trail, flip, age = "long", b.close - loss[i], "BUY", 0
        rows.append(_row(b, tr[i], loss[i], cur_trail, cur_state, flip, age))
    return rows


def _row(b, tr, loss, trail, state, flip, age=None) -> dict:
    et = datetime.fromtimestamp(b.ts / 1000, UTC).astimezone(ET).strftime("%H:%M")
    return {"et": et, "ts": b.ts, "close": round(b.close, 4),
            "tr": round(tr, 4) if tr is not None else None,
            "loss": round(loss, 4) if loss is not None else None,
            "trail": round(trail, 4) if trail is not None else None,
            "state": state, "flip": flip, "state_age": age, "vol": b.volume}


# ----------------------------- parity CLI --------------------------------

def fetch_day(client, settings, sym, day) -> list[Bar]:
    """Fetch 1m bars for the ET trading SESSION of `day` (04:00→20:00 ET), so the
    state inits at 04:00 ET (the reset anchor / chart-left) and matches a TOS
    1-day extended-hours chart. Bars outside that ET window are excluded — using
    UTC-day boundaries spans the 04:00-ET reset and pollutes the state."""
    d = datetime.strptime(day, "%Y-%m-%d")
    sess_start = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)    # 04:00 ET
    sess_end = datetime(d.year, d.month, d.day, 20, 0, tzinfo=ET)     # 20:00 ET
    lo, hi = int(sess_start.timestamp() * 1000), int(sess_end.timestamp() * 1000)
    # fetch a UTC-day-padded window then slice to the ET session
    params = urlencode({"symbol": sym, "periodType": "day", "frequencyType": "minute",
                        "frequency": 1, "startDate": lo,
                        "endDate": hi, "needExtendedHoursData": "true"})
    url = f"{settings.schwab_base_url.rstrip('/')}{client.PRICE_HISTORY_PATH}?{params}"
    out = []
    for c in (client._authorized_get(url).get("candles") or []):  # noqa: SLF001
        if not isinstance(c, dict):
            continue
        ts = int(c.get("datetime", 0) or 0)
        if not (lo <= ts < hi):
            continue
        out.append(Bar(ts, float(c.get("open", 0) or 0), float(c.get("high", 0) or 0),
                       float(c.get("low", 0) or 0), float(c.get("close", 0) or 0),
                       int(float(c.get("volume", 0) or 0))))
    out.sort(key=lambda b: b.ts)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--symbols", required=True, help="CSV")
    ap.add_argument("--seed", choices=["sma5", "first"], default="sma5")
    ap.add_argument("--out", default="/tmp/atr_parity.json")
    args = ap.parse_args()
    settings = Settings()
    client = SchwabV2RestClient(settings, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)

    report = {"day": args.day, "seed": args.seed, "symbols": {}}
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        bars = fetch_day(client, settings, sym, args.day)
        rows = compute_atr_trail(bars, seed=args.seed)
        flips = [r for r in rows if r["flip"]]
        report["symbols"][sym] = {"bars": len(bars), "flips": flips, "rows": rows}
        print(f"\n=== {sym} {args.day}  (bars={len(bars)}, seed={args.seed}) ===")
        if not flips:
            print("  no flips")
        for f in flips:
            print(f"  {f['et']} ET  {f['flip']:4}  close={f['close']:<8} trail={f['trail']:<8} "
                  f"loss={f['loss']:<7} vol={f['vol']}")
        # show a small trail-series window around the first BUY for eyeballing
        buys = [i for i, r in enumerate(rows) if r["flip"] == "BUY"]
        if buys:
            j = buys[0]
            print("  trail series around first BUY (et close trail state):")
            for r in rows[max(0, j - 2):j + 4]:
                print(f"    {r['et']} c={r['close']:<8} trail={r['trail']} {r['state']} "
                      f"{'<<BUY' if r['flip']=='BUY' else ''}")
    json.dump(report, open(args.out, "w"), default=str, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
