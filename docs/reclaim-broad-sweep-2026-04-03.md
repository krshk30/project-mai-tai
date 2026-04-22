# Reclaim Broad Sweep - 2026-04-03

## Scope

This sweep used the widest recoverable 30s universe available from local history plus provider pulls.

Recovered symbols and days:

- `2026-04-01`: `AGPU`, `BFRG`, `CYCN`, `ELAB`, `RENX`, `SST`
- `2026-04-02`: `BDRX`, `BFRG`, `COCP`, `PFSA`, `SKYQ`, `TMDE`, `TURB`

Provider-backed replay sources:

- `tmp_replay/massive_apr01_sample.sqlite`
- `tmp_replay/massive_apr01_renx.sqlite`
- `tmp_replay/massive_apr02_sample.sqlite`

April 3 provider fetches returned `0` bars for the tested names, so they were excluded from the broad-sweep conclusion.

## Profiles Tested

### Strict Runner

Reclaim with all three runner filters enabled:

- higher low
- held move
- pullback volume absorption

### Middle Ground

Reclaim with:

- higher low OFF
- held move OFF
- pullback volume absorption ON

### All Structure Off

Reclaim with:

- higher low OFF
- held move OFF
- pullback volume absorption OFF

## Results

### Strict Runner

- total intents: `0`
- good: `0`
- bad: `0`
- symbols with intents: `0`

Top blockers:

- `pretrigger reclaim pullback not ready`
- `pretrigger reclaim higher low not ready`

Conclusion:

- too strict for the current broad runner universe

### Middle Ground

- total intents: `4`
- good: `0`
- bad: `1`
- symbols with intents: `2`

Symbols with intents:

- `CYCN` on `2026-04-01`: `2` intents
- `SKYQ` on `2026-04-02`: `2` intents, `1` bad

Top blockers:

- `pretrigger reclaim pullback not ready`
- `pretrigger reclaim pullback volume not ready`
- `pretrigger reclaim location not ready`
- `pretrigger reclaim candle not ready`

Conclusion:

- this is the best balanced profile tested so far
- it creates some opportunity without turning every structural gate off
- it is still not good enough for live promotion

### All Structure Off

- total intents: `4`
- good: `0`
- bad: `1`
- symbols with intents: `2`

Symbols with intents:

- `CYCN` on `2026-04-01`: `2` intents
- `SKYQ` on `2026-04-02`: `2` intents, `1` bad

Top blockers after removing all three structure filters:

- `pretrigger reclaim pullback not ready`
- `pretrigger reclaim location not ready`
- `pretrigger reclaim candle not ready`

Conclusion:

- turning off pullback-volume protection did not improve opportunity versus the middle-ground profile
- volume absorption is not the main bottleneck

## Main Findings

1. The original strict runner reclaim model is too tight across the broad recoverable universe.
2. The main bottleneck is still pullback formation itself, not just one extra filter.
3. Higher-low and held-move are the two structure filters that most reduce opportunity.
4. Pullback-volume absorption should stay ON for now because turning it OFF did not improve results.
5. Even after widening the universe well beyond `CYCN`, `RENX`, `SKYQ`, and `TMDE`, reclaim still did not show broad robust edge.

## Best Current Direction

Use the middle-ground reclaim profile as the next research baseline:

- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.04`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

## Next Step

Do not keep widening structure filters blindly.

Instead, tune the next bottlenecks under the middle-ground profile:

- reclaim location logic
- reclaim candle quality logic
- interaction with ticker loss pause during replay

Relevant sweep outputs:

- `tmp_replay/reclaim_structure_sweep_stage1.json`
- `tmp_replay/reclaim_filter_ablation.json`
- `tmp_replay/reclaim_filter_middle_ground.json`
- `tmp_replay/reclaim_broad_universe_profiles.json`

## Follow-Up Findings

### Location

Location sweeps showed:

- loosening `EMA9` extension from `4%` to `5%` increased intents
- further widening beyond `5%` did not materially improve outcomes
- `VWAP` extension changes had very little effect

Best practical location baseline:

- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`

Relevant output:

- `tmp_replay/reclaim_location_sweep.json`

### Candle

Candle rules were tested one group at a time under the best current structure/location baseline:

- `min_close_pos_pct`
- `min_body_pct`
- `max_upper_wick_pct`

Result:

- none of these materially changed intents or outcomes across the broad universe
- candle is not the real bottleneck for the current reclaim profile

Relevant outputs:

- `tmp_replay/reclaim_candle_closepos_sweep.json`
- `tmp_replay/reclaim_candle_body_sweep.json`
- `tmp_replay/reclaim_candle_wick_sweep.json`

### Loss-Pause Interaction

Replay loss-pause did matter.

With the middle-ground reclaim baseline:

- normal replay pause:
  - intents: `7`
  - good: `0`
  - bad: `1`
  - active symbols: `CYCN`, `SKYQ`

- replay with ticker loss-pause disabled:
  - intents: `9`
  - good: `1`
  - bad: `1`
  - active symbols: `AGPU`, `CYCN`, `SKYQ`

This surfaced one additional good reclaim on `AGPU`.

Relevant output:

- `tmp_replay/reclaim_pause_interaction.json`

### Final Mixed Check

I also compared:

- middle-ground no-pause baseline
- slightly looser location plus looser pullback-volume ratio

Result:

- both profiles produced the same practical outcome:
  - intents: `9`
  - good: `1`
  - bad: `1`
  - active symbols: `AGPU`, `CYCN`, `SKYQ`

Conclusion:

- the simpler no-pause middle-ground baseline is enough
- extra loosening of location and pullback-volume did not add value

## Best Current Reclaim Research Baseline

- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

For replay research only:

- `ticker_loss_pause_streak_limit = 0`

## Updated Direction

The reclaim path is no longer blocked by candle tuning.

The remaining core blocker is pullback definition itself:

- too many names still fail `pullback not ready`
- after that, volume absorption and location remain the next meaningful filters

The next useful research step is not more candle work.

It is:

1. refine the pullback model itself
2. decide whether replay should continue with no ticker-loss pause while tuning entry quality
3. compare the `AGPU` good reclaim versus the `SKYQ` bad reclaim bar-by-bar to see what truly separates the two

## Replay Alignment Correction

After the initial sweep, I found that the review helper was classifying buy intents against the previous bar by default even when the replay runtime emitted the intent on the exact completed 30s bar.

That shifted some reclaim case studies backward by one bar and understated the current reclaim profile.

I fixed the review helper in `scripts/render_live_day_review.py` to prefer an exact timestamp match before falling back to the older previous-bar behavior.

Relevant outputs after the fix:

- `tmp_replay/reclaim_broad_universe_profiles_corrected.json`
- `tmp_replay/reclaim_case_studies_agpu_skyq_corrected.json`

## Corrected Broad Results

### Strict Runner

- total intents: `3`
- good: `0`
- bad: `0`
- symbols with intents: `1`

Symbols with intents:

- `SKYQ` on `2026-04-02`: `3` intents

Conclusion:

- the strict runner profile is still tight, but it is not completely dead

### Middle Ground

- total intents: `7`
- good: `1`
- bad: `0`
- symbols with intents: `2`

Symbols with intents:

- `CYCN` on `2026-04-01`: `2` intents
- `SKYQ` on `2026-04-02`: `5` intents, `1` good

Conclusion:

- reclaim looks healthier than the pre-fix sweep suggested
- the earlier `SKYQ` bad conclusion was caused by replay bar misalignment

### Middle Ground No Pause

- total intents: `9`
- good: `2`
- bad: `0`
- symbols with intents: `3`

Symbols with intents:

- `AGPU` on `2026-04-01`: `2` intents, `1` good
- `CYCN` on `2026-04-01`: `2` intents
- `SKYQ` on `2026-04-02`: `5` intents, `1` good

Conclusion:

- this is now the best reclaim research baseline
- replay loss-pause still suppresses some opportunity during tuning

### All Structure Off No Pause

- total intents: `9`
- good: `2`
- bad: `0`
- symbols with intents: `3`

Conclusion:

- removing pullback-volume protection still does not improve opportunity versus the simpler middle-ground no-pause profile
- keep pullback-volume protection ON for now

## Updated Best Current Reclaim Research Baseline

- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

For replay research only:

- `ticker_loss_pause_streak_limit = 0`

## Revised Takeaway

The reclaim path is still not ready for live promotion.

But after fixing replay alignment, it is clearly better than the earlier analysis suggested:

- strict runner is not completely dead
- middle-ground reclaim can produce good outcomes on the broad universe
- no-pause replay shows `2` good and `0` bad outcomes on the current recovered sample

That means the next work should stay data-driven, but it should no longer start from the assumption that reclaim is fundamentally broken.
