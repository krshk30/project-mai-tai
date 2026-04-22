# Reclaim Canonical Scoreboard (2026-04-09)

This note resolves the earlier counting mismatch between the ad hoc mix sweep and the stage diagnostic, and now treats the combined replay universe as the default check.

It also reflects the latest research-harness alignment:

- replay now inherits the in-code reclaim baseline directly
- replay disables ticker-loss pause and daily-loss throttles by default for research only
- combined-universe numbers below should be read as setup-quality research, not live risk-managed outcomes

## What Was Wrong

An earlier `reclaim_mix_sweep.json` snapshot had stale partial results from a long-running sweep. That is why one report showed a lower `good/bad/open` line while the later stage diagnostic showed a stronger line.

## Canonical Source

Use [reclaim_canonical_scoreboard.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_canonical_scoreboard.py) and its output file:

- [reclaim_canonical_scoreboard.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_canonical_scoreboard.json)

This scoreboard uses one consistent method:

- count only filled `open` buy intents
- match those entries against classified replay outcomes
- count unmatched entries as `unresolved`
- also report starter lifecycle counts

## Current Truth

The replay tools now support three universes:

- `baseline`: original April 1-2 recovered sample
- `apr08_top5`: fresh April 8, 2026 top gainers
- `combined`: old sample plus April 8 top gainers

`combined` is now the default validation universe.

The replay harness now keeps only two research-only overrides by default:

- `max_daily_loss = -1_000_000.0`
- `ticker_loss_pause_streak_limit = 0`

Everything else inherits the current reclaim research baseline directly from code.

For the current reclaim research baseline on the original `baseline` universe after three changes:

- reclaim-specific `R1_BREAK_CONFIRM` follow-through confirmation
- reclaim-specific soft fail-fast disabled by default (hold-floor still active)
- combined-universe pullback tuning: shallower min percent pullback plus wider leg-retrace cap

- `open_intents = 79`
- `taken_good = 24`
- `taken_bad = 12`
- `taken_open = 13`
- `unresolved = 30`
- `resolved_good_rate = 0.6667`

Starter lifecycle counts:

- `starter_no_confirm = 17`
- `starter_fail_fast = 13`
- `starter_closed = 21`
- `starter_with_add = 28`

This is the key result:

- replay edge improved from `27 / 22` to `24 / 12` on the original baseline universe
- `starter_fail_fast` was the real remaining reclaim choke point after confirm work
- reclaim no longer gets shaken out early by EMA9/MACD softness
- the best pullback tweak so far is allowing shallower percent pullbacks while widening the leg-retrace cap

## Comparison Check

`location_off` is a useful sanity check:

- `open_intents = 227`
- `taken_good = 55`
- `taken_bad = 84`
- `resolved_good_rate = 0.3957`

So location is still doing important cleanup work even though it is one of the top remaining blockers.

## Rejected Research Branch

I also tested a softer same-bar touch-recovery location rule for reclaim.

That branch was directionally wrong for the broad universe:

- it pushed the baseline close to `location_off`
- it expanded entries too aggressively
- it flipped the replay edge negative

So that softer location recovery logic stays implemented as an explicit research flag only, and it is `off` by default in the reclaim baseline.

## Takeaway

The current reclaim research baseline is stronger on the original baseline universe, but the combined universe is still the main truth check.

But it should still be read as:

- promising research result
- not live P&L
- not yet a promotable strategy

The next bottlenecks are still:

1. location modeling
2. pullback modeling
3. daily-loss / session gating interaction in replay

## Combined Universe Check

Once the fresh April 8 top gainers were added to the replay set, the reclaim baseline became harder to satisfy. With the corrected research harness, the current combined-universe baseline is:

- `open_intents = 184`
- `taken_good = 52`
- `taken_bad = 65`
- `taken_open = 21`
- `unresolved = 46`

## Location Tightening Update

The next reclaim cleanup pass focused on losers that were more extended above EMA9 and VWAP than the winners.

Tested combined-universe result with stricter reclaim location caps:

- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.04`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.04`

Compared with the previous combined no-pause reclaim baseline:

- before: `52 good / 65 bad`
- after: `50 good / 61 bad`

Why this version was kept:

- it reduced bad trades by `4`
- it only reduced good trades by `2`
- resolved good rate improved from `0.4444` to `0.4505`

So this became the first tighter reclaim research baseline. It keeps reclaim selective, but trims some of the more extended loser entries instead of broadening the setup.

## Focus-Universe Follow-Up

After excluding the worst reclaim-quality destroyers from the active tuning universe (`JEM`, `CYCN`, `BFRG`, `UCAR`, `BBGI`), the focus-universe reclaim baseline was:

- `32 good / 22 bad`

The next EMA-specific pass tested whether reclaim should be even less tolerant of entries stretched above EMA9.

Result on the focus universe:

- baseline (`EMA9 cap = 4%`, `VWAP cap = 4%`): `32 good / 22 bad`
- `EMA9 cap = 3%`: `31 good / 21 bad`
- `EMA9 cap = 2.5%`: `28 good / 19 bad`
- `EMA9 cap = 2.0%`: `26 good / 14 bad`
- `EMA9 cap = 1.5%`: `21 good / 10 bad`

VWAP tightening by itself did not change the result, and forcing the reclaim floor exactly at EMA9 also did not improve the scoreboard. So the useful next step was tighter EMA9 extension, not tighter VWAP.

That means the current reclaim research baseline is now:

- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.02`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.04`

Why `2%` was chosen:

- it materially improves quality without cutting reclaim activity as hard as `1.5%`
- it improves the focus-universe resolved good rate to `0.6500`
- it keeps reclaim in a practical middle ground between “better quality” and “still enough trades”
- `resolved_good_rate = 0.4444`

That is a better research picture for raw setup discovery, but it is still not robust enough for live promotion across fresher leaders.

## Location Follow-Up

Three reclaim-specific location research branches were tested on the combined universe:

- same-bar touch recovery
- single-anchor location (close above one anchor while staying near the other)
- stricter single-anchor recovery that also requires a stronger reclaim candle

All three branches increased activity without improving the combined edge, so they remain research-only ideas and stay `off` by default.

## Pullback Follow-Up

The first combined-universe pullback sweep did find one directionally useful improvement:

- lowering `pretrigger_reclaim_min_pullback_from_high_pct`
- widening `pretrigger_reclaim_max_retrace_fraction_of_leg`

That combination is now the reclaim research baseline in code because it improved:

- original baseline universe
- fresh April 8 universe
- combined universe

But even with that improvement, the combined universe is still negative overall, so pullback is not finished.
