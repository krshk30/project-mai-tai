# Reclaim Baseline Update (2026-04-07)

This update rolls the best current reclaim research settings into the reclaim variant itself.

## What Moved Into The Reclaim Variant

`make_30s_reclaim_variant()` now uses this research baseline by default:

- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_require_volume = false`
- `pretrigger_reclaim_require_pullback_absorption = false`
- `pretrigger_reclaim_require_stoch = false`
- `pretrigger_failed_break_lookahead_bars = 4`

These are still research defaults, not a statement that reclaim is live-ready.

## Why These Were Chosen

Broad sweeps after the staged reclaim rewrite showed:

- `volume_off` was the cleanest single-gate improvement over the staged reclaim baseline.
- `volume_off + pullback_volume_off` improved further and made `location` the clear next blocker.
- Location cap tweaks (`4%`, `5%`, `6%`) on that mixed profile were almost flat, so the blocker is not just extension percentages.
- `volume_off + stoch_off` became the strongest mixed profile tested so far on the current reclaim baseline.

Useful files:

- [reclaim_gate_knockout_sweep.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_gate_knockout_sweep.json)
- [reclaim_gate_additive_sweep.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_gate_additive_sweep.json)
- [reclaim_mix_sweep.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_mix_sweep.json)
- [reclaim_location_mix_sweep_2026-04-07.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_location_mix_sweep_2026-04-07.json)
- [reclaim_stage_diag_volume_stoch_off.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_stage_diag_volume_stoch_off.json)

## Best Current Read

With the new reclaim baseline plus `stoch_off`, the top remaining blockers are:

- `pretrigger reclaim location not ready`
- `pretrigger reclaim pullback not ready`
- `pretrigger reclaim trend not ready`
- `pretrigger reclaim momentum not ready`

That means the next reclaim work should focus on:

1. location modeling, not just extension percentages
2. pullback modeling
3. confirmation / no-confirm behavior

Not worth spending more time on right now:

- touch gate
- reclaim candle micromanagement
- basic score threshold tuning
