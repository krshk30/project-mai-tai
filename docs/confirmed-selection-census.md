# Confirmed-stock ATR selection census

Status: **PRE-REGISTERED; population results not yet read.** This is a read-only research
instrument. It changes no scanner, strategy, order, exit, or deployment path.

## Question

Can information already visible before a canonical ATR BUY flip distinguish exhausted confirmed
stocks from names still capable of reaching +5% before the next ATR SELL?

SUGP and MYSZ on 2026-09-15 are illustrations only. Their four fills cannot select a feature,
threshold, or direction.

## Population and clocks

- A confirmed symbol-day must contain at least one `scanner_confirmed_events.event_type='CONFIRM'`.
  A FADE-only or RETENTION_DROP-only row is not a confirmed day.
- Repeated CONFIRM snapshots while membership is already active collapse into one membership.
- Canonical ATR BUY flips come from `backtest.atr_oracle.compute_atr_trail`, `sma5`, period 5,
  factor 3.5, over stored live Schwab one-minute bars. Eligible flip bars begin at 07:00 ET and end
  before 16:00 ET.
- Outcome starts with the bar after the BUY-flip bar. It is `reached +5%` when a later bar high is
  at least 105% of the flip close before and including the next SELL-flip bar; otherwise it is a
  miss. The window ends at 16:00 ET when no SELL arrives. MFE and MAE use the same window.
- The exact scanner feed begins 2026-07-09. Massive pre-07:00 trades begin 2026-09-01. Before that
  date, session-high features declare a 07:00 anchor. From that date forward, Massive trade-built
  bars provide 04:00-06:59 and Schwab bars provide 07:00 onward. Missing inputs are `UNKNOWN`.
- All displayed times are ET. No dollars are reported.

## Frozen features

Every feature reads bars strictly before the BUY-flip bar:

1. Minutes since the latest occurrence of the session high.
2. Percentage from the session high to the last pre-flip close.
3. Consecutive completed 30-minute blocks whose high is below the preceding block high. Missing
   blocks make the value unknown rather than silently joining non-adjacent blocks.
4. Kaufman efficiency over the latest 60 observed closes. The shared implementation is extracted
   from `scripts/orb_operator_filter_census.py`; fewer than 60 bars is unknown.
5. High-low range over the preceding 120 clock minutes, plus the count of non-overlapping 5%
   close-to-close legs. Each completed leg resets its anchor.
6. Volume trend: last-30-minute volume divided by half of prior-60-minute volume. This compares
   per-minute rates; a zero prior denominator is unknown.
7. Change from the membership-opening confirmation price to the last pre-flip close, and the
   scanner's `change_pct` at that confirmation. A flip preceding its first confirmation is unknown.

The MYSZ/SUGP line, high age >90 minutes **and** >5% below the high, is one frozen candidate. It is
not the hypothesis and cannot be tuned after the run.

## Analysis rules

- Each numeric feature is divided into tie-preserving equal-frequency deciles. Equal values never
  split across buckets. Every bucket prints hits, flips, and hit rate.
- A feature is retained as descriptive evidence only when decile hit rates are monotonic in either
  direction. No threshold is selected from the decile output.
- Each feature gradient is re-bucketed after dropping every one name and every one day. It is
  retained only when the same non-flat monotonic direction survives both checks. The report also
  names the day contributing the most measured flips, with its denominator.
- Drop-one-by-name and drop-one-by-day tests separately prevent one runner or one abnormal session
  from carrying the pre-registered candidate.
- The live-entry subset is a separate labelled table and never drives thresholds. A logical live
  entry matches only when its first fill falls inside the canonical flip minute. Intrabar fills
  without a close-confirmed BUY remain explicitly unmatched.

## Pre-registered pass criterion

The candidate passes only if all four conditions hold:

1. The blocked bucket contains at least 60 outcome-measured BUY flips.
2. The kept bucket contains at least 60 outcome-measured BUY flips.
3. The blocked +5% hit rate is strictly less than half the kept hit rate.
4. Conditions 1-3 remain true after dropping every one symbol and after dropping every one day.

The report prints `PASS` or `FAIL`; narrative cannot override it.

## Production facts, not changes

The scanner currently removes a confirmed name once live day change falls below +30%. Feed-retention
evaluation returns early and resets a name active while it remains scanner-confirmed. This study
records those facts and changes neither.

## Commands

Run after 16:00 ET, PRE07 first:

```bash
python -m project_mai_tai.backtest.pre0700_shadow \
  --range 2026-09-01 2026-09-15 \
  --json analysis/reports/pre0700-shadow-2026-09-01-to-2026-09-15.json

python -m project_mai_tai.backtest.confirmed_selection_census \
  --range 2026-07-09 2026-09-15 \
  --json analysis/reports/confirmed-selection-census-2026-07-09-to-2026-09-15.json \
  --markdown analysis/reports/confirmed-selection-census-2026-07-09-to-2026-09-15.md
```
