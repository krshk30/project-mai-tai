# Independent entry-context tests: 2026-10-02

## Three-claim result in plain language

**A = UNMEASURED; B = UNMEASURED; C = UNMEASURED on the new blind test.**
There are only 34 untouched June-July Webull entries, below the required 40
**good** trades for any claim. I did not change the population after learning
that. The earlier August-October outcomes were already seen during A's study,
so the extension treats them as supporting evidence, not another blind test.

| Claim | What the evidence says | Cost of good-only retention on supporting Webull sample |
|---|---|---|
| A: ten-bar calm tape <1.5% | Strongest supporting pattern; original numerical proxy gate passes, but original decision-time OHLC cannot be certified. Order-creation diagnostic misses the one-point gap. | Skip 201/285 entries (70.5%) and 103/157 known target-first winners (65.6%). |
| B: bounce from real bot segment low <4% | Not enough low-bounce observations: only **1 good Webull trade and 8 good Schwab trades**. Real bot state does not substantiate the claimed 57-trade good bucket. | Skip 49/50 measurable entries (98.0%) and 25/26 known winners (96.2%); another 263/313 lack the required segment evidence. |
| C: last-120-bar box <20% | A barely positive standalone proxy comparison, not a dependable added filter. After A is applied, C's incremental gap is negative in both supporting samples. Stored 120 bars can be much older than two hours. | Skip 196/260 measurable entries (75.4%) and 100/139 known winners (71.9%); another 53/313 are unmeasured. |

The new blind A/C results have the favorable direction but only 8 and 6 good
observations, respectively. B cannot be reconstructed there from retained bot
probes. **None earns a new independent PASS or authorization to change entry
rules.** In particular, do not combine three correlated descriptions and call
that three independent confirmations. Exits remain +5% / -8%.

Four candidates remain untested proposals: executable liquidity, bid resilience,
overhead volume-at-price supply and peer-relative demand. Their mechanisms,
real-fill test designs and falsifiers remain in the candidate table below.

## Three-claim results and coverage

The extension design was committed and pushed at `e43d83b9` before its database
extraction, new price paths or B/C feature/outcome joins. The design and gates
are retained below. No alternative cutoff was fitted. Means are percentages
per trade, +/- one ordinary SE; `SE unavailable` means n<2, not zero risk.
Full symbol-bootstrap SEs, same-subset P&L tables and robustness tests are in
`THREE_CLAIM_RESULTS_2026-10-02.json` beside this report.

| Population | Matched entries | A timestamp-eligible | B complete recorded segment | C timestamp-eligible | Role |
|---|---:|---:|---:|---:|---|
| Untwinned Webull Jun 17-Jul 31 | 34 | 33 | 0 | 29 | New blind primary; actual entries Jul 27-31 |
| Untwinned Webull Aug 03-Oct 02 | 313 | 285 | 50 | 260 | Already-seen supporting sample |
| Schwab Aug 03-Oct 02 | 417 | 395 | 120 | 368 | Replication only |

The June-July extract had 56 Webull managed entries: 22 had a Schwab twin and
34 remained, with no unmatched/ambiguous BUY mapping. It also contained 154
Schwab entries used only to identify twins, not to enlarge the blind test.

C raw 120-row coverage is 34/34, 289/313 and 389/417 respectively. Timestamp
checks reject 5, 29 and 21 of those sets; another 24/28 recent Webull/Schwab
entries lack 120 rows. A/C timestamps remain only proxies for original values:
the OHLC revision-history problem in the prior study is unchanged.

31 retained v2 log files yielded 119,940 parsable probes, observed from
2026-09-05 00:05:07 UTC through 2026-10-02 18:43:02 UTC. No July entry has a
pre-entry probe. In recent Webull, B has 262 missing-probe entries and one with
no short segment within three bars; in Schwab, 289 and eight respectively.
No vendor-ATR substitute or entry-minute-open proxy is used. B's actual fill
VWAP and complete, age-checked bot short run differ materially from the
reviewer's instruments; exact trade-ID reconciliation is still needed before
attributing the different good-bucket count to one specific cause.

### New blind primary

| Claim / bucket | Resolved N | Target-first rate +/- SE | Uniform return +/- SE | Actual completed N; return +/- SE |
|---|---:|---|---|---|
| A good <1.5% | 8 | 5/8; 62.5% +/- 17.1 pp | +1.37% +/- 2.08% | 5; +0.26% +/- 1.38% |
| A bad >=1.5% | 25 | 12/25; 48.0% +/- 10.0 pp | -1.76% +/- 1.33% | 22; -1.10% +/- 0.76% |
| B good / bad | 0 / 0 | UNMEASURED | UNMEASURED | UNMEASURED |
| C good <20% | 6 | 4/6; 66.7% +/- 19.2 pp | +0.67% +/- 2.74% | 2; -1.32% +/- 3.58% |
| C middle [20%,35%) | 9 | Not used in gate | -0.78% +/- 2.28% | Included in cost, not relabeled bad |
| C bad >=35% | 14 | 6/14; 42.9% +/- 13.2 pp | -2.43% +/- 1.78% | 14; -0.66% +/- 0.90% |

A's gap is **+3.13 +/- 2.46 pp** and C's **+3.10 +/- 3.27 pp**. Neither has
40 good trades. A has no good pre-11:00 observations, so only one time block
is comparable. C is negative in 09:30-11:00 (one good trade, SE unavailable)
and positive in 11:00-16:00; the required two favorable blocks do not hold.
These are underpowered, not validated rejections of either hypothesis.

Removing best name BIYA leaves A's gap **+2.64 +/- 2.70 pp**; removing ENTX
leaves C's **+0.93 +/- 4.16 pp**. This does not repair the count/time-block gates.
Good-only A would skip **25/33 entries and 12/17 winners**; C would skip
**23/29 entries and 11/15 winners**. B's cost is unknown, not zero.

### Supporting and replication tables: B and C

A's unchanged full tables remain below. These B/C tables do not rescue the blind
gate. All N are resolved uniform outcomes; actual P&L has its own denominator.

| Sample / claim / bucket | N | Target-first rate +/- SE | Uniform return +/- SE | Actual N; return +/- SE |
|---|---:|---|---|---|
| Webull B good <4% | 1 | 1/1; 100%, uninformative single observation | +5.00%; SE unavailable | 0; UNMEASURED |
| Webull B bad >=7% | 37 | 17/37; 45.9% +/- 8.2 pp | -2.03% +/- 1.08% | 31; -0.85% +/- 1.20% |
| Schwab B good <4% | 8 | 5/8; 62.5% +/- 17.1 pp | +0.13% +/- 2.38% | 7; +0.91% +/- 1.59% |
| Schwab B bad >=7% | 81 | 40/81; 49.4% +/- 5.6 pp | -1.55% +/- 0.72% | 77; -0.88% +/- 0.62% |
| Webull C good <20% | 64 | 39/64; 60.9% +/- 6.1 pp | -0.06% +/- 0.80% | 55; -0.48% +/- 0.47% |
| Webull C bad >=35% | 120 | 64/120; 53.3% +/- 4.6 pp | -1.07% +/- 0.59% | 109; -0.62% +/- 0.41% |
| Schwab C good <20% | 113 | 68/113; 60.2% +/- 4.6 pp | +0.33% +/- 0.56% | 97; -0.42% +/- 0.34% |
| Schwab C bad >=35% | 148 | 80/148; 54.1% +/- 4.1 pp | -0.97% +/- 0.53% | 134; -0.63% +/- 0.40% |

The plug-in binomial SE for 1/1 is mechanically zero; it is not useful
uncertainty evidence and is not presented as precision. The sole good Webull B
trade is YMAT; removing it leaves no comparison. Schwab B has a
**+1.67 +/- 2.49 pp** gap, only eight good trades, and no good observations in
the first two time blocks: UNMEASURED, not a new confirmation.

Middle-bucket uniform returns: Webull B n=12, **+0.77% +/- 1.81%**; Schwab B
n=31, **+0.39% +/- 1.14%**; Webull C n=75, **-1.76% +/- 0.76%**; Schwab C
n=107, **-0.31% +/- 0.62%**. Middle is skipped by good-only retention but is
never inserted into the bad bucket to improve the gate.

| Standalone C numerical robustness (not blind) | Webull | Schwab |
|---|---|---|
| Good-minus-bad gap | +1.007 +/- 0.994 pp | +1.308 +/- 0.774 pp |
| Drop best name | YYGH: +0.63 +/- 1.03 pp | XOS: +1.00 +/- 0.79 pp |
| 07:00-09:30 gap | -3.25 +/- 2.88 pp (8/16 good/bad) | -1.65 +/- 3.48 pp (9/7) |
| 09:30-11:00 gap | +2.49 +/- 2.66 pp (8/30) | +6.24 +/- 1.33 pp (7/25) |
| 11:00-16:00 gap | +1.22 +/- 1.18 pp (48/74) | +1.18 +/- 0.85 pp (97/116) |
| Proxy numerical gate | PASS, barely | PASS |

The drop-best requirement is positive sign, not a second >=1-point test.
These passes do not supply missing decision-time fidelity or a blind sample.
Schwab C direction holds across the two historical halves: good/bad means
**+0.31% +/- 0.63% / -0.50% +/- 0.63%** (n=88/104) before September 14, and
**+0.43% +/- 1.25% / -2.09% +/- 0.99%** (n=25/44) afterward. Webull C reverses
in the latter half: **-1.50% +/- 2.91% / -0.32% +/- 1.39%** (n=6/22).

Actual completed cashflows are at real historical share quantities, not the
planned dollar sizing. Actual dollars/trade +/- SE:

| Sample / claim | Good bucket | Bad bucket |
|---|---|---|
| Blind A | n=5; +$0.0261 +/- $0.0363 | n=22; -$0.0394 +/- $0.0332 |
| Blind C | n=2; -$0.0525 +/- $0.1375 | n=14; -$0.0110 +/- $0.0383 |
| Supporting Webull B | n=0; UNMEASURED | n=31; -$0.0737 +/- $0.0439 |
| Supporting Webull C | n=55; -$0.0240 +/- $0.0239 | n=109; -$0.0203 +/- $0.0253 |
| Replication Schwab B | n=7; +$0.0086 +/- $0.0798 | n=77; -$0.0568 +/- $0.0655 |
| Replication Schwab C | n=97; -$0.0084 +/- $0.0302 | n=134; -$0.0386 +/- $0.0450 |

On the identical completed-fill subset, Webull C uniform means are
**-0.18% +/- 0.86%** (n=55 good) versus **-1.02% +/- 0.63%** (n=108 bad);
actual means **-0.48% +/- 0.47%** versus **-0.65% +/- 0.41%**. Schwab C has
n=97/134, uniform **+0.23% +/- 0.61% / -1.50% +/- 0.56%**, actual
**-0.42% +/- 0.34% / -0.63% +/- 0.40%**. These are not demonstrated positive
actual-profit buckets. Complete same-subset B tables are retained in the JSON.

### Overlap and incremental information

Only entries with all three measurable features appear in this cross-table.
`1` means good; `0` means a measured bad OR middle bucket. An unknown feature
does not become a zero. The blind sample has **zero complete triples**, so
its cross-table is UNMEASURED, not eight zero-valued behavioral counts.

| A/B/C good mask | Supporting Webull | Replication Schwab |
|---|---:|---:|
| 000 | 28 | 56 |
| 001 | 4 | 4 |
| 010 | 0 | 0 |
| 011 | 0 | 0 |
| 100 | 10 | 20 |
| 101 | 4 | 20 |
| 110 | 0 | 2 |
| 111 | 1 | 6 |
| Total complete triples | 47 | 108 |
| Good on 0 / 1 / 2 / 3 | 28 / 14 / 4 / 1 | 56 / 24 / 22 / 6 |
| At least one feature unknown | 266/313 | 309/417 |

Conditional comparisons use pairwise feature availability, so missing B does
not unnecessarily discard A/C evidence. The JSON also includes the stricter
complete-triple versions. Rows below compare the added rule's good vs bad
bucket **within the first rule's good bucket**; middle is omitted.

| Conditional question | Webull: good/bad N; uniform gap +/- SE | Schwab: good/bad N; uniform gap +/- SE |
|---|---|---|
| Does C add after A? | 39/17; **-1.58 +/- 1.71 pp** | 84/17; **-0.64 +/- 1.61 pp** |
| Does A add after C? | 39/25; +1.08 +/- 1.66 pp | 84/29; +0.78 +/- 1.35 pp |
| Does B add after A? | 1/4; +9.75 pp, SE unavailable | 8/13; -1.09 +/- 2.90 pp |
| Does B add after C? | 1/4; +9.75 pp, SE unavailable | 6/7; +2.70 +/- 3.72 pp |
| Does A add after B? | 1/0; UNMEASURED | 8/0; UNMEASURED |
| Does C add after B? | 1/0; UNMEASURED | 6/0; UNMEASURED |

**Call: C has not demonstrated incremental benefit after A; B's incremental
value is UNMEASURED.** These are descriptive comparisons with substantial SE,
not evidence that any condition causally harms trades. Among A-good trades,
Webull C-good actual mean is **+0.15% +/- 0.56%** (n=32) versus C-bad
**+0.50% +/- 0.86%** (n=15); Schwab is **-0.44% +/- 0.38%** (n=71) versus
**+1.63% +/- 1.66%** (n=17). Actual fills do not reverse the caution.

Requiring all three keeps one supporting Webull trade (uniform +5%, SE and
actual P&L unavailable) and six Schwab trades (uniform **+0.67% +/- 2.74%**;
five completed actual cycles average **-0.82% +/- 1.64%**). Not a validated
combined rule. No combination was optimized.

### Cost and availability

| Sample / claim | Measured good / middle / bad | Entries skipped | Winners forgone | Unknown features |
|---|---|---|---|---|
| Blind A | 8 / 0 / 25 | 25/33 (75.8%) | 12/17 (70.6%) | 1/34 |
| Blind B | 0 / 0 / 0 | UNMEASURED | UNMEASURED | 34/34 |
| Blind C | 6 / 9 / 14 | 23/29 (79.3%) | 11/15 (73.3%) | 5/34 |
| Supporting A | 84 / 0 / 201 | 201/285 (70.5%) | 103/157 (65.6%) | 28/313 |
| Supporting B | 1 / 12 / 37 | 49/50 (98.0%) | 25/26 (96.2%) | 263/313 |
| Supporting C | 64 / 75 / 121 | 196/260 (75.4%) | 100/139 (71.9%) | 53/313 |
| Replication A | 148 / 0 / 247 | 247/395 (62.5%) | 126/225 (56.0%) | 22/417 |
| Replication B | 8 / 31 / 81 | 112/120 (93.3%) | 60/65 (92.3%) | 297/417 |
| Replication C | 113 / 107 / 148 | 255/368 (69.3%) | 142/210 (67.6%) | 49/417 |

One supporting Webull bad-bucket outcome is entry-second ambiguous, explaining
the 201/200 and 121/120 entry/outcome differences. Unknown-feature policies are
not specified or simulated. These counts are not a portfolio replay.

At **07:10** or immediately after a watchlist add, A needs ten already-delivered
closed bars; C needs 120; B needs the complete preceding short run and its low.
Warm-up can supply them without waiting ten/120 new minutes, but it can also
leave insufficient history. The current implementation consumes older REST
bars in memory without persisting all of them. There are no pre-07:10 entries
in these samples and no complete per-add memory inventory, so those edge cases
remain UNMEASURED. B at the actual fill price also cannot be known exactly
before placing an order; using the proposed wire limit would be a separately
specified instrument, not silently substituted here.

### C clock-span diagnostic and CYCU

The card's literal last-120-observed-bars proxy can differ from its name,
"two-hour box." Among raw 120-bar sets, 8/34 blind, 48/289 supporting, and
56/389 replication sets cross a session date. Median span is 123, 120 and 120
minutes, respectively; those medians hide very old tails. Of timestamp-eligible
sets, 8, 45 and 51 cross sessions. This limits C's interpretation even when
its numerical proxy gate passes.

**Post-hoc instrument sensitivity, NOT the frozen result:** retain only 120
consecutive minutes whose last close is >0 and <=60 seconds before entry.
The feature and its <20/>=35 thresholds are unchanged; no threshold sweep.
This diagnostic was added after seeing CYCU's stale-history case.

| Exact-clock sensitivity | Good / bad N | Good mean +/- SE | Bad mean +/- SE | Gap +/- SE | Numerical diagnostic |
|---|---|---|---|---|---|
| Blind | 5 / 2 | +2.40% +/- 2.60% | +5.00% +/- 0.00% | -2.60 +/- 2.60 pp | UNMEASURED: too few |
| Supporting Webull | 53 / 52 | -0.13% +/- 0.88% | -0.75% +/- 0.90% | +0.62 +/- 1.26 pp | FAIL: gap <1 and bad wins 55.8% >55 |
| Replication Schwab | 101 / 53 | +0.36% +/- 0.59% | -2.11% +/- 0.90% | +2.48 +/- 1.07 pp | Proxy PASS, not independent validation |

Zero sample SE for two identical +5 outcomes is not certainty. Exact-clock
supporting C-good actual P&L remains **-0.48% +/- 0.53%** (n=45), and Schwab
**-0.45% +/- 0.36%** (n=88).

CYCU's three Schwab entries in the fixed replication extract illustrate the
operator's concern without fitting a rule to them:

| Entry ET / actual price | Bot short-segment start/end ET | Start-bar low / segment low | B: bounce already spent | C: raw box / span |
|---|---|---|---|---|
| 12:12:19 / $4.105 | 11:56-12:11 | $3.8200 / $3.8100 | 7.743% | 481.532% / 86,569 minutes: NOT a two-hour box |
| 12:33:14 / $4.120 | 12:21-12:32 | $3.8000 / $3.7601 | 9.572% | 35.050% / 120 minutes |
| 12:36:19 / $4.140 | 12:21-12:35 | $3.8000 / $3.7601 | 10.103% | 32.708% / 120 minutes |

Thus the real entries were already 7.7-10.1% above their logged short-run lows.
The first C value cannot describe the contemporaneous two-hour range. The
qualitative "floor held within 2% / real downtrend / floor broken >8%" census
remains **UNMEASURED pending the reference-floor definition** requested from
the operator. I have not assigned those labels from a guessed reference.

### Extension provenance and checks

All raw files remain local under
`/Users/velkris/.codex/study-evidence/entry-context-20261002/`.
The extension retained the old population/trade IDs and intraday outcomes for
support/replication. It acquired 15 July symbol-day paths plus one previously
uncovered September SUNE path: 16 complete HTTP responses, 114,500 one-second
bars, maximum response 2,123,185 bytes. The filename `three-blind-paths.ndjson`
contains that one supporting path too. No bulk Redis read or production write.

The 250-row collection ceiling is a read bound, not a new feature: A uses ten,
C 120, and B is unmeasured if its run start lies outside the available rows.
The June-July SQL extract was at 14:41:49 ET; the new recent-feature extract
completed 14:43:46 ET. Probe collection ended after reading the current file
at 14:43 ET. Original A features and earlier intraday price-path cutoff remain
unchanged. This is not a complete October 2 session.

| Artifact | SHA256 |
|---|---|
| `three-blind.ndjson` | `58c36c75187e9d77ea9042cf8cc77d50bd9f43c55f4f2a5544da12886a108d87` |
| `three-support.ndjson` | `c39683a6c63a07c62a059f38cbf3725d67126f088a0115ab7a939572912b3de8` |
| `probes.ndjson` | `5107d1c263bad58253779cc69bbc580b8874ea22da9888a89f70d5f864e31423` |
| `three-blind-paths.ndjson` | `12752823db6d40c3c7e14a4f72efaee682c9fdaa5f2ae7dafc933db91748c05d` |
| `three-result.json` | `a325c99c44ff81239c799ee1a7f12b029523f31fc7d01586ca394b5512d0280c` |
| `three-ledger.json` | `a52d83f7c19f5f5d8b18774bc357cbb64bd283f7a2402b22f7a2af4561e806d1` |

22 offline instrument tests pass, including exact bucket boundaries, 119 versus
120 bars, post-entry data exclusion, use of real fill price, the three-bar limit,
missing/late/conflicting probes, and missing or broken segment starts. No
runtime code is changed. The separate Decimal first-touch implementation checks
**714 eligible entries with zero outcome mismatches**: 400 targets, 297 stops,
16 end-of-day outcomes and one ambiguous entry second. Its output is retained
in `three-verification.json`. All three-claim tables can be rebuilt locally with:

```sh
"$PY" docs/review-artifacts/entry-context/three_claim_study.py /Users/velkris/.codex/study-evidence/entry-context-20261002
"$PY" -m unittest discover -s docs/review-artifacts/entry-context -p 'test_*study.py' -v
"$PY" docs/review-artifacts/entry-context/verify_raw_outcomes.py /Users/velkris/.codex/study-evidence/entry-context-20261002 --extension
```

The results JSON preserves all intermediate tables, the middle groups, both
overlap denominators, same-subset actual P&L, symbol-bootstrap SEs and the
explicitly post-hoc sensitivity. Large raw logs/bar extracts are not committed.

## Three-claim extension: frozen before new feature/outcome joins

**FROZEN DESIGN at e43d83b9, before B/C results.** The new operator card
supersedes the single-claim scope. The completed A-only study below is retained
as prior evidence, not relabeled as a blind three-claim result.

The analyst has already seen August-October outcome labels for that study.
Therefore the new blind primary population for **all three claims** is real
Webull `live:orb` ATR entries dated **2026-06-17 through 2026-07-31 ET**, with no
same-symbol Schwab ATR BUY first fill within inclusive +/-120 seconds. Neither
this population's counts nor its outcomes have been extracted for this study.
It is also outside the reviewer's stated Schwab-only population. If it is small,
has no live fills, or lacks historical bot features, call it UNMEASURED; do not
switch samples after seeing that. Entries before the resting-order rollout are
an explicitly accepted transportability limitation, not evidence about the
current rest mechanism.

Separate supporting tables will use the **already-seen** untwinned Webull
Aug 03-Oct 02 sample, and a **replication-only** Schwab Aug 03-Oct 02 sample.
Those are not substitutes for the new blind primary gate. Their existing raw
price paths/outcomes are reused with their original intraday cutoff; no quiet
refresh or repeated threshold search. Matching, uniform +5/-8/19:55 outcomes,
ambiguous seconds, actual-fill cashflows, and SE conventions stay as originally
frozen below. New June-July paths are independently requested, unadjusted.

### Frozen feature definitions

| Claim | Exact feature before actual entry | Good | Bad | Middle |
|---|---|---|---|---|
| A: calm tape | Mean of 100*(high-low)/close over the last ten closed bot 1-min bars | <1.5% | >=1.5% | None |
| B: bounce spent | 100*(real BUY fill VWAP / minimum low in the bot's last qualifying contiguous ATR-short segment - 1) | <4% | >=7% | [4%,7%) |
| C: two-hour box | 100*(maximum high / minimum low - 1) over the last 120 closed bot 1-min bars | <20% | >=35% | [20%,35%) |

Bar start+60 seconds must be strictly before the entry anchor. A and C require
all 10/120 finite, valid rows; raw coverage, timestamp eligibility and strict
historical-version limitations are reported separately. Do not interpolate
missing minutes or substitute provider bars. The card says 120 bars: report
their elapsed span and cross-session use rather than silently pretending they
always cover exactly two clock hours.

B uses **recorded V2-ATR-PROBE state**, never the reviewer's reconstructed ATR
or an invented replay initialized at 04:00. The qualifying short segment ends
at the last closed bot bar or at most three observed closed bars before it.
Require an unambiguous probe for every included bar, captured before entry;
require a complete short run with a `SELL`, age=0 start and monotonically
incrementing ages through its end. Its lows come from those bot probes.
Missing run start, missing probe, conflicting probe values, broken age sequence
or ambiguous timestamp = UNMEASURED. This is deliberately not filled in with
Massive ATR. An unknown in-memory/DB mismatch remains a strict fidelity limit.

### Frozen per-claim gate

Only good and bad observations with measurable features and resolved outcomes
enter each claim's gate. Middle and unknown observations do not become bad.
**PASS requires every condition** on the new blind primary population:

1. At least 40 good observations and a nonempty bad group.
2. Good +5%-first rate >=60%; bad +5%-first rate <=55%.
3. Good-minus-bad uniform mean >=1.0 percentage point per trade.
4. Positive mean gap after removing the largest summed-return contributor to
   the good bucket (lexical tie break), with both groups still nonempty.
5. Positive gap in at least two of [07:00,09:30), [09:30,11:00), [11:00,16:00) ET.

Insufficient counts or missing instrument/robustness evidence = UNMEASURED.
Adequate evidence violating a performance criterion = FAIL. A positive numeric
proxy gate does not repair missing historical decision-time proof. Report both
plainly. No sensitivity thresholds will be searched in this extension.

### Frozen overlap, costs and pattern treatment

- Cross-table only fully measured A/B/C observations; show all eight good/not-good
  masks and counts good on 0/1/2/3, and unknown-feature counts separately.
- Incremental value: within each other claim's good bucket, compare the target
  claim's good versus bad buckets, with N, mean/SE, matched actual P&L and lost
  winners. Also show the all-good intersection. These are descriptive tests of
  redundancy, not a fourth optimized combination or a new pass gate.
- Cost means **good-only retention**: skip bad AND middle among measurable
  observations. Report both buckets separately, skipped winners, missing-feature
  share, and completed actual-fill dollars. No unknown-feature trading policy
  is presumed. Never infer hypothetical portfolio P&L from these sums.
- At 07:10 or a new watchlist addition, report whether enough bars and a complete
  bot ATR run actually exist. Actual entry price is not known before fill;
  B at that price is not automatically a validated pre-placement rule.
- The qualitative floor-held pattern is not uniquely defined by the card:
  the reference for "floor held within 2%" is unspecified. A clarification is
  requested before joining that pattern to outcomes. Until supplied, report
  UNMEASURED, not a guessed replication. Segment start close, first-bar low,
  segment minimum and entry distance can be retained as raw diagnostics only.
- Keep the four previously proposed candidates unless one duplicates the newly
  expanded exclusion list; none is promoted to a tested finding.

All extension files remain on the same unmerged research branch. Main and
production are untouched; tonight's approved install takes precedence.

## Prior A-only result, retained for audit

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
| **Overhead supply:** permit entry only when less than 20% of the day's prior executed volume lies between the proposed entry and its +5% target. | A large concentration of recent holders just overhead could supply selling before the target; this is volume-at-price mass, not the already-tested price position inside a box, day-high distance or VWAP. | Build the profile from only prints received before each original intent, replay the exact proposed price band, and compare outcomes at matched range/spread AND matched position inside the two-hour range. Freeze 20% before a new sample. | No separation after those controls, inconsistent sign after dropping the leading symbol/day, or results require post-entry prints. |
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
