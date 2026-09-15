# Confirmed-stock ATR selection census

Status: **RESULT CAPTURED after the 2026-09-15 close.** This is a read-only research instrument.
It changes no scanner, strategy, order, exit, or deployment path.

## Result

The pre-registered candidate **FAILED**. Across 683 outcome-measured BUY flips, the proposed
blocked group reached +5% on 185/440 (42.05%) and the kept group on 104/243 (42.80%). The rates
are effectively the same, not the required two-to-one separation, and the result fails both
drop-one-name and drop-one-day robustness. All nine frozen feature gradients were non-monotonic;
none was retained.

The final population contains 855 confirmed symbol-days. The earlier 846/640 checkpoint was
taken before the last nine 2026-09-15 confirmations arrived. After close, 649/855 symbol-days
have a live Schwab bar somewhere that day, exactly nine above the earlier 640. Of those, 506/855
have a bar inside the 04:00-15:59 ET analysis window. Keeping both denominators prevents
after-hours-only bars from being mislabeled as usable feature history.

The real 2026-09-15 tape also contradicts a simple fade filter inside MYSZ itself: its 09:34 BUY
was 26.89% below a 327-minute-old high and missed +5%, but its 11:22 BUY was still 31.11% below a
435-minute-old high and reached +5% with 12.31% MFE. SUGP's canonical 09:44 BUY was a miss from
the flip-bar close, while the actual Webull rest filled one minute earlier and won; that is why
the live-fill table remains descriptive and cannot select a threshold.

PRE07 remains an instrument limitation rather than a trading conclusion: 7/16 symbol-days were
instrument-valid, only 1/16 was `FIDELITY_OK`, and 9/16 were `INSTRUMENT_MISMATCH`. The current
MYSZ and BDRX 07:08 canonical readings agreed with the live probe, but the ten-session shadow
sample is not strong enough to infer blind-window outcomes.

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
   per-minute rates; a zero prior denominator is unknown. A 90-minute window containing both
   pre-07:00 Massive trade sizes and post-07:00 Schwab CHART volume is also unknown because those
   feeds do not share a proven volume unit.
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
  entry matches when its first fill falls in the canonical flip minute or the immediately following
  minute, covering an intrabar resting trigger and its next-minute fill. Other fills remain
  explicitly unmatched.

## Pre-registered pass criterion

The candidate passes only if all four conditions hold:

1. The blocked bucket contains at least 60 outcome-measured BUY flips.
2. The kept bucket contains at least 60 outcome-measured BUY flips.
3. The blocked +5% hit rate is strictly less than half the kept hit rate.
4. Conditions 1-3 remain true after dropping every one symbol and after dropping every one day.

The report prints `PASS` or `FAIL`; narrative cannot override it.

## Interpretation caveats

- Outcome entry is the BUY-flip bar close. The live rest enters at the ATR trail below that close,
  so +5% from the close is stricter than the live entry geometry.
- Integer features can tie into only a few buckets. The report prints the actual bucket count next
  to every monotonicity result; a ten-decile label is never implied when fewer buckets exist.
- The canonical oracle has no bar-gap guard. The study measures the stored Schwab series and does
  not convert a missing bar into proof of continuity.

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
