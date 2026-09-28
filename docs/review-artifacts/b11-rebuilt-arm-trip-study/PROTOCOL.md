# B11 / PROV1x: rebuilt-arm entry study (preregistration)

This protocol is frozen before reading per-trip outcomes. It measures existing
trading; it does not authorize an entry-rule or configuration change. Code and
data extraction must not run heavily during 09:30-16:00 ET.

## Population and units

- Include every managed row closed from 2026-09-01 04:00 ET through the report's
  fixed extraction cutoff for `live:schwab_1m_v2` and `live:orb`. Report the
  cutoff and exclusions. An open or manually closed row is counted but has no
  fill-derived exit outcome unless an actual sell Fill can be attributed.
- A strategy arm and resting placement are logical opportunities. The Schwab
  primary and Webull fan-out are one opportunity but separate account legs.
  Never sum the legs as independent strategy opportunities or pool their returns.
- Count arms, first-slot resting placements, entry orders, entry fills, and
  completed fill-derived outcomes separately. A zero at any stage is reported,
  not silently removed from the denominator.

## Causal attribution, before outcomes

1. Parse v2 logs in file/write order, sorting only by timestamp (stable for
   same-millisecond lines). For each `[V2-CW-ARM]`, record symbol, emitted time,
   `bar_ts`, segment/arm identity when present, and the current replay/seed and
   watchlist-add context. A same-second seed marker preceding an ARM matters;
   alphabetical text order must never be used.
2. Join an entry intent to the arm by `cw_arm_bar_ts` and/or
   `fanout_segment_id` in the intent payload when these identify exactly one
   candidate in the symbol/session. Label this `direct`. Otherwise join a
   `[V2-RESTING-PLACE]` to the last preceding ARM in the same symbol/session,
   only when no intervening disarm or competing arm exists, then match the
   placement to the entry order and fill; label this `inferred`. If either join
   is ambiguous, missing, or crosses a watchlist removal/re-add without a new
   arm, label `UNKNOWN` and do not put it in a LIVE or REBUILT result bin.
3. Compute arm age as log emission time minus the arm bar's opening timestamp.
   `LIVE` requires age <= 120 seconds and no evidence that the arm was emitted
   inside replay/seed/warmup. `REBUILT` requires age > 300 seconds or positive
   causal replay/seed/warmup evidence for that ARM. Ages in (120, 300] seconds,
   missing timestamps, or contradictory evidence are `UNKNOWN`. A historical
   arm merely sharing a symbol-day with a later entry is not attribution.
4. Subclass REBUILT as pre-watch flip/seed-capped or same-session re-add using
   the actual watch-start, seed-cap, and watchlist-add ordering. If evidence is
   insufficient, report subclass `UNKNOWN` rather than infer it from the date.
   The WHLR 2026-09-25 sequence must classify the 15:24:25.962Z arm separately
   from the 15:25:03.177Z arm; GYGY re-adds are a churn control.

## Outcome and cuts

- Join buy and sell Fills through their broker orders to the same account and
  managed-row lifetime. Use quantity-weighted average buy and sell prices;
  return percent is `100 * (sell_vwap / buy_vwap - 1)`. No mark-to-market
  `current_profit_pct`, bar high, or hypothetical exit substitutes for a Fill.
  Report unpaired/partial/manual/unknown exits separately. A winner has a
  positive realized percentage; zero is not a winner. Fees are excluded and
  labeled as such unless a complete fee record is available for both legs.
- Split at the verified production activation timestamp of #993 and the
  verified v2 restart that activates #1049. Do not use merge time as deploy
  time. If #993's activation instant cannot be verified, exclude the boundary
  session from the split and name it. Post-#1049 is a distinct population even
  if it initially has zero completed trips. Never pool across either boundary.
- Within each deployment era, report LIVE, REBUILT, and UNKNOWN separately,
  then account, REBUILT subclass, hour bucket (ET), and the 2026-09-22 through
  2026-09-26 holdout separately. Every line names the window and the number of
  arms, placements, orders, fills, and rows with attributable exits.
- For rows with outcomes, report winners/denominator, median percent first,
  and sum of percentages second. For each name, recompute the median and sum
  after removing all of its trips; report the full range and the names at both
  extremes. This is sensitivity, not a strategy backtest or dollar P&L.

## Decision question and failure conditions

The report asks whether rebuilt-arm entries look acceptable, unacceptable,
or mixed by causal subclass; the operator, not this script, chooses among
those rules. No result is called a pass when attribution is UNKNOWN, exits are
missing, or a deployment-era denominator is too small to support it. A
negative/positive median with material drop-one sign reversal is explicitly
fragile. Missing bar/log/intent/fill evidence is a measured coverage gap, not
a zero or a losing trade. A trip-list CSV must include row and intent IDs,
account, symbol, arm and placement times, join method, class/subclass, era,
fill IDs, VWAPs, and any exclusion reason for independent review.
