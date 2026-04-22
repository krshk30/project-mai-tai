# 30s Probe/Reclaim Recommendation Review - 2026-04-03

## Scope

This note compares the current 30s implementation against the latest Probe/Reclaim recommendations and the saved-session replay results from:

- `CYCN` on `2026-04-01`
- `RENX` on `2026-04-01`
- `TMDE` on `2026-04-02`
- `SKYQ` on `2026-04-02`

The replay tool is:

- `scripts/compare_30s_variants.py`

Saved replay outputs used here:

- `tmp_replay/analysis_cycn_apr01.json`
- `tmp_replay/analysis_renx_apr01.json`
- `tmp_replay/analysis_tmde_apr02.json`
- `tmp_replay/analysis_skyq_apr02.json`
- `tmp_replay/analysis_cycn_apr01_reclaim_tuned.json`
- `tmp_replay/analysis_renx_apr01_reclaim_tuned.json`
- `tmp_replay/analysis_tmde_apr02_reclaim_tuned.json`
- `tmp_replay/analysis_skyq_apr02_reclaim_tuned.json`

## Current Fit

### Probe

Current Probe still behaves like a compression-shelf strategy:

- hard MACD-above-signal gate is still present
- no explicit 10-bar shelf timer
- no declining-volume shelf filter
- no target-universe distinction between shelf names and small-cap spike runners

Conclusion:

- For the current small-cap runner universe, Probe is still a mismatch.
- Keep it available as a separate bot, but do not prioritize it for live promotion on CYCN/RENX/SKYQ/TMDE-style names.

### Reclaim

Current Reclaim is much closer to the desired runner model:

- reclaim lookback already defaults to `8` bars
- current bar can optionally satisfy touch
- EMA9/VWAP reclaim logic already exists
- candle, location, volume, momentum, and trend checks already exist

The recommended Reclaim parameter changes are mostly compatible with current config overrides:

- `pretrigger_reclaim_touch_lookback_bars`
- `pretrigger_reclaim_min_pullback_from_high_pct`
- `pretrigger_reclaim_max_pullback_from_high_pct`
- `pretrigger_reclaim_max_extension_above_ema9_pct`
- `pretrigger_reclaim_max_extension_above_vwap_pct`

The recommended new Reclaim filters are not implemented yet:

- higher-low versus pre-spike price
- pullback-volume absorption
- held-percent-of-move filter

Update after implementation:

- these three Reclaim runner filters are now implemented as tunable config fields in the reclaim path
- they are available through the existing 30s config override plumbing
- focused decision-layer and runtime-config tests were added for them

## Replay Comparison

Reclaim baseline vs tuned replay used these overrides:

```json
{
  "pretrigger_reclaim_touch_lookback_bars": 8,
  "pretrigger_reclaim_min_pullback_from_high_pct": 0.015,
  "pretrigger_reclaim_max_pullback_from_high_pct": 0.15,
  "pretrigger_reclaim_max_extension_above_ema9_pct": 0.04,
  "pretrigger_reclaim_max_extension_above_vwap_pct": 0.05
}
```

### CYCN

- baseline reclaim: `0` intents
- tuned reclaim: `0` intents
- baseline top blockers:
  - `pullback not ready: 488`
  - `location not ready: 175`
  - `candle not ready: 123`
- tuned top blockers:
  - `candle not ready: 330`
  - `pullback not ready: 225`
  - `location not ready: 190`

Interpretation:

- the recommended reclaim settings helped
- pullback stopped being the dominant blocker
- candle quality is now the biggest gate

### RENX

- baseline reclaim: `0` intents
- tuned reclaim: `0` intents
- baseline top blockers:
  - `pullback not ready: 597`
  - `location not ready: 176`
  - `candle not ready: 49`
- tuned top blockers:
  - `pullback not ready: 381`
  - `location not ready: 309`
  - `candle not ready: 115`

Interpretation:

- pullback gating improved materially
- location and candle now block more often
- runner-style reclaim is closer, but not ready yet

### TMDE

- baseline reclaim: `0` intents
- tuned reclaim: `0` intents
- baseline top blockers:
  - `pullback not ready: 168`
  - `location not ready: 74`
  - `candle not ready: 29`
- tuned top blockers:
  - `pullback not ready: 102`
  - `location not ready: 92`
  - `candle not ready: 73`

Interpretation:

- pullback improved
- location and candle remain the main next gates

### SKYQ

- baseline reclaim: `0` intents
- tuned reclaim: `0` intents
- both runs dominated by outside-hours bars

Interpretation:

- this is not a clean tuning sample until the replay window is trimmed

## Replay After Runner Filters

After implementing the runner-specific Reclaim filters and replaying the same runner-style overrides, the blocker profile changed again:

### CYCN

- reclaim intents: `0`
- top blockers:
  - `higher low not ready: 475`
  - `pullback not ready: 234`
  - `held move not ready: 69`
  - `candle not ready: 44`
  - `pullback volume not ready: 17`

### RENX

- reclaim intents: `0`
- top blockers:
  - `higher low not ready: 400`
  - `pullback not ready: 381`
  - `held move not ready: 25`
  - `pullback volume not ready: 10`

### TMDE

- reclaim intents: `0`
- top blockers:
  - `higher low not ready: 134`
  - `pullback not ready: 102`
  - `held move not ready: 21`
  - `candle not ready: 14`
  - `pullback volume not ready: 14`

Interpretation:

- the new runner filters are active and measurable
- on these saved sessions, the dominant issue is now structural quality of the pullback itself
- that is useful because it confirms the next problem is not just candle/location tuning
- these names often did not hold enough of the first move to qualify as a clean second-leg reclaim

## What To Change Now

### Keep

- keep the structural fixes already implemented:
  - separate Probe/Reclaim bots
  - reclaim runtime wiring fix
  - aggregate-bar fallback
  - env-driven 30s override plumbing

- keep 30s decisions on local indicators
  - provider overlay remains audit-only for 30s and should stay that way

### Do Not Change Yet

- do not rewrite Probe around the current runner universe yet
- do not bake the new Reclaim parameter values in as hard defaults yet

Reason:

- the new Reclaim settings clearly shift the blocker profile in the right direction, but they still produce `0` entries on the saved runner sessions
- that means the next bottleneck is not only touch/pullback

## Next Items

### Reclaim Priority

Implement the missing runner-specific Reclaim filters:

1. review whether `higher low` is too strict for the current small-cap runner universe
2. review whether the `held move` threshold should stay at `50%`
3. review whether pullback-volume absorption should use average pullback volume, max pullback volume, or a shorter subset of pullback bars

Then replay again on `CYCN`, `RENX`, and `TMDE`.

### Reclaim Candle/Location Review

After the runner-specific filters are added, inspect whether these current checks are still too strict for fast small-cap second legs:

- `pretrigger_reclaim_min_close_pos_pct`
- `pretrigger_reclaim_max_upper_wick_pct`
- `pretrigger_reclaim_min_body_pct`
- reclaim location relative to EMA9/VWAP extension caps
- trend requirement `price > ema20 and ema9 >= ema20`

### Probe Priority

Probe should be treated as a separate research track:

- either retarget Probe to true shelf names and mid-cap continuation setups
- or keep it disabled for the current small-cap runner universe

## Recommendation Summary

- `macd_30s`: keep as the confirmed entry bot
- `macd_30s_reclaim`: best next path for small-cap runners
- `macd_30s_probe`: keep available, but deprioritize for the current names

- the latest Reclaim recommendations improve the shape of the runner model
- they do not fully solve live-readiness by themselves
- the next development step should be targeted tuning of the new runner filters before loosening candle/location rules broadly
