# Validated backtest engine (`project_mai_tai.backtest`)

The **single trusted path** for backtesting. Replaces the throwaway `/tmp` scripts that let ≥3
real bugs change strategy conclusions (the exit-model fake-win; the CELZ bar-based-running-high
phantom re-entries — 93/−$39 shown for a real 23/+$1.91; SDOT never chart-checked). Design:
`docs/backtest-engine-design.md`.

## Run it (the only supported entry point)
```
# one symbol:
python -m project_mai_tai.backtest SYMBOL YYYY-MM-DD [--strategy orb|v2] [--mode bar_close|intrabar] [--capped]
# full daily sheet (ALL qualified names, each with a reason — no silent absence):
python -m project_mai_tai.backtest YYYY-MM-DD --strategy orb|v2 --sheet
```
ORB reports P&L across the **measured per-broker latency band** — never a single point. `--sheet`
enumerates the qualified universe (v2 = tracked∪traded; ORB = window-captured∪traded) and prints
every name with trades OR an explicit reason (SKIP-no-feed / 0t-no-signal). Run after close for a
final sheet (intraday capture is still filling in).

## Why it is trustworthy (validated against ground truth, not assumed)
- **Decision source = `market_capture_trades`** (the live gateway stream the bot actually saw),
  built into 1-min bars with the LIVE `OrbTickAggregator`. `market_capture_bars` (REST aggs) is
  only a parity cross-check — the sources legitimately differ ~0.5%.
- **Honest fills (Option A):** pay the ask at placement, sell the bid; charge full spread; never
  assume price improvement. The real broker fill is reported alongside (Δ = the surfaced edge).
- **Per-broker latency BAND (measured, not assumed):** Webull 3s (liquid) → 14s (thin),
  measured from real ORB fills. Schwab/v2 gets its OWN measured band before v2 is trusted.
- **Reclaim = eager upper bound** (racy live event; bounded, not faked).
- **Exit mirrors the live OMS** (`_ratcheted_trailing_stop`: HWM=fill price, bid-only ratchet).

## The CI gate = the enforcement (`tests/backtest/test_golden.py`)
Conclusions are trusted ONLY when these pass in CI (run on committed fixtures, no DB):
- **KIDZ 07-06** — real broker-fill anchor: modeled −$0.20 vs real −$0.175, exact shape.
- **CELZ 06-30** — bar-close count = 5; **phantom-free** (intrabar ≪ 93).
- **Intrabar parity** — two independent implementations agree exactly (intrabar's substitute for
  the missing real-fill anchor, since the live bot only trades bar-close).

## Do NOT add throwaway backtest scripts
Add a strategy adapter to the engine + a hand-verified golden case instead. The superseded
scripts are quarantined in `scripts/legacy/` (kept for reference only; `orb_fill_slippage.py`
remains as the DB real-fill reporter that cross-checks modeled vs actual fills).

## Scope — two strategies
- **ORB running-high** (`--strategy orb`): Polygon stream, Webull latency band. bar-close =
  live-faithful; intrabar = re-adjudication mode.
- **ATR/v2** (`--strategy v2`): THREE feeds — ATR signal + entry fill on **Schwab** LEVELONE
  (`strategy_bar_history` + `market_quote_ticks`), exit ladder (`ExitEngine`) on the **massive**
  bid, Schwab ~0s latency. Entry = variant-B ATR touch (vendored `atr_oracle`, pinned to
  `analysis/atr_flip`) + intrabar hold-confirm. **FEED-LIMITED**: the Schwab LEVELONE capture is
  sparse (anchor ~1¢ conservative) with coverage gaps (some names have no bars/ticks). Trustworthy
  for **shape + directional P&L**, not penny-exact — read v2 numbers as directional. See the
  future market-data-capture investigation in the reports log for widening v2 coverage.

P1/P3/P5 extend from here — each needs its own broker latency band + golden cases before its
conclusions are trusted.
