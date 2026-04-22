# Reclaim Bad Pattern Analysis

## Counts
- taken_good: 41
- taken_bad: 57
- taken_open: 21

## Strongest Separators
- vwap_extension_ge_0.02: good=0.3902, bad=0.386
- vwap_extension_ge_0.03: good=0.3415, bad=0.3684
- ema9_extension_ge_0.01: good=0.1463, bad=0.2456
- ema9_extension_ge_0.02: good=0.0488, bad=0.1228
- pullback_volume_ratio_lt_1.0: good=0.5366, bad=0.6316
- upper_wick_pct_ge_0.25: good=0.439, bad=0.3158
- close_pos_pct_lt_0.5: good=0.4634, bad=0.5088

## Summary Stats
- ema9_extension_pct: good_mean=0.0004, bad_mean=0.0039, good_median=0.0024, bad_median=-0.0005
- vwap_extension_pct: good_mean=0.0084, bad_mean=0.0251, good_median=-0.0074, bad_median=0.0005
- pullback_volume_ratio: good_mean=1.4844, bad_mean=0.9677, good_median=0.862, bad_median=0.74
- current_rel_vol: good_mean=1.4308, bad_mean=1.3096, good_median=0.7823, bad_median=0.7674
- upper_wick_pct: good_mean=0.2707, bad_mean=0.2018, good_median=0.2, bad_median=0.1429
- close_pos_pct: good_mean=0.5281, bad_mean=0.4828, good_median=0.5263, bad_median=0.5
- body_pct: good_mean=0.4075, bad_mean=0.4671, good_median=0.3086, bad_median=0.4434

## Worst Bad Clusters
- JEM: 16
- CYCN: 7
- BFRG: 5
- UCAR: 5
- BBGI: 5
- AGPU: 4
- HUBC: 4
- ELAB: 3
- SKYQ: 2
- TMDE: 2

## Worst Bad Days
- 2026-04-08: 32
- 2026-04-01: 19
- 2026-04-02: 6