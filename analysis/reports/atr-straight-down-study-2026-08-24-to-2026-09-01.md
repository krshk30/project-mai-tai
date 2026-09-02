# ATR Straight-Down First-Five-Bar Study

Locked target: **24/97 entries that never touched +1%** (19 build, 5 holdout). Comparator: the other 73 entries. The 31 sub-+5 trades that did touch +1% are not mixed into the target; they remain part of the explicitly requested 73-entry comparator.

Bars are the first five Schwab strategy bars after the ATR BUY fill. `Traded above` uses captured trade prints strictly after the executable fill, avoiding pre-fill seconds inside bar 1. Closes, bar direction, volume, indicators, VWAP and ATR trail use the exact Schwab strategy series. Volume ratio is current volume divided by the live 20-bar average including the current bar. Missing bars or prints are unavailable, never inferred.

## Coverage

| Bar | Strategy bar | Post-fill prints | Complete state |
|---:|---:|---:|---:|
| 1 | 96/97 | 97/97 | 96/97 |
| 2 | 93/97 | 95/97 | 93/97 |
| 3 | 92/97 | 93/97 | 92/97 |
| 4 | 91/97 | 91/97 | 91/97 |
| 5 | 90/97 | 90/97 | 90/97 |

## Operator Hypothesis

| Through bar | Build: target caught / 19 | Build: comparator touched / 48 | Holdout: target caught / 5 | Holdout: comparator touched / 25 | All: target / comparator | Unavailable |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 17/19 | 14/48 | 4/5 | 11/25 | 21/24 / 25/73 | 2 |
| 3 | 15/19 | 12/48 | 3/5 | 10/25 | 18/24 / 22/73 | 4 |
| 4 | 15/19 | 10/48 | 3/5 | 8/25 | 18/24 / 18/73 | 6 |
| 5 | 14/19 | 8/48 | 3/5 | 7/25 | 17/24 / 15/73 | 7 |

The hypothesis condition is: from bar 2 through the stated horizon, no captured post-fill trade print exceeded the ask fill.

## Build-Selected Pairs and Triples

Only build-day rules that caught the proportional equivalent of 15/24 targets while touching fewer than the proportional equivalent of 10/73 comparators were eligible. Holdout results were read only after ranking. Missing inputs remain on the not-caught side.

| By bar | Conditions | Build target / comparator | Holdout target / comparator | Pooled target / comparator | Meets 15/<10 | Holdout separates |
|---:|---|---:|---:|---:|:---:|:---:|
| 1 | b1.close_vs_entry_pct <= +0 AND b1.volume_ratio_20 >= +0.5 AND b1.price_vs_vwap_pct >= -2 | 14/19 / 5/48 | 2/5 / 5/25 | 16/24 / 10/73 | No | Yes |
| 1 | b1.close_vs_entry_pct <= +0 AND b1.running_low_pct >= -5 AND b1.price_vs_vwap_pct >= -2 | 13/19 / 5/48 | 2/5 / 5/25 | 15/24 / 10/73 | No | Yes |
| 1 | b1.volume_ratio_20 >= +0.5 AND b1.price_vs_vwap_pct >= -2 AND b1.traded_above_entry is False | 12/19 / 2/48 | 2/5 / 0/25 | 14/24 / 2/73 | No | Yes |
| 1 | b1.price_vs_vwap_pct >= -2 AND b1.traded_above_entry is False | 12/19 / 3/48 | 2/5 / 0/25 | 14/24 / 3/73 | No | Yes |
| 1 | b1.close_vs_entry_pct <= +0 AND b1.volume_ratio_20 >= +0.5 AND b1.price_vs_vwap_pct >= -1 | 12/19 / 4/48 | 2/5 / 4/25 | 14/24 / 8/73 | No | Yes |
| 2 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False | 14/19 / 3/48 | 2/5 / 3/25 | 16/24 / 6/73 | Yes | Yes |
| 2 | b1.rsi <= +70 AND b1.price_vs_vwap_pct >= -2 AND b2.close_vs_entry_pct <= +1 | 14/19 / 5/48 | 2/5 / 5/25 | 16/24 / 10/73 | No | Yes |
| 2 | b1.close_vs_entry_pct >= -3 AND b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False | 13/19 / 2/48 | 2/5 / 2/25 | 15/24 / 4/73 | Yes | Yes |
| 2 | b1.rsi >= +50 AND b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False | 13/19 / 3/48 | 2/5 / 3/25 | 15/24 / 6/73 | Yes | Yes |
| 2 | b1.price_vs_vwap_pct >= -2 AND b2.close_vs_entry_pct <= +0 AND b2.traded_above_entry is False | 13/19 / 3/48 | 2/5 / 3/25 | 15/24 / 6/73 | Yes | Yes |
| 3 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False AND b3.traded_above_entry is False | 13/19 / 3/48 | 1/5 / 3/25 | 14/24 / 6/73 | No | Yes |
| 3 | b1.close_vs_entry_pct <= +0 AND b1.price_vs_vwap_pct >= -2 AND b3.traded_above_entry is False | 13/19 / 4/48 | 1/5 / 3/25 | 14/24 / 7/73 | No | Yes |
| 3 | b1.price_vs_vwap_pct >= -2 AND b3.traded_above_entry is False | 13/19 / 5/48 | 1/5 / 4/25 | 14/24 / 9/73 | No | Yes |
| 3 | b1.price_vs_vwap_pct >= -1 AND b2.close_vs_entry_pct <= +1 AND b3.atr_stop_position is below | 13/19 / 5/48 | 2/5 / 5/25 | 15/24 / 10/73 | No | Yes |
| 3 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False AND b3.running_low_pct >= -5 | 12/19 / 2/48 | 2/5 / 1/25 | 14/24 / 3/73 | No | Yes |
| 4 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False AND b4.close_vs_entry_pct <= +0 | 13/19 / 3/48 | 2/5 / 3/25 | 15/24 / 6/73 | Yes | Yes |
| 4 | b1.close_vs_entry_pct <= +0 AND b1.price_vs_vwap_pct >= -2 AND b4.traded_above_entry is False | 13/19 / 4/48 | 2/5 / 2/25 | 15/24 / 6/73 | Yes | Yes |
| 4 | b1.close_vs_entry_pct <= +1 AND b1.price_vs_vwap_pct >= -1 AND b4.close_vs_entry_pct <= +1 | 13/19 / 4/48 | 2/5 / 4/25 | 15/24 / 8/73 | Yes | Yes |
| 4 | b1.price_vs_vwap_pct >= -1 AND b2.close_vs_entry_pct <= +1 AND b4.close_vs_entry_pct <= +1 | 13/19 / 4/48 | 2/5 / 4/25 | 15/24 / 8/73 | Yes | Yes |
| 4 | b1.close_vs_entry_pct <= +0 AND b1.price_vs_vwap_pct >= -2 AND b4.close_vs_entry_pct <= +0 | 13/19 / 5/48 | 2/5 / 4/25 | 15/24 / 9/73 | Yes | Yes |
| 5 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False AND b5.running_low_pct <= -1 | 12/19 / 3/48 | 2/5 / 3/25 | 14/24 / 6/73 | No | Yes |
| 5 | b1.price_vs_vwap_pct >= -2 AND b2.traded_above_entry is False AND b5.atr_stop_position is below | 12/19 / 3/48 | 2/5 / 3/25 | 14/24 / 6/73 | No | Yes |
| 5 | b1.close_vs_entry_pct <= +0 AND b1.price_vs_vwap_pct >= -2 AND b5.running_low_pct <= -1 | 12/19 / 5/48 | 2/5 / 5/25 | 14/24 / 10/73 | No | Yes |
| 5 | b1.close_vs_entry_pct <= +0 AND b1.price_vs_vwap_pct >= -2 AND b5.atr_stop_position is below | 12/19 / 5/48 | 2/5 / 5/25 | 14/24 / 10/73 | No | Yes |
| 5 | b1.close_vs_entry_pct <= +1 AND b1.price_vs_vwap_pct >= -2 AND b5.macd_histogram_direction is falling | 12/19 / 5/48 | 2/5 / 3/25 | 14/24 / 8/73 | No | Yes |

Conditions visible by bar 3 meeting the stated pooled count and preserving holdout separation: **4**.

Exact 485 bar states, hypothesis counts, all build-qualified combinations, and the build-ranked rules with unchanged holdout results are in the companion CSV files. This is measurement only; no entry or exit rule was changed.
