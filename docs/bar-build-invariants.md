# Bar-build invariants

What "the true bar" means for each bot, and which fields can be cross-checked against which sources. This exists because the validator (`scripts/check_bar_build_runtime.py`) and the dashboard are easy to misread when the operator forgets that different bots define their bars from different upstream feeds.

## Quick reference

| Bot          | Primary feed       | HIGH/LOW source                | Volume source                  | Late-trade revision |
|--------------|--------------------|--------------------------------|--------------------------------|---------------------|
| `macd_30s`   | Schwab LEVELONE_EQUITIES (TIMESALE) | TIMESALE-built (tick-by-tick)  | TIMESALE-built (`cum_vol` delta) | Yes (PR #77 path)   |
| `polygon_30s`| Polygon trade ticks | Polygon-tick-built             | Polygon-tick-built             | Yes (`on_bar` mirror) |
| `schwab_1m`  | Schwab CHART_EQUITY (1-minute final bars) | **CHART** (verbatim)        | **CHART** (verbatim)        | **No** (CHART is canonical) |

## Why schwab_1m HIGH/LOW cannot be validated against TIMESALE

For `schwab_1m`, `live_aggregate_bars_are_final=True`. The persistence path writes CHART's OHLC straight to `strategy_bar_history`; the TIMESALE late-trade revision (`schwab_native_30s.py::_revise_last_closed_bar_from_trade`) is intentionally short-circuited when `_last_closed_bar_from_aggregate=True`.

Schwab's TIMESALE stream systematically drops a large fraction of executions on heavy-volume bars. Worked example from 2026-05-14 MOBX 09:34 ET:

- CHART volume: 2,273,464
- Sum of TIMESALE trade-event sizes in the same minute: 29,404
- Ratio: 77× undercount

The missing prints carry the bar HIGH. Concretely on 2026-05-14:

| Symbol | Bar (ET) | Rebuilt H | CHART H (persisted) | Δ (¢) | Δ%   |
|--------|----------|-----------|---------------------|-------|------|
| MOBX   | 09:34    | 3.2600    | 3.4100              | 15    | 4.60 |
| MOBX   | 09:42    | 4.1101    | 4.2500              | 14    | 3.40 |
| OCG    | 09:25    | 2.1000    | 2.2000              | 10    | 4.76 |

Across the full 2026-05-14 RTH session, 579 schwab_1m bars (22 symbols) showed this pattern; 91% are `persisted_HIGH > rebuilt_HIGH`. Penny stocks dominate (87% of outliers are <$5).

The bot trades against CHART HIGH — that is the live execution reality. Sourcing HIGH from TIMESALE rebuild would degrade fidelity AND drift the dashboard from what the bot actually sees.

**Validator behavior since this doc**: `scripts/check_bar_build_runtime.py` skips HIGH/LOW comparison when `--strategy-code schwab_1m`. Only OPEN/CLOSE price diffs and volume diffs are surfaced. See issue #144 for full forensics.

## Where the invariants matter

- **macd_30s**: a HIGH discrepancy IS a bug to investigate — the bot's TIMESALE bar is what the strategy is reading. Issue #130 tracks one such pattern.
- **polygon_30s**: Polygon ticks come from a different SIP than Schwab's TIMESALE, so they can disagree with macd_30s on volume by 10-20% in normal operation. This is provider-feed difference, not a bug.
- **schwab_1m**: only CLOSE/volume comparisons against CHART are meaningful. HIGH/LOW comparisons against TIMESALE are noise (see above).

## Persist-lag validation (signal timing)

Independent of OHLC correctness, every bar should reach `strategy_bar_history` within seconds of its scheduled close. Persistence delays indicate the strategy event loop was blocked (hydration, sync I/O, etc.) and signals will fire late — exactly the GOVX 2026-05-18 07:08:40 ET incident, where a P1_CROSS confirmation bar persisted 40s late and the buy filled after the move was over.

`scripts/check_bar_persist_lag.py` audits this directly from `strategy_bar_history`:

```bash
# All bots, today, default thresholds
PYTHONPATH=/home/trader/project-mai-tai/src \
  PGPASSWORD=$PGPASSWORD \
  /home/trader/project-mai-tai/.venv/bin/python \
  /home/trader/project-mai-tai/scripts/check_bar_persist_lag.py \
  --day YYYY-MM-DD --all-bots --dsn "$DSN"
```

Thresholds (default):

| Interval | Warn  | Error |
|----------|-------|-------|
| 30s bots | 15s   | 30s   |
| 60s bot  | 30s   | 60s   |

Exit code 0 if every bar is under the error threshold, 1 otherwise. Cron-friendly. Run it as part of daily bar-build validation alongside `check_bar_build_runtime.py` and `backtest_validate_trades.py`.

The 2026-05-18 incident would have surfaced as: `macd_30s GOVX bar=07:07:30 persisted=07:08:40 lag=40.0s ERROR`.
