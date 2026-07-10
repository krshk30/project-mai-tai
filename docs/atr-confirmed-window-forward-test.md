# ATR confirmed-window forward test — pre-committed stopping rule

**Written 2026-07-09, before any live data is collected.** This document is committed *before*
the flag flips so the stopping rule cannot be rationalized after the fact.

## What the live test measures — and what it does not

This is **not** a test of "does the confirmed-window ATR rule work." The one-day proof
(2026-07-09, +$10.03) is not evidence: it had **zero flip exits**, three trades round-tripped in
under nine seconds at **zero modeled fill latency**, and it changed **three variables at once**
(universe → scanner-confirmed only; window → confirm-time floor; exit → wait-3 entry + bar-close
flip). Nine trades and no flip exits is not an answer.

The single question this forward test answers:

> **Do flip exits fire on scanner-confirmed names, and what do they cost?**

Everything else (target win rate, dollar P&L) is secondary and easy to be fooled by.

## Evaluation method (fixed in advance)

- **Window:** 30 name-days minimum before any conclusion.
- **Unit:** percentages, **not** dollars (dollars are dominated by the $10–13 names — SDOT/UPC/VRAX
  — and have misled us twice).
- **Statistic:** **median** trade, not total.
- **Robustness:** **drop-one** applied *before* any "it's just one bad name" conclusion.

## Stopping rule — stop if ANY of these is true after 30 name-days

1. **Median trade is negative.**
2. **Flip exits fire and average worse than −5%.**
3. **Win rate is below the payoff-implied breakeven** over the same window.

## Rationale

**Why −5%, not −3% (criterion 2).** The 153-trade intrabar sample had flip exits averaging
**−4.13%**, and the big winners routinely drew down **3–8%** before running. A −3% threshold would
flag ordinary noise as failure and stop a working strategy. −5% is the line past which flips are
doing real damage, not breathing.

**Why criterion 3 is the one that matters.** It is the only criterion that catches a *comfortable*
loss. The 2026-07-09 day showed **89% win at +2% targets** — which looks excellent — but the target
config carries roughly a **0.49× payoff**, so **breakeven win rate is ~67%**. If flip exits start
showing up at −5% and the win rate settles at, say, **60%**, we lose money **while feeling fine**
(high win rate, small green targets, occasional large red flips). Criterion 3 forces the
payoff-adjusted view so a high win rate can't hide a negative expectancy.

## Preconditions (must be true before the flag flips)

- Scanner-confirmed-event capture is **live and writing to a durable table** (so the 30 name-days
  are actually reconstructable — the reason ATR taught us nothing for a month is that nothing was
  recorded).
- Deploy is attended, in a quiet window, with rollback ready. Confirmed names only, confirm-time
  entry floor, qty 10, Schwab.
