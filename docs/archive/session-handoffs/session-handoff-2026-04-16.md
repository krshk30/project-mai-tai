# Session Handoff - 2026-04-16

## What Changed

This handoff captures the reclaim work completed after the original 2026-04-13 note, including:

- after-hours reclaim runtime changes that were deployed
- live-day reclaim analysis from 2026-04-14
- improved reclaim reporting and what-if tooling
- large-cap-style replay comparisons on `AVNS`, `MGRT`, and `MRNA`
- current recommendation on whether reclaim is still the right main direction

## Deployed Reclaim Runtime Changes

The following reclaim changes were deployed after market hours:

1. Narrower ticker pause
   - reclaim no longer uses the old blunt ticker-loss pause
   - only `cold losses` count toward pause
   - a `cold loss` means the trade never reached `+1%` peak profit first

2. Confirm-add safeguard
   - `R1_BREAK_CONFIRM` add now requires the starter to have already shown at least `+1%` peak profit
   - goal: stop weak reclaim attempts from growing into bigger giveback losses

3. Earlier profit protection
   - reclaim now locks a little more profit earlier:
     - after `+1%` peak: lock `+0.25%`
     - after `+2%` peak: lock `+0.75%`

4. True close-reason preservation
   - filled close records now preserve the actual strategy close reason instead of collapsing to generic `OMS_FILL`
   - this makes day review much more trustworthy

## Reporting / Analysis Tooling Added

The reclaim analysis tooling was expanded so we can inspect one day cleanly instead of inferring from dashboard fragments.

Main scripts:

- [reclaim_live_day_report.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_live_day_report.py)
- [reclaim_live_day_whatif.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_live_day_whatif.py)
- [reclaim_live_day_replay.py](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/scripts/reclaim_live_day_replay.py)

Main local outputs:

- [reclaim_live_day_report_2026-04-14.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_live_day_report_2026-04-14.md)
- [reclaim_live_day_report_2026-04-14.json](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_live_day_report_2026-04-14.json)
- [reclaim_live_day_whatif_2026-04-14.json](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_live_day_whatif_2026-04-14.json)
- [reclaim_live_day_replay_2026-04-14.json](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/reclaim_live_day_replay_2026-04-14.json)

## April 14 Live Reclaim Read

The real reclaim bot behavior on 2026-04-14 was:

- `23` closed trades
- `4` wins
- `15` losses
- `4` flats

Important interpretation:

- `7` losers had already reached at least `+1%` unrealized first
- `7` were true `cold losses`

So the problem was not only entry quality. It was both:

- some weak entries still getting through
- some decent entries giving back too much before exit

Main live blocked clusters:

- `below_vwap`
- `pullback_too_shallow`
- `ticker_pause`
- `below_vwap_and_ema9`
- `below_ema9`

## Key What-If Findings

The strongest useful simulation findings from the 2026-04-14 live-day what-if pass were:

1. `pause_off` looked much better than the old blunt pause
   - but turning pause off completely is still too broad for live use

2. `ticker_pause` was overblocking real later opportunities
   - this is why the narrower `cold-loss-only` pause was promoted

3. `below_vwap_and_ema9` was the most interesting missed later-pullback cluster
   - that blocked group still showed strong follow-through rates in the simulation
   - but no safe location redesign was fully validated yet

4. `soft_location` style loosening was not good enough
   - too noisy
   - not a safe promotion candidate

## Critical Replay Warning

After the after-hours tightening work, a full replay of the current reclaim rules on the 2026-04-14 live small-cap-style reclaim universe came back with:

- `0` good
- `0` bad
- `0` open
- `0` intents

Meaning:

- the post-market reclaim entry shape had become too tight for that live-day tape
- the safety/process fixes were still useful
- but the entry profile itself was no longer acceptable as a “tomorrow morning” live reclaim shape for that style of tape

This was the moment that forced the broader question:

- is reclaim still the right main project
- or is it becoming a niche path while `macd_30s` stays the stronger base bot

## Large-Cap-Style Replay Checks

To test whether reclaim might fit smoother, larger-cap-style tape better than the small-cap momentum names, three fresh single-name replays were run on 2026-04-14 data:

- `AVNS`
- `MGRT`
- `MRNA`

### AVNS

Direct P&L comparison:

- `macd_30s`: `3` trades, `0W / 3L / 0F`, `-$2.00`
- `macd_30s_reclaim`: `30` trades, `9W / 7L / 14F`, `-$0.625`

Interpretation:

- reclaim was more active
- reclaim lost less than core here
- but it still looked churny and not clearly attractive

### MGRT

Direct P&L comparison:

- `macd_30s`: `0` trades
- `macd_30s_reclaim`: `0` trades

Interpretation:

- neither bot found an edge on this tape

### MRNA

Direct P&L comparison:

- `macd_30s`: `12` trades, `4W / 5L / 3F`, `+$84.84`
- `macd_30s_reclaim`: `7` trades, `2W / 5L / 0F`, `-$0.605`

Interpretation:

- this is the strongest direct counterexample to reclaim
- on a larger-cap smoother name, `macd_30s` clearly outperformed reclaim

## Current Read

The large-cap-style checks do **not** support the claim that reclaim is simply “better for large caps.”

Instead they suggest:

- `AVNS`: reclaim was less bad than core, but still churny
- `MGRT`: neither bot did anything
- `MRNA`: core clearly beat reclaim

So the current evidence is:

- reclaim is **not** proving itself as the stronger general-purpose 30s bot
- reclaim may still have niche value
- but `macd_30s` currently looks like the stronger candidate overall when compared directly on P&L

## Recommendation

Current recommendation is to **de-prioritize reclaim as the main development path** until it can beat `macd_30s` in direct tape-matched comparisons.

Practical interpretation:

1. Keep the deployed runtime/process/reporting fixes
   - narrower cold-loss-only pause
   - safer confirm-add behavior
   - tighter early profit protection
   - real exit reason preservation

2. Stop assuming reclaim deserves more strategy work by default
   - it has not yet justified the extra complexity with better direct P&L

3. Treat reclaim as a niche / research path for now
   - not the default “best next bot”

4. Use direct side-by-side comparison against `macd_30s` as the decision rule
   - same symbol
   - same date
   - same replay framework
   - compare trade count, win/loss/flat, and net P&L

5. If reclaim work continues, it should be only for a clearly defined niche tape shape
   - not as a broad replacement for `macd_30s`

## Best Next Step

The cleanest next move is:

1. run a few more direct `macd_30s` vs `macd_30s_reclaim` comparisons on tape styles we actually care about
2. decide whether reclaim is:
   - worth keeping as a niche bot
   - worth pausing
   - or worth tape-splitting from core

If a decision has to be made immediately based on current evidence, the most honest answer is:

- `macd_30s` currently looks like the stronger main path
- reclaim has not yet earned more primary strategy time
