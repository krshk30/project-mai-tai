# Independent calm-tape entry test: 2026-10-02

## Plain-language status

**DESIGN FROZEN; RESULTS NOT YET READ.** This is a read-only study, not a rule
change or an installation recommendation. The only tested line is **1.5%**.
Exits remain +5% / -8%. No threshold search, main merge or production changes.
Main stays `b8b0dafbdf583ca4af9eddc8dbf922cbf887a482` for tonight's install.

We will test whether smaller recent one-minute ranges distinguish better ATR
entries on Webull trades without a near-simultaneous Schwab twin, then separately
replicate the Schwab sample. Missing bars or outcomes will be UNMEASURED, never
counted as calm. The original 1.5% finding was selected after looking at outcomes
and 17 features; this test cannot make that original selection prospective.

## Frozen design, before extracting any outcomes

1. **Population.** Real `schwab_1m_v2` managed-position entries, backed by real
   BUY fills, on `live:orb` (Webull), 2026-08-03 through 2026-10-02 ET inclusive.
   The fixed extraction cutoff is 2026-10-02 14:40 ET. Exclude a Webull entry if
   a real same-symbol `live:schwab_1m_v2` ATR BUY starts within inclusive +/-120
   seconds. This is the declared untouched holdout; do not switch to another
   population if it is small or unfavorable. It is a different account subset,
   not a statistically independent market regime. No claim about unseen data
   beyond the reviewer's declaration is possible.
2. **Replication, separate.** All real Schwab ATR entries in the same date/cutoff
   window, with the same instruments and missing-data rules. Split descriptively
   at 2026-09-14 to match the claimed Aug/Sep-Oct halves. Never use replication
   to rescue a holdout result or adjust the line.
3. **Unit.** One entry/managed-position row, not shares and not individual partial
   executions. Verify the real entry against grouped BUY-order fills and use
   actual execution VWAP/time. Match each row to at most one nearest same-account,
   same-symbol ATR BUY order within 120 seconds of its entry time. Ambiguous,
   unmatched, overlapping or non-positive-price entries are UNMEASURED; report
   counts. The recorded managed-row entry price/time are retained for auditing.
4. **Feature.** Arithmetic mean of `100 * (high-low)/close` for the last TEN
   one-minute `strategy_bar_history` rows with `strategy_code=schwab_1m_v2`,
   `interval_secs=60`, same symbol/session (04:00 ET onward), and bar close
   strictly before actual entry time. The bar timestamp convention will be
   checked against the writer, not guessed. Require finite valid OHLC and close
   >0. Both live and REST provenance are reported; no Massive-bar substitution.
   Fewer than ten is UNMEASURED. Also require these ten bars to have been created
   by the entry and not revised after it; otherwise decision-time evidence is
   UNMEASURED. Report raw ten-bar coverage and stricter as-of coverage separately.
   No interpolation or carrying yesterday's bars into a thin morning.
5. **Fixed groups.** Calm = feature strictly <1.5%; other = >=1.5%. No alternative
   threshold is a result. Boundary equality belongs in other.
6. **Independent price-path construction.** Obtain raw one-second Massive
   aggregates independently through the existing authorized provider API (or
   an identifiable raw cache, never Claude's outcome labels), unadjusted to
   match real execution prices. Recompute the first +5% versus -8% touch from
   the actual entry until 19:55 ET that date. This is independent acquisition
   and scoring, **not an independent market-data vendor**. Preserve raw paths,
   request bounds/status/pagination and hashes. Empty/malformed/truncated
   responses are not proof of no hits. No bulk Redis snapshot read.
7. **Ordering and horizon.** A boundary touched inside the partial entry second
   is ambiguous unless independently ordered ticks resolve it. A later second
   touching both thresholds is also UNMEASURED, not stop-first or target-first.
   Otherwise the first touched boundary scores exactly +5 or -8 percentage
   points. With neither hit, use the last valid trade close at/before 19:55,
   no older than five minutes. A still-open horizon at extraction, missing
   endpoint or incomplete API pagination is UNMEASURED. Friday entries can be
   resolved before cutoff only if their first boundary was already observed.
   Uniform scores are gross hypothetical barrier returns, not executable P&L.
8. **Actual P&L.** Reconstruct FIFO real BUY/SELL fill cashflows per strategy,
   account and symbol, one flat-to-flat cycle. Associate only an unambiguous
   cycle to an entry; incomplete inventory, manual-close-only outcomes and
   unmatched fills stay UNMEASURED. Report gross realized return and dollars
   alongside the uniform outcome; fees/slippage not present in fills are not
   invented. Use identical measured subsets when contrasting groups; also
   give actual-P&L coverage separately.
9. **Uncertainty.** Every mean return/dollars per trade is shown +/- one ordinary
   sample standard error, sample SD/sqrt(n); n<2 => SE unavailable. Win-rate
   SE is sqrt(p*(1-p)/n). For a difference use sqrt(SE_calm^2+SE_other^2).
   Trades cluster by symbol/day, so these naive SEs may understate uncertainty;
   additionally report symbol-cluster bootstrap SE with 2,000 draws and seed
   20261002 where at least two symbols exist. No post-hoc significance gate.
10. **Costs.** Report measured entries skipped, their winning entries forgone
    (count/share of all winners), unknown-feature share separately, and changes
    in gross realized dollars on matched completed cycles. Hypothetical filter
    sums are not portfolio replay: skipped entries could change later state.
11. **Availability before placement.** The primary claim uses bars before fill;
    a resting order may have been placed much earlier. Separately check the
    original entry-intent/order creation time and whether its last ten bars
    existed then. Report classification changes and unavailable pre-placement
    features; do not sell a fill-time association as an implementable entry
    rule. Report pre-07:10 and new-watchlist-add coverage and inspect the actual
    seed/warm-up path. No inferred instantaneous access to later REST bars.

## Frozen holdout gate

Evaluate only entries with both a valid as-of feature and a resolved uniform
outcome. **PASS only if every item below holds:**

- At least **40 calm trades**, and a nonempty other group.
- Calm target-first win rate **>=60%**; other target-first win rate **<=55%**.
- Calm minus other mean uniform return **>=1.0 percentage point per trade**.
- Remove the symbol contributing the largest total uniform return to the calm
  group (tie: lexical symbol). The calm-minus-other mean return remains >0,
  with both groups still nonempty. Report which name and its contribution.
- Positive calm-minus-other mean-return gap in at least two of the three
  predeclared ET entry-time blocks: [07:00,09:30), [09:30,11:00), [11:00,16:00).
  A block with either group empty is UNMEASURED, never a favorable block.

**UNMEASURED** if sample size <40 calm trades or required evidence/robustness
comparisons cannot be evaluated. **FAIL** if adequate measurable evidence
violates any performance condition. If there is both an observed negative
criterion and inadequate coverage, report the negative criterion explicitly
but keep the overall test UNMEASURED, not a claimed validated rejection. This
is a research hypothesis verdict, not a production-service failure.

No cohort substitution, new threshold, outcome-based trimming, or switch to
the replication table is allowed after the frozen-design commit. Any necessary
instrument amendment is dated and disclosed without replacing the primary test.

## Collection safety

Production access is read-only SQL with transaction read-only, statement timeout,
one connection and small symbol/time-bounded extracts. Run with nice 19 and idle
I/O priority on the box. Compute locally. Raw price requests are sequential and
throttled, bounded per symbol/day; no service, flag, owner-hash or production
checkout changes. Do not query Redis snapshot-batches. Respect tonight's install.

## Results and candidates

Pending the frozen-design commit. Candidate ideas will be proposals only, at
most five, not newly discovered empirical successes. They must exclude the
already-tested feature list and the nine #986 census features.
