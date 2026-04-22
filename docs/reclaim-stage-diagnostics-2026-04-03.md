# Reclaim Stage Diagnostics - 2026-04-03

## Scope

This pass replayed the current reclaim research baseline across the full recovered provider-backed universe:

- `AGPU`, `BFRG`, `CYCN`, `ELAB`, `RENX`, `SST` on `2026-04-01`
- `BDRX`, `BFRG`, `COCP`, `PFSA`, `SKYQ`, `TMDE`, `TURB` on `2026-04-02`

Baseline used:

- `ticker_loss_pause_streak_limit = 0`
- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

Output:

- `tmp_replay/reclaim_stage_diagnostics.json`

Script:

- `scripts/reclaim_stage_diagnostics.py`

## Main Results

### Outcome Counts

- reclaim starters with classified forward outcomes: `2`
- `taken_good`: `2`
- `taken_bad`: `0`

### Starter Lifecycle Counts

- `starter_no_confirm`: `3`
- `starter_fail_fast`: `1`
- `starter_with_add`: `0`

This is the most important new result from the stage diagnostics.

The current reclaim research baseline is not mainly failing because it enters too many bad starters.

It is failing because:

1. many promising setups are still blocked before entry
2. the starters that do fire are not surviving long enough to confirm/add

### Top Blockers On Bars That Still Would Have Worked

Top `should_enter` blocked reasons:

- `pretrigger reclaim pullback not ready`: `1455`
- `pretrigger reclaim pullback volume not ready`: `1141`
- `pretrigger reclaim location not ready`: `504`
- `pretrigger reclaim candle not ready`: `311`

This confirms the earlier direction:

- pullback readiness is still the main structural bottleneck
- pullback-volume protection is also a major suppressor
- candle matters much less than pullback and volume, but it still blocks some winners

## What The Good Starters Looked Like

Good reclaim starters in this pass:

- `AGPU` on `2026-04-01 14:52:30 ET`
- `SKYQ` on `2026-04-02 14:11:30 ET`

Shared traits:

- same-bar support touch was present
- current relative volume was low, not high
- pullback-volume ratio was low
- both were slightly below EMA9 at entry
- neither had a confirmed add

Average metrics for `taken_good`:

- pullback from high: `5.68%`
- retrace fraction of impulse leg: `1.59`
- pullback-volume ratio: `0.44`
- current relative volume: `0.41`
- EMA9 extension: `-1.02%`
- VWAP extension: `-3.84%`
- body: `0.63`
- close position: `0.67`
- upper wick: `0.08`

## What The Blocked Winners Looked Like

Average metrics for `blocked_should_enter`:

- pullback from high: `2.45%`
- retrace fraction of impulse leg: `2.52`
- pullback-volume ratio: `1.56`
- current relative volume: `1.19`
- EMA9 extension: `-0.14%`
- VWAP extension: `+3.88%`
- body: `0.49`
- close position: `0.41`
- upper wick: `0.23`

Interpretation:

- blocked winners are noisier than the actual good starters
- many of them happen with high current volume and poor pullback-volume absorption
- many are also more extended above VWAP and have worse candle quality

So the data does **not** support broadly loosening everything.

It supports a narrower change:

- keep the calmer pullback / absorption bias
- improve pullback timing and starter/confirmation flow

## Most Important New Insight

The stage diagnostics shift the next priority.

Before this pass, the main question looked like:

- how do we get reclaim to enter more often?

After this pass, the better question is:

- how do we stop good reclaim starters from dying as `no_confirm` or `fail_fast` before they can become full trades?

That means the next reclaim research should be split into two separate problems:

1. starter gating
2. post-starter management

## What To Do Next

### Keep

- keep `higher_low` and `held_move` out of hard gates
- keep replay research on `ticker_loss_pause_streak_limit = 0`
- keep same-bar touch allowed
- keep pullback-volume protection on for now

### Stop

- stop spending time on broad candle loosening
- stop spending time on looser EMA9 and VWAP caps for now

### Next Research Target

Run the next sweep on reclaim starter management:

- `pretrigger_failed_break_lookahead_bars`
- fail-fast conditions:
  - price below hold floor
  - MACD below signal
  - price below EMA9
- confirm/add rules:
  - MACD cross
  - histogram surge
  - VWAP breakout with volume

The reason is simple:

- all current reclaim starters still die as `no_confirm` or `fail_fast`
- none become confirmed/add trades
- this is now the clearest remaining bottleneck after the replay-alignment fix

## Bottom Line

The outside recommendation was directionally useful.

It was right that:

- reclaim is the path worth focusing on
- higher-low and held-move should stay out of hard gating
- replay pause should stay separated from research
- pullback shape is still the main structural blocker

But after correcting replay alignment and running stage diagnostics across the full universe, the next move is not a full reclaim rewrite yet.

The data now says:

- starter quality can work
- starter management is the next weak link
- pullback readiness still needs refinement, but confirm and fail-fast logic now deserve equal attention
