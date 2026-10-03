# Schwab-tick pre-market ATR seed: read-only impact protocol

Frozen before reading study outcomes on 2026-09-29. BKYI 2026-09-29 and MEDS
2026-09-16 are known motivating cases, not holdout validations. This study does
not change a trading service, flag, subscription, order, or broker account.

## Question and population

- Would genuine Schwab LEVELONE trade updates before the first Schwab CHART bar
  produce a more chart-aligned ATR state and change executable v2 entries?
- Include every stock-day watched by v2 in every completed ET session with
  retained `market_trade_ticks` and `strategy_bar_history` data, ending
  2026-09-29. Enumerate the earliest retained tick session, exact dates, and
  watchlist stock-days *before* outcome joins. Do not select winners or names
  based on their chart, tick density, or later return.
- Reconstruct watch intervals from the durable scanner/watchlist evidence.
  A stock-day with an ambiguous watch interval remains in the denominator but
  is UNKNOWN for executable-entry grading. Report the number of stock-days at
  each stage and the separately known BKYI/MEDS sentinels.
- Split results at each independently verified live activation that changes
  entry or exit behavior. Unknown activation timing is UNKNOWN, not retroactive
  application of current code.

## Inputs and three paths

1. LIVE: the actual v2 bar/probe, rest/intent, broker order, fill, and managed
   exit sequence, with Schwab and Webull accounts separate. An intent is not a
   fill, and a chart flip is not an order.
2. TICK-SEEDED: 1-minute OHLC from `market_trade_ticks` where provider is
   `schwab`, service is `LEVELONE_EQUITIES`, and the raw record carries its own
   positive trade-time field `35`. Ignore recycled last prices without `35`,
   quote-only records, duplicate raw events, and events received too late to
   have been available at the decision time. Main seed starts at 06:00 ET;
   04:00 ET is a separately reported sensitivity. Use printed minutes only:
   never fabricate an empty minute. End strictly before the first CHART bar;
   Schwab CHART owns a shared minute. Feed the seed through the existing
   state-only ATR path, then replay subsequent Schwab bars with the same
   session-anchor and first-live-bar reset/restore semantics as PR #994. Seed
   bars cannot arm, rest, claim a slot, or emit an intent.
3. CHART PROXY: Massive 04:00 ET bars are used only to compare ATR state, trail,
   and BUY/SELL flip timing. They are not fed to the trading replay and are not
   called a verified chart oracle. Report proxy/Schwab overlapping OHLC
   disagreement instead of treating a proxy mismatch as a bot error.

All paths use the same modified true range, Wilder period 5, factor 3.5, and
SMA5 initialization. Compare the tick-built OHLC with Massive OHLC on every
shared pre-07:00 minute. Report close/high/low agreement within 0.1%, missing
minutes, tick counts, trade-time vs receive-time lag, first tick time, longest
printed gap, and coverage after v2 subscription. A stock-day with fewer than
10 tick-built bars, no genuine field-35 prints, or ambiguous timing is
INSUFFICIENT/UNKNOWN for the tick-seed decision, never a silent negative.

## Replay and outcome grading

- Before counterfactual scoring, run the unseeded replay as a control against
  LIVE. Report differences in rests, intents, fill times/prices, and exits.
  Counterfactual outcomes are UNKNOWN when the unseeded control cannot
  reproduce the relevant live decision or the required quote/tape is absent.
- Reuse the production strategy's short-segment, three-short-bar rest,
  flip-owned first-slot, entry window, 0.5% resting band, and live-period
  configuration. Feed historical quotes only when they would have been known.
  A counterfactual order without qualifying quote/tape evidence is unfilled or
  UNKNOWN, not a simulated fill by assertion.
- The existing replay engine models the Schwab leg but explicitly excludes
  Webull fan-out. Report actual Webull fills and exits separately. A Webull
  *counterfactual* is UNKNOWN unless an independently tested Webull order and
  quote model is added before outcome reads. Never pool its modeled result
  with Schwab or present it as an actual fill.
- Use the historical live exit configuration for each activation segment,
  including native bracket, CW floor/trail, confirmation exit, and session
  handover where evidenced. Missing exit evidence is UNKNOWN. Distinguish
  realized LIVE outcomes from replay-modeled outcomes throughout.
- Compare BUY/SELL flips by stock-day and exact bar minute; also report a
  within-one-bar alignment separately. Report added/removed/shifted entry
  *opportunities*, resting orders, accepted orders, modeled fills, and actual
  fills as separate counts. An existing live trade whose decision changes is
  named individually. A new signal cannot be called a new trade without a
  modeled, evidence-backed fill.
- For graded modeled fills, report winners/denominator, median return, sum of
  one-share percentage returns, drop-one-by-symbol results, session and entry
  hour tables, and exit-reason counts. Never label this actual P&L. Report
  false/proxy-disagreeing flips and provider-fidelity failures separately.
  Better means more correctly aligned, evidence-backed filled opportunities
  without degrading existing live decisions; worse includes lost live fills,
  false new fills, poor provider fidelity, or an unbounded UNKNOWN population.

## Operational and decision boundary

- Commit this protocol before opening any study outcome dataset. Queries must
  be read-only, date/symbol bounded, and run at `nice -n 19`; move full-history
  scans, Massive pulls, and replay after 16:00 ET. Do not overlap other heavy
  studies or production restarts. Retain source paths, exact query bounds,
  replay code SHA, per-stage denominators, and a reproducible result artifact.
- A single-symbol read-only TIMESALE_EQUITY availability probe may be done in
  a later pre-market session only if it does not change a production service or
  subscription; otherwise report NOT TESTED rather than silently enabling it.
- The result informs an operator decision only. Do not build or deploy the
  proposed live Schwab-tick seed, enable Massive, or change any flag from this
  study. A chart match on BKYI alone is not an enable gate for all stocks.

## Amendment 1: coverage-census timing

On 2026-09-29 at about 09:01 ET, before the all-stock-day coverage read,
the operator explicitly authorized running that coverage census during market
hours. Only the bounded, read-only coverage query is moved earlier. It runs at
nice 19 with a statement timeout. Full outcome joins, provider comparisons,
and fill-aware replay remain subject to the original protocol's load caution.
