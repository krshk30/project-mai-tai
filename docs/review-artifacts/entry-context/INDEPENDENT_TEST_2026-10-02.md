# Independent calm-tape entry test: 2026-10-02

## Plain-language status

**Call: UNMEASURED for the strict decision-time test; the historical fill-time
proxy passes every frozen numerical condition.** This is useful supporting
evidence, not a validated rule to turn on before placing a resting order.

Using the bot's stored bars and independently fetched one-second price paths,
the untouched Webull subset below **1.5%** reached +5% first in **54/84 trades
(64.3% +/- 5.2 percentage points)**; the other group did so in **103/200
(51.5% +/- 3.5 points)**. Uniform return was **+0.37% +/- 0.68%** versus
**-1.31% +/- 0.46%** per trade. The gap survives removing SSM, the best calm
name, and is positive in all three time blocks. All +/- figures are one SE,
not confidence bounds.

The cost is large: it would skip **201/285 (70.5%)** timestamp-eligible
holdout entries and **103/157 (65.6%)** of their known +5%-first winners.
On completed actual fill cycles, calm trades averaged only
**+0.04% +/- 0.39%** gross realized return, not the hypothetical +5/-8 result.

Two reasons not to call this an entry-rule validation:

- The database can overwrite OHLC without updating a revision timestamp.
  It cannot prove that the historical values were the ones the bot knew then.
  Older warm-up bars can also exist in memory without being persisted.
- At original order creation, rather than fill, the comparable holdout gap is
  **0.993 +/- 0.844 points**, below the frozen 1.000-point requirement.
  The calm group's uniform mean becomes **-0.12% +/- 0.71%**. This is a
  diagnostic, not a replacement primary test or a new threshold.

My four untested candidates are executable spread/impact, bid resilience after
sell prints, overhead volume-at-price supply, and stock-specific strength versus
a fixed peer basket. Conditions, mechanisms and falsifiers are below.

Read-only study only. No trading-code change, merge, install, service touch or
Redis snapshot read. Exits stay +5% / -8%; no other threshold was searched.
Main remains reserved at `b8b0dafbdf583ca4af9eddc8dbf922cbf887a482`.

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
   `interval_secs=60`, same symbol, and bar close
   strictly before actual entry time. The bar timestamp convention will be
   checked against the writer, not guessed. Require finite valid OHLC and close
   >0. Both live and REST provenance are reported; no Massive-bar substitution.
   Fewer than ten is UNMEASURED. Also require these ten bars to have been created
   by the entry and not revised after it; otherwise decision-time evidence is
   UNMEASURED. Report raw ten-bar coverage and stricter as-of coverage separately.
   No interpolation. Cross-session rows are flagged and counted, not excluded.
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

## Instrument correction before outcome scoring

The original design commit `96de0ddda62a61fccd0d5548a4c51793bfbac5b3` added an
unrequested same-session restriction. This correction removes it to match the
operator's exact last-ten-closed-bars feature. It was made after the first DB
coverage extract and raw REST acquisition, but **before reading or computing
outcome labels**. The correction commit's description said collection was still
running; the retained request timestamps show its final request was about 13
seconds before that commit. This timing correction does not claim a new data
freeze. Neither the 1.5% threshold, cohort, dates nor
performance gate changes. Keep cross-session counts/age visible. Original
restricted extract is retained as `db.ndjson`; corrected extract is `db-exact.ndjson`.

## Timing and evidence limits

Design frozen and pushed at `96de0ddda62a61fccd0d5548a4c51793bfbac5b3` before
extracting outcomes. The exact-feature correction was committed at
`4a0222ac7cfb7219015d871a24a08666a1bdf300` before outcome scoring. Both commits
remain in this branch; the initial definition is not silently replaced.

The requested endpoint was October 2, with a declared 14:40 ET ceiling. The
actual corrected database extraction began **14:18:09 ET**, and the independent
REST reads ran **14:13:47-14:17:53 ET**. This is an **intraday October 2 snapshot,
not a completed October 2 session or coverage through 14:40**. The exact and
initial extracts generated byte-identical lists of 187 symbol-day requests;
no additional path was needed after the feature correction. Current-day trades
enter the result only if their first barrier was already observed.

All 187 responses completed pagination (187 HTTP 200 responses), containing
2,077,288 one-second aggregates. The largest response was 2,597,596 bytes;
178,919,904 response bytes total, sequentially requested at no more than one
request per second. Each response had a 16 MB safety ceiling. No Redis call was
made. SQL used a read-only transaction, nice 19, idle I/O priority, a 10-second
statement timeout, and one connection; calculation was local.

### Population and coverage

| Stage | Untwinned Webull holdout | Schwab replication |
|---|---:|---:|
| Raw managed-position rows | 544 | 419 |
| Excluded: matched Schwab twin within +/-120 s | 221 | Not applicable |
| Ambiguous BUY mapping | 6 | 2 |
| No matching BUY fill | 4 | 0 |
| Declared population after matching | 313 | 417 |
| At least ten closed bot bars in today's database | 313/313 | 417/417 |
| Timestamp-eligible ten-bar proxy | 285/313 (91.1%) | 395/417 (94.7%) |
| At least one bar first persisted after entry: UNMEASURED | 28 | 22 |
| Resolved uniform outcome among timestamp-eligible | 284/285 | 395/395 |
| Complete actual fill cycle among timestamp-eligible | 249/285 | 350/395 |
| Strict original OHLC version proved | UNMEASURED | UNMEASURED |

The one ambiguous uniform result is RMCF, 2026-08-12 09:54:10.181 ET: the
partial entry second touches a boundary. It is excluded, not assigned a win
or loss. Outcome totals are holdout 157 targets / 126 stops / 1 end-of-day /
1 ambiguous, and replication 225 targets / 156 stops / 14 end-of-day.

There are 84 symbols in the measured holdout and 81 in replication. Latest real
entries are 11:05:53 ET (holdout) and 12:36:19 ET (replication), October 2.
BUY/managed-row matching was at most 74.37 seconds apart in holdout and 29.91
seconds in replication. Two holdout managed-row prices differ from their BUY
VWAP; scoring consistently uses real fill VWAP, not the managed-row price.

Across the raw ten-bar sets, holdout source labels are 3,012 `live` / 118 `rest`;
replication 4,108 `live` / 62 `rest`. These labels are not an immutable delivery
audit: v2's persistence writer does not always supply the source. One raw
cross-session ten-bar set occurs in each cohort; both are excluded by the
timestamp check, not by an extra same-session rule. No missing minute is
interpolated: the definition uses ten observed one-minute bars, not necessarily
ten consecutive wall-clock minutes.

### Primary historical proxy result

Returns below are percentage points per trade, gross. Win means +5% was reached
first; a positive end-of-day close is not relabeled a target-first win.

| Population / fixed group | Resolved N | +5% first, count and rate +/- SE | Uniform mean +/- SE | Symbol-bootstrap SE of mean |
|---|---:|---|---|---:|
| Holdout <1.5% | 84 | 54; 64.3% +/- 5.2 pp | +0.37% +/- 0.68% | 0.84 pp |
| Holdout >=1.5% | 200 | 103; 51.5% +/- 3.5 pp | -1.31% +/- 0.46% | 0.56 pp |
| Schwab <1.5% | 148 | 99; 66.9% +/- 3.9 pp | +1.06% +/- 0.47% | 0.59 pp |
| Schwab >=1.5% | 247 | 126; 51.0% +/- 3.2 pp | -1.29% +/- 0.41% | 0.47 pp |

Holdout difference: **+1.68 +/- 0.82 pp**; Schwab replication difference:
**+2.35 +/- 0.63 pp**. The unfiltered holdout mean is **-0.81% +/- 0.38%**
(284 trades). A higher average for retained trades is not a forecast of total
portfolio profit. The bootstrap resamples symbols, not independent trades;
neither error estimate establishes a causal effect or accounts for all regime risk.

| Frozen holdout requirement | Measured proxy value | Numerical call |
|---|---|---|
| >=40 calm observations | 84 | PASS |
| Calm wins >=60% | 54/84 = 64.3% +/- 5.2 pp | PASS |
| Other wins <=55% | 103/200 = 51.5% +/- 3.5 pp | PASS |
| Mean gap >=1.0 point | +1.68 +/- 0.82 pp | PASS |
| Positive gap after removing best name | Remove SSM (5 calm trades, +25 summed percentage points); remaining gap +1.38 +/- 0.85 pp | PASS |
| Positive gap in >=2 of 3 time blocks | 3 of 3, values below | PASS |
| Original, decision-time feature values proved | No versioned OHLC history; timestamps alone do not establish this | UNMEASURED |

The last row is the frozen instrument requirement, not a new performance gate.
Accordingly the **strict study call remains UNMEASURED**, while the historical
proxy meets every numerical gate. This is not a production failure or a
claim that the hypothesis has been disproved.

### Time blocks and replication halves

| Holdout ET entry block | Calm N / other N | Calm mean +/- SE | Other mean +/- SE | Gap +/- SE |
|---|---|---|---|---|
| 07:00-09:30 | 16 / 29 | +0.13% +/- 1.63% | +0.07% +/- 1.19% | +0.06 +/- 2.02 pp |
| 09:30-11:00 | 6 / 50 | -1.50% +/- 2.91% | -2.02% +/- 0.93% | +0.52 +/- 3.05 pp |
| 11:00-16:00 | 62 / 121 | +0.62% +/- 0.78% | -1.34% +/- 0.59% | +1.95 +/- 0.98 pp |

The first two positive signs have large uncertainty and the middle block only
six calm observations. They meet the predeclared sign test, not a stronger
claim of dependable profitability in each block.

| Separate Schwab replication | Calm N; wins +/- SE; mean +/- SE | Other N; wins +/- SE; mean +/- SE |
|---|---|---|
| Aug 03-Sep 13 (split before Sep 14) | 110; 65.5% +/- 4.5 pp; +0.98% +/- 0.55% | 181; 53.6% +/- 3.7 pp; -0.92% +/- 0.48% |
| Sep 14-Oct 02 intraday | 38; 71.1% +/- 7.4 pp; +1.31% +/- 0.95% | 66; 43.9% +/- 6.1 pp; -2.29% +/- 0.80% |

The direction replicates. This is not a row-identical reproduction of the
reviewer's 290+102 trades: our declared matching starts with 417 real matched
Schwab entries, then 395 timestamp-eligible entries. A trade-ID reconciliation
would be needed to explain every population difference; the supplied aggregate
claim cannot do that. We did not tune exclusions to recover its counts.

### Real fill P&L, not barrier P&L

Only complete, single-entry-order flat-to-flat cycles are attributed. Partial
executions of that same order are combined. Multiple entry orders, incomplete
inventory and manual closes without matched SELL fills are not invented.
These are gross cashflows at actual execution prices; commissions/fees absent
from the extract are not included. Small dollar totals reflect historical
tiny-share trading, not the planned $600/$300 sizing.

| Cohort / feature at fill | Actual N | Actual return/trade +/- SE | Actual dollars/trade +/- SE | Actual return symbol-bootstrap SE |
|---|---:|---|---|---:|
| Holdout calm | 72 | +0.038% +/- 0.387% | +$0.0117 +/- $0.0151 | 0.419 pp |
| Holdout other | 177 | -0.753% +/- 0.337% | -$0.0398 +/- $0.0193 | 0.376 pp |
| Schwab calm | 130 | +0.385% +/- 0.334% | +$0.0509 +/- $0.0307 | 0.355 pp |
| Schwab other | 220 | -0.828% +/- 0.278% | -$0.0643 +/- $0.0326 | 0.285 pp |

For an apples-to-apples outcome/P&L comparison, require both kinds of outcome:

| Matched subset | N | Uniform return +/- SE | Actual return +/- SE |
|---|---:|---|---|
| Holdout calm | 72 | +0.503% +/- 0.732% | +0.038% +/- 0.387% |
| Holdout other | 176 | -1.352% +/- 0.491% | -0.775% +/- 0.339% |
| Schwab calm | 130 | +0.814% +/- 0.518% | +0.385% +/- 0.334% |
| Schwab other | 220 | -1.643% +/- 0.436% | -0.828% +/- 0.278% |

Actual-return gap on that matched subset is **+0.813 +/- 0.514 pp** in holdout,
**+1.214 +/- 0.435 pp** in replication. Historical strategy exits include
confirmation/flip and other exits, so actual results need not equal the
uniform +5/-8 score. Real P&L does not demonstrate a reliably positive calm
holdout mean after costs.

### What filtering would cost

| Cost on timestamp-eligible entries | Holdout | Schwab replication |
|---|---:|---:|
| Entries skipped at >=1.5% | 201/285 = 70.5% | 247/395 = 62.5% |
| Known target-first winners forgone | 103/157 = 65.6% | 126/225 = 56.0% |
| Feature UNMEASURED, policy not assumed | 28/313 = 8.9% | 22/417 = 5.3% |
| Unattributed actual cycles among timestamp-eligible | 36/285 | 45/395 |
| Actual gross dollars: all completed cycles | -$6.2016 | -$7.5261 |
| Actual gross dollars in would-skip cycles | -$7.0422 | -$14.1483 |
| Actual gross dollars in retained cycles | +$0.8406 | +$6.6222 |

Dollar rows are totals, not per-trade estimates. The per-trade SEs are above.
Do not interpret subtracting the losing bucket as a realizable portfolio gain:
skipping a trade changes cash, slot ownership, retries and later opportunities.
No rule for missing-feature trades was tested or silently added.

### Can it be known before the rest is placed?

| Check | Holdout | Schwab replication |
|---|---:|---:|
| Timestamp-eligible feature at original order creation | 296/313 | 402/417 |
| Comparable at both creation and fill | 284/285 | 393/395 |
| Group changes between those clocks | 9/284 | 22/393 |
| Real entries before 07:10 in this population | 0: UNMEASURED | 0: UNMEASURED |

On the comparable holdout, grouping at order creation gives 81 calm resolved
trades and 202 other resolved trades: wins **60.5% +/- 5.4 pp** versus
**53.0% +/- 3.5 pp**; uniform means **-0.121% +/- 0.709%** versus
**-1.114% +/- 0.458%**. The gap is **0.992779 +/- 0.843589 pp**. It is
**below**, not rounded up to, the frozen 1.000-point requirement. This diagnostic
does not replace the fill-time primary test; it directly cautions against
implementing the fill-time association as a pre-placement filter. Order creation
is itself a proxy for submission and does not reconstruct every later reprice.

Code/design read at base `b8b0dafb`:

- `services/schwab_1m_v2_bot.py:_persist_bar` (4888-4935) stores bar start
  timestamps and upserts OHLCV only. The bar-close test is start+60 seconds <
  anchor. Its conflict update does not set `updated_at`. A read-only PostgreSQL
  catalog check found **zero non-internal triggers** on this table, and both
  timestamp defaults are `CURRENT_TIMESTAMP`; there is no hidden update trigger
  repairing this evidence gap. We cannot certify the historical OHLC version.
- `_handle_bar` (4395-4430) feeds all bars into memory but persists only bars
  <=300 seconds old at processing. The ten retained database rows can therefore
  differ from the ten bars in memory, especially just after warm-up.
- `_seed_strategy_bars_from_db` (4249 onward) can replay up to 250 rows;
  watchlist additions also use REST warm-up. Thus **pre-07:10 is possible** if
  ten valid completed bars have already been delivered. It does not inherently
  require waiting until 07:10. Conversely, a just-added name with fewer than
  ten delivered bars is **UNMEASURED**, not calm. This extract has no pre-07:10
  trades and no complete per-add in-memory bar inventory, so neither edge is
  certified by this study.

A decision-time bar-value snapshot attached to each entry intent would close
that measurement gap in a separately approved study. This report does not
implement it or alter missing-data trading behavior.

## My candidates: untested, not new claimed findings

These four are distinct from MACD sign/direction, time of day, day-high distance,
volume-window ratios, earlier losses, duration below ATR, target/floor variants,
and the nine frozen #986 census features. They are also not rediscoveries of
the already-known weak 30-minute-change, flip-count, VWAP or first-trade effects.
None was fitted to the outcomes above. Any tuning belongs in a declared training
sample followed by a fresh, untouched time block; keep +5/-8 unchanged.

| Candidate and one-sentence condition | Why it could matter for an ATR resting buy | Test on real fills | What would falsify it |
|---|---|---|---|
| **Executable liquidity:** take a rest only if the observed spread plus estimated entry impact at the intended share size is <=0.5% of price. | A chart cross can be genuine while a thin ask makes the attainable entry much worse; this measures execution cost, not candle range. | Reconstruct point-in-time bid/ask and depth before each original rest/reprice; freeze the cost model before a new holdout; compare both uniform and actual fill returns, unfilled opportunities, and slippage at matched candle range. Depth missing = UNMEASURED. | No improvement after execution costs, or the effect vanishes after spread/range matching or dropping the best symbol. A result based only on future fill spread is invalid. |
| **Bid resilience:** permit entry only when the bid has recovered to its pre-event level within five seconds after the most recent clearly sell-initiated print in the preceding minute. | A bid that absorbs sellers may support a breakout; repeated bid erosion signals fragile demand even if total volume is high. | Use ordered trade/NBBO receipt data before the intent; predeclare trade classification and freshness; compare filled trades and skipped winners on an untouched period. Unknown aggressor or no qualifying event is UNMEASURED. | Matched high-range/low-range cohorts show no actual-return or slippage advantage, or the sign reverses on a second time block. |
| **Overhead supply:** permit entry only when less than 20% of the day's prior executed volume lies between the proposed entry and its +5% target. | A large concentration of recent holders just overhead could supply selling before the target; this is a volume-at-price map, not distance from the high or VWAP. | Build the profile from only prints received before each original intent, replay the exact proposed price band, and compare the first-target outcome and actual P&L at matched range/spread. Freeze 20% before a new sample. | No separation after those controls, inconsistent sign after dropping the leading symbol/day, or results require post-entry prints. |
| **Peer-relative demand:** take the cross only if its preceding five-minute return exceeds the median of a preselected price/liquidity peer basket. | A stock-specific bid may be more durable than merely following a broad speculative burst; it is relative demand, not the already-tested absolute 30-minute change. | Freeze a survivorship-safe basket before each day, exclude the target stock, align receipt times, and test on new real fills while matching absolute return, range and spread. | The residual adds nothing after those matches or depends on a retrospectively chosen basket. |

For each candidate, a better mean alone is insufficient: show entry retention,
winners forgone, symbol/day robustness, real fill P&L with SE, and whether the
condition was actually available before placing the order. These are research
proposals, not authorization to build or change entry rules.

## Reproduction and raw evidence

Evidence directory (local, not on the production box):
`/Users/velkris/.codex/study-evidence/entry-context-20261002/`.
No reviewer outcome labels or reviewer price-path dump was used. The source
vendor remains Massive for the independent second-level outcome path, so this
is not an independent-vendor validation. The untwinned Webull subset is not a
different market regime, and account/routing selection can confound the result.
August-October rule changes, original feature-search selection, costs, and
correlated symbols/days also limit inference. No Jun-Jul result or alternate
threshold is offered as a rescue.

| Raw file | Bytes | SHA256 |
|---|---:|---|
| `db-exact.ndjson` | 7091107 | `cafdd49101760da0e2f237de4a3b5cb6c1738a2a899f7a63066a9925ef74e918` |
| `price_jobs_exact.json` | 15417 | `fad9da3dfa2cf4d3d6fddd328189fdb057145d6a640d982193dd019433a1f0b5` |
| `paths.ndjson` | 212185017 | `cf38dbb19ee78d3b967c8808deac30422098a9cc72d0e7a4dd4bc54a9df93498` |
| `result.json` | 29744 | `990a65eda22e9b6ab16055edf5602d1d61a7cd65b923348dfc4c11b9e4fc169d` |
| `result.json.trades.json` | 8442829 | `1b94594acf8a3fd65f3e22bd42aff2e8283b25249413017985442afdbfe003ea` |

`bar-catalog.ndjson` records the read-only trigger/default inspection;
`verification.json` records a second, Decimal-based first-touch implementation:
**680 checked, zero label/return mismatches** (679 resolved, one ambiguous).
The generated aggregate results and audit are included beside this report;
large raw extracts and the per-trade audit ledger stay at the local paths above.

Research instruments are included here, not under runtime `src/`:
`collect_readonly.py`, `fetch_paths_readonly.py`, `score_study.py`,
`audit_results.py`, `inspect_bar_catalog_readonly.py`, `verify_raw_outcomes.py`.
The scorer's **13 offline tests pass**: threshold equality, insufficient bars,
late revision, first-touch ordering, both-touch ambiguity, partial entry-second
ambiguity, incomplete/open paths, missing endpoint, SE, complete partial-fill
cashflow, unclosed cycle, multiple-entry cycle and oversold-cycle rejection.

Local rerun (replace `$PY` with an available Python interpreter):

```sh
DIR=docs/review-artifacts/entry-context
RAW=/Users/velkris/.codex/study-evidence/entry-context-20261002
"$PY" -m unittest discover -s "$DIR" -p test_score_study.py -v
"$PY" "$DIR/score_study.py" "$RAW/db-exact.ndjson" --paths "$RAW/paths.ndjson" --output "$RAW/result.json"
"$PY" "$DIR/audit_results.py" "$RAW" --output "$RAW/audit.json"
"$PY" "$DIR/verify_raw_outcomes.py" "$RAW"
```

Branch: `codex/entry-calm-tape-independent-study`, unmerged. No service restart,
flag edit, production file write or main merge is part of this deliverable.
