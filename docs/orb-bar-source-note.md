# ORB bar source — is it the v2 "wrong bars" defect? (finding, 2026-07-22)

> Follow-up to [[project_mai_tai_bar_source_defect]] (v2: backtests used Polygon bars while live
> decides on Schwab; 54.2% ATR-flip agreement). The standing note said "⛔ ORB has the mirror
> problem." **Verified here — and it is materially LESS severe than v2. Do not copy the v2
> conclusion onto ORB.**

## What was actually checked

1. **ORB live** (`services/orb_app.py`): builds 1-min decision bars from the market-data gateway
   (Massive/**Polygon**) trade-tick stream via `OrbTickAggregator`.
2. **ORB canonical backtest** (`python -m project_mai_tai.backtest`, `backtest/__main__.py` →
   `build_bars(src.trades(...))`): builds bars from `market_capture_trades` (**Polygon**) via the
   **same** `OrbTickAggregator`.

⇒ **The canonical ORB backtest and ORB live use the SAME source (Polygon), the same aggregator.**
This is the opposite of v2, whose canonical backtest used Polygon while live used Schwab. **The
in-repo ORB engine does not have the mirror problem** — it already matches production.

## Where a mismatch could still exist

- **Schwab-REST-sourced ORB studies.** The fleet-roster note records that some ORB *exit research*
  pulled 1-min bars from Schwab REST `pricehistory` (validated against the operator's TradingView).
  Any conclusion drawn on Schwab-REST bars is on a different source than ORB live (Polygon) — that
  IS a mismatch, confined to those specific studies, not the deployed engine.
- **Off-tree ad-hoc scripts** (`/home/trader/orb_*_bt.py`) read from CSV globs; their provenance
  varies and should not be treated as canonical.

## Why the v2 severity does NOT transfer

v2's defect was amplified by a **recursive** indicator: the ATR trailing stop compounds a 12 bps
close difference into opposite states that persist for a whole segment → only 54% of flips agreed.

**ORB's signal is a running-high BREAKOUT — not recursive.** The decision is "did price exceed the
opening-range high?" A small per-bar vendor difference shifts the break level by cents and the
entry by at most a bar; it does not compound. So even on the studies that used Schwab bars, the
expected divergence is far smaller than v2's 54% — closer to the raw ~0-10% bar-content difference,
not the amplified figure.

## Recommendation (no code)

1. **Trust the canonical in-repo ORB backtest** — it matches live. Prefer it over the off-tree scripts.
2. **Flag, don't rerun-in-a-panic:** any ORB conclusion that came specifically from Schwab-REST
   bars should be re-checked on the Polygon canonical engine before being trusted — but the
   priority is LOW (non-recursive signal, small expected divergence), unlike v2 where it was
   invalidating.
3. **If a precise number is wanted:** run the same break-level divergence check used for v2's ATR
   flips — count how often the opening-range-high break time differs between a Polygon-built and a
   Schwab-built bar series on the same ORB names. Pre-stated expectation: **small** (single-digit
   percent), because the signal is a level crossing, not a recursion.

## Status
Finding recorded. Corrects the overstated "ORB has the mirror problem" flag: the canonical engine
already matches live; residual risk is confined to Schwab-REST studies and is low-severity because
the breakout signal is non-recursive.
