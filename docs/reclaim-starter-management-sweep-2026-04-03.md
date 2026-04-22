# Reclaim Starter Management Sweep - 2026-04-03

## Scope

This sweep tested reclaim starter-management changes across the full recovered universe:

- `AGPU`, `BFRG`, `CYCN`, `ELAB`, `RENX`, `SST` on `2026-04-01`
- `BDRX`, `BFRG`, `COCP`, `PFSA`, `SKYQ`, `TMDE`, `TURB` on `2026-04-02`

Baseline reclaim research profile stayed the same:

- `ticker_loss_pause_streak_limit = 0`
- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

Sweep outputs:

- `tmp_replay/reclaim_starter_management_sweep_full.json`
- `tmp_replay/reclaim_starter_management_sweep_combined.json`
- `tmp_replay/reclaim_starter_management_logic_sweep.json`

Scripts:

- `scripts/reclaim_starter_management_sweep.py`
- `scripts/reclaim_stage_diagnostics.py`

## What Was Tested

### Config-Only Sweeps

- `pretrigger_failed_break_lookahead_bars`
- `pretrigger_fail_hold_buf_atr`
- `pretrigger_add_max_distance_to_ema9_pct`
- `pretrigger_min_bar_rel_vol_breakout`
- mixed combinations of the above

### Logic-Level Sweeps

I added two new reclaim/probe fail-fast config flags:

- `pretrigger_fail_fast_on_macd_below_signal`
- `pretrigger_fail_fast_on_price_below_ema9`

Then I swept:

- disable MACD fail-fast only
- disable EMA9 fail-fast only
- disable both and keep only hold-floor fail-fast
- disable both and also extend no-confirm lookahead to `4`

## Main Result

Every profile produced the same top-line outcome:

- `4` open intents
- `2` good
- `0` bad
- `2` unresolved
- active symbols stayed the same: `AGPU`, `CYCN`, `SKYQ`

This is the strongest conclusion from the sweep.

Starter-management tuning in the current shape does **not** improve the recovered-universe reclaim result.

## What Changed And What Did Not

### No-Confirm Lookahead

- `lookahead_4` and `lookahead_5` did not create more good outcomes
- longer lookahead mostly changed how starters were classified
- by `5` bars, several starters simply turned into `PRETRIGGER_FAIL_FAST`

Conclusion:

- giving reclaim starters more time does not solve the current bottleneck

### Hold-Floor Buffer

- widening `pretrigger_fail_hold_buf_atr` from `0.15` to `0.25` or `0.35` did nothing

Conclusion:

- the ATR hold-floor buffer is not the active limiter on this sample

### Confirm/Add Loosening

- relaxing `pretrigger_add_max_distance_to_ema9_pct`
- relaxing `pretrigger_min_bar_rel_vol_breakout`

did nothing to outcome counts or active symbols

Conclusion:

- existing confirm/add thresholds are not the main blocker either

### Fail-Fast Logic Toggles

#### Disable MACD Fail-Fast Only

- no change

Conclusion:

- MACD-below-signal is not the important fail-fast trigger on this sample

#### Disable EMA9 Fail-Fast Only

- the single `starter_fail_fast` became `starter_no_confirm`
- top-line outcomes still did not improve

Conclusion:

- price-below-EMA9 is the active fail-fast trigger that mattered
- but removing it alone does not create a better reclaim result

#### Hold-Floor-Only Fail-Fast

- same result as disabling EMA9 fail-fast only
- starters shifted from `fail_fast` to `no_confirm`
- outcomes stayed the same

Conclusion:

- fail-fast is not the core remaining limiter
- it is only changing how the same starters eventually die

## Best Interpretation

The earlier stage diagnostics already suggested that the next weak link was post-starter management.

This sweep refines that conclusion:

- it is **not** enough to loosen current starter-management knobs
- it is **not** enough to turn off the EMA9 or MACD fail-fast checks

The current confirm/no-confirm model is likely the bigger bottleneck now.

## Best Next Step

Do not keep sweeping the same starter-management knobs.

The next useful work should target confirmation logic itself:

1. allow reclaim starters to become valid without requiring the current confirmation shape to fire exactly
2. consider a reclaim-specific confirmation path instead of reusing the probe confirmation model
3. test a 1-bar armed-break confirmation for reclaim:
   - reclaim bar qualifies
   - next bar breaks reclaim high
   - if not, expire

That direction fits the evidence better than more tuning of:

- no-confirm lookahead
- hold-floor buffer
- breakout volume
- add distance
- EMA9 fail-fast
- MACD fail-fast

## Bottom Line

This sweep ruled out a large chunk of the remaining starter-management parameter space.

That is useful progress:

- the starter-management knobs are mostly not where the edge is hiding
- the next reclaim research should move from parameter tuning to confirmation-model design
