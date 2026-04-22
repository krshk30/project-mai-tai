# Reclaim Gate Bidirectional Sweep (2026-04-04)

This note compares reclaim filter behavior in two directions across the recovered replay universe:

- `disable one gate at a time` from the current reclaim research baseline
- `add one gate at a time` from an almost-open reclaim baseline

Universe:

- 2026-04-01: `AGPU`, `BFRG`, `CYCN`, `ELAB`, `RENX`, `SST`
- 2026-04-02: `BDRX`, `BFRG`, `COCP`, `PFSA`, `SKYQ`, `TMDE`, `TURB`

Research baseline:

- `ticker_loss_pause_streak_limit = 0`
- `pretrigger_reclaim_touch_lookback_bars = 8`
- `pretrigger_reclaim_min_pullback_from_high_pct = 0.015`
- `pretrigger_reclaim_max_pullback_from_high_pct = 0.15`
- `pretrigger_reclaim_max_extension_above_ema9_pct = 0.05`
- `pretrigger_reclaim_max_extension_above_vwap_pct = 0.05`
- `pretrigger_reclaim_require_higher_low = false`
- `pretrigger_reclaim_require_held_move = false`
- `pretrigger_reclaim_pullback_volume_max_spike_ratio = 0.60`

## Disable-One Results

Current reclaim baseline reference:

- `baseline`: `4` open intents, `2` good, `0` bad, `0` open, `2` unresolved

Knockout results:

| Profile | Open | Good | Bad | Open Trades | Unresolved | Read |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `pullback_off` | 28 | 7 | 7 | 3 | 11 | Biggest active blocker in the current baseline |
| `candle_off` | 24 | 2 | 11 | 1 | 10 | Also a major active blocker, but very noisy |
| `volume_off` | 5 | 3 | 0 | 0 | 2 | Small incremental unlock, cleaner than candle |
| `location_off` | 5 | 2 | 1 | 0 | 2 | Small incremental unlock |
| `touch_off` | 4 | 2 | 0 | 0 | 2 | No practical change |
| `pullback_volume_off` | 4 | 2 | 0 | 0 | 2 | No practical change at current strictness |
| `stoch_off` | 4 | 2 | 0 | 0 | 2 | No practical change |
| `trend_off` | 4 | 2 | 0 | 0 | 2 | No practical change |
| `momentum_off` | 4 | 2 | 0 | 0 | 2 | No practical change |
| `score_off` | 4 | 2 | 0 | 0 | 2 | No practical change |

Takeaway:

- In the current reclaim baseline, `pullback` is the clearest live blocker.
- `candle` is the second major blocker, but it looks dangerous to simply remove because bad trades jump hard.
- `volume` and `location` matter a little, but they are not the first bottleneck in the current stack.
- `touch`, `stoch`, `trend`, `momentum`, and `score` do not materially change the current strict baseline when toggled alone.

## Add-One-Back Results

Open reclaim baseline reference:

- `open_baseline`: `1574` open intents, `333` good, `682` bad, `214` open, `345` unresolved

Additive results:

| Profile | Open | Good | Bad | Open Trades | Unresolved | Delta vs Open | Read |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `location_on` | 572 | 132 | 200 | 50 | 190 | -1002 | Strongest stabilizer |
| `momentum_on` | 744 | 172 | 299 | 115 | 158 | -830 | Very strong stabilizer |
| `trend_on` | 816 | 185 | 359 | 103 | 169 | -758 | Very strong stabilizer |
| `pullback_on` | 1008 | 197 | 420 | 106 | 285 | -566 | Strong structural reducer |
| `volume_on` | 1019 | 233 | 418 | 151 | 217 | -555 | Strong reducer |
| `candle_on` | 1077 | 244 | 449 | 148 | 236 | -497 | Strong reducer |
| `pullback_volume_on` | 1111 | 236 | 485 | 145 | 245 | -463 | Meaningful reducer once setup is open |
| `score_on` | 1222 | 278 | 514 | 168 | 262 | -352 | Moderate reducer |
| `stoch_on` | 1519 | 328 | 646 | 212 | 333 | -55 | Mild reducer |
| `touch_on` | 1574 | 333 | 682 | 214 | 345 | 0 | No measurable effect |

Takeaway:

- Once reclaim is opened up, `location`, `momentum`, and `trend` become the heavy stabilizers.
- `pullback`, `volume`, `candle`, and `pullback absorption` all materially reduce raw trade flow too.
- `touch` still does nothing measurable, even in the open baseline.
- `stoch` has only a small effect compared with the main gates.

## What This Means

There is not one single culprit. The reclaim stack is layered:

- In the current strict baseline, `pullback` and then `candle` are the gates actively preventing entries.
- In a loose/open reclaim, `location`, `momentum`, and `trend` become the next major guard rails preventing a flood of weak entries.

So the right read is:

- `pullback` is the first redesign target.
- `candle` should probably be softened or converted into a weaker veto, not removed blindly.
- `location`, `momentum`, and `trend` should not be stripped out casually. They are doing real cleanup work once pullback pressure is reduced.
- `touch` looks like the weakest gate and is a good candidate to redesign into a softer timing aid instead of a hard requirement.

## Recommended Next Steps

1. Redesign `pullback` as a staged reclaim model instead of a single hard readiness gate.
2. Soften `candle` into a penalty or lower-priority veto rather than a hard block.
3. Keep `location`, `momentum`, and `trend` as important safety rails for the next reclaim version.
4. Demote `touch` from hard gate to timing/context signal unless a later test proves otherwise.
5. After the pullback redesign, rerun the same two-way sweep again before changing live defaults.
