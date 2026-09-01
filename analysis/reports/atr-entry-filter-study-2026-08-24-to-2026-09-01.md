# ATR Entry Filter And Zero-Floor Reassessment

Study population: the 129 `target+1_floor-2` trades from 2026-08-24 through 2026-09-01.

Research code: `codex/atr-bracket-grid` at `99d8b3b`.

## Decision

Volume, MACD, VWAP, spread, time, scanner metadata, and entry-path features can remove some weak
entries, but they do not support a five-winner/one-loser daily rule in this seven-session sample.
The best honest held-out selector was 19 wins and 19 losses. It cut 129 candidates to 38, but
remained unprofitable at -24.4463 summed trade-return percentage points.

The literal 0% floor result was mechanically correct but incomplete. It exits on the bid as soon as
the bid is below the ask-based entry. That happened on 127/129 entries, including 45/46 trades that
would otherwise hit +1%. Many entries rallied later, but a large fraction first failed the requested
-2% risk boundary.

## Daily Ceiling

"Good" means +1% target before -2% floor. The oracle column uses hindsight and is not tradable; it
shows whether enough winners existed to satisfy a six-trade daily cap.

| Session | Candidates | Winners | Losers | Oracle wins in top 6 | Oracle losses in top 6 |
|---|---:|---:|---:|---:|---:|
| 2026-08-24 | 29 | 5 | 24 | 5 | 1 |
| 2026-08-25 | 16 | 7 | 9 | 6 | 0 |
| 2026-08-26 | 22 | 15 | 7 | 6 | 0 |
| 2026-08-27 | 16 | 5 | 11 | 5 | 1 |
| 2026-08-28 | 2 | 1 | 1 | 1 | 1 |
| 2026-08-31 | 25 | 7 | 18 | 6 | 0 |
| 2026-09-01 | 19 | 6 | 13 | 6 | 0 |
| **Total** | **129** | **46** | **83** | **35** | **3** |

The desired result is present with perfect hindsight on six sessions. It is impossible on 08-28,
where only two trades existed and only one won. The research problem is therefore identification,
not a complete absence of winning candidates.

## Held-Out Selection

For each row below, each session was scored by a logistic model trained on the other six sessions.
At most six trades were selected per session. No future price or outcome was an input.

| Feature family | Selected | Wins | Losses | Win rate | Mean return | Sum return |
|---|---:|---:|---:|---:|---:|---:|
| All features | 38 | 16 | 22 | 42.1% | -0.8727% | -33.1623 pts |
| Volume only | 38 | 17 | 21 | 44.7% | -0.7848% | -29.8224 pts |
| MACD only | 38 | 16 | 22 | 42.1% | -0.8208% | -31.1907 pts |
| VWAP only | 38 | 14 | 24 | 36.8% | -0.9867% | -37.4963 pts |
| Market context | 38 | 17 | 21 | 44.7% | -0.7388% | -28.0761 pts |
| Path only | 38 | 12 | 26 | 31.6% | -1.1895% | -45.1999 pts |
| Volume, max one/symbol | 26 | 13 | 13 | 50.0% | -0.6790% | -17.6536 pts |
| Volume, max two/symbol | 38 | 18 | 20 | 47.4% | -0.7336% | -27.8771 pts |
| Market context, max two/symbol | 38 | 19 | 19 | 50.0% | -0.6433% | -24.4463 pts |

No held-out selector reached the approximately 83.3% win rate required for five winners and one
loser. A +1/-2 bracket also needs more than about 66.7% wins before spread and slippage merely to
break even.

## Volume Cutoffs

Schwab one-minute volume relative to the preceding five bars was the strongest single feature.
These thresholds are descriptive on all seven sessions, not independent validation.

| Minimum volume ratio | Trades left | Wins | Losses | Win rate | Sum return |
|---:|---:|---:|---:|---:|---:|
| 1.5x | 91 | 43 | 48 | 47.3% | -64.940 pts |
| 2.0x | 66 | 32 | 34 | 48.5% | -43.768 pts |
| 2.5x | 52 | 28 | 24 | 53.8% | -26.672 pts |
| 3.0x | 41 | 23 | 18 | 56.1% | -18.093 pts |
| 5.0x | 16 | 9 | 7 | 56.2% | -7.126 pts |
| 10.0x | 4 | 3 | 1 | 75.0% | -1.000 pts |

The `2.5x` threshold answers the request to reduce 129 below half, but 24/52 losses is far from one
bad trade per day. Raising the threshold reduces opportunity faster than it improves precision.

The lowest volume quartile contained only 2 winners and 30 losers. Rejecting that group is the most
credible feature hypothesis found, but its exact cutoff was learned from these same seven sessions
and must be validated on new sessions before use.

## Feature Separation

All measurements are the last fully closed values available before the ATR entry. MACD values are
normalized by price so different symbols are comparable.

| Feature | Winner median | Loser median | Direction / strength |
|---|---:|---:|---|
| Schwab 1m volume / prior 5 | 2.949x | 1.690x | Strongest; higher helped, AUC 0.715 |
| Polygon 30s volume / prior 20 | 1.792x | 1.400x | Weak-to-moderate; AUC 0.591 |
| Entry distance from VWAP | -0.794% | -3.717% | Nearer VWAP helped weakly; AUC 0.543 |
| MACD histogram / price | 0.1515% | 0.2404% | Larger histogram hurt; lower-is-win AUC 0.614 |
| MACD delta / price | 0.0687% | 0.1217% | Strong acceleration hurt; lower-is-win AUC 0.611 |
| Entry spread | 0.508% | 0.535% | Little separation; lower-is-win AUC 0.531 |

The data does not support "more MACD increase is always better." The losing entries had larger
MACD delta and histogram, consistent with entering after the move was already extended. VWAP alone
was not useful; its held-out selector performed close to the unfiltered population.

An in-sample hypothesis, `Polygon volume >= 1.5x` and normalized `MACD delta <= 0.10`, found 15
winners in 21 trades and +2.2452 summed points. It was unstable by day and fell to 10 wins / 10
losses in held-out rule selection. It is a hypothesis for new data, not an approved filter.

## Zero-Floor Audit

| Observation | Count |
|---|---:|
| First executable bid below entry | 127 / 129 |
| Later reached +1% before 16:00 | 112 / 129 |
| Later reached +2% before 16:00 | 106 / 129 |
| Later reached +5% before 16:00 | 88 / 129 |
| -2% floor losers that later reached +1% | 66 / 83 |
| -2% floor losers that later reached +2% | 62 / 83 |
| -2% floor losers that later reached +5% | 49 / 83 |

Your observation of 5% runners is confirmed. It does not make an immediate 0% floor viable: that
floor exits through the spread before the rally. The 49 stopped losers that later ran +5% first had
a median maximum drawdown of -4.5249% and needed a median 2,444.5 seconds (40.7 minutes) to reach
+5%. They are later reversals, not +1/-2 winners hidden by the report.

A different and testable meaning of "zero floor" is a break-even floor that remains inactive at
entry, arms only after the trade first reaches a stated profit trigger, then protects entry while
seeking a higher target. For example: arm 0% after +1%, then seek +2%. That is not the same policy as
the immediate static 0% floor tested here and should be graded as a separate bracket.

## Next Research Gate

No live guard should be built from these seven sessions. The evidence supports the following next
test, still research-only:

1. Keep the current ATR first/reclaim signal unchanged.
2. Cap at one entry per symbol and six entries per day.
3. Reject only the lowest pre-entry Schwab-volume regime, with the cutoff frozen before new data.
4. Test a delayed confirmation separately: strong volume, MACD acceleration cooling rather than
   expanding, and price near VWAP.
5. Test `+2% target / 0% break-even armed after +1%` as a separate exit policy.
6. Require genuinely forward sessions. Do not tune the thresholds again on these seven days.

## Validation

- Feature coverage was 129/129 for every modeled field.
- Polygon indicators used only fully closed 30-second bars before entry.
- Schwab volume used only bars already available to the live ATR service.
- Future quote fields were excluded from every selector.
- Every held-out day was scored by a model trained without that day.
- 102 related replay/backtest tests passed.
- No live strategy, service, configuration, or deployment behavior changed.

Artifacts:

- `atr-entry-filter-study-2026-08-24-to-2026-09-01-candidates.csv`
- `atr-entry-filter-study-2026-08-24-to-2026-09-01-selected.csv`
- `atr-entry-filter-study-2026-08-24-to-2026-09-01.json`
