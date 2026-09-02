# ATR Early-State Combination Study: 2026-08-24 to 2026-09-01

Locked population: **97 entries**. Build (Aug 24-28): 28 reached +5, 39 did not. Untouched holdout (Aug 31 and Sep 1): 14 reached +5, 16 did not.

Each checkpoint uses the executable bid path from the ask fill and the exact Schwab strategy bar closing at that signal-relative minute. Indicators use the stored series: 12/26/9 MACD histogram, TOS FastStochastic(10), Wilder RSI(14), exact three-row dot consensus, live replay ATR state, 04:00-anchored typical-price VWAP, and current volume divided by the live 20-bar average (including the current bar). No missing state is imputed.

## Coverage

| Checkpoint | Complete rows | Rows retained |
|---:|---:|---:|
| 0 min | 97/97 | 97/97 |
| 3 min | 92/97 | 97/97 |
| 5 min | 90/97 | 97/97 |
| 10 min | 88/97 | 97/97 |

## Build-Selected Pairs and Triples

The table shows the five highest-ranked build-day rules at each checkpoint. The fail side is the AND of its conditions. Rules needed at least 85% coverage in each build population and the build-day proportional equivalent of keeping 30/42 winners while removing 25/55 losers. Ranking did not inspect holdout results.

| Min | Conditions (fail side) | Build: W kept / L removed | Holdout: W kept / L removed | Unavailable B/H | Pooled 30/25 | Holdout |
|---:|---|---:|---:|---:|:---:|---|
| 0 | price_vs_entry_pct >= -3 AND volume_ratio_20 <= +2 | 22/28 / 21/39 | 8/14 / 3/16 | 0 / 0 | No | reversed/failed |
| 0 | price_vs_entry_pct <= +0 AND volume_ratio_20 <= +2 | 21/28 / 22/39 | 8/14 / 3/16 | 0 / 0 | No | reversed/failed |
| 0 | price_vs_entry_pct >= -1 AND volume_ratio_20 <= +2 | 22/28 / 20/39 | 8/14 / 3/16 | 0 / 0 | No | reversed/failed |
| 0 | price_vs_entry_pct >= -3 AND volume_ratio_20 <= +2 AND rsi >= +50 | 22/28 / 20/39 | 8/14 / 3/16 | 0 / 0 | No | reversed/failed |
| 0 | price_vs_entry_pct >= -3 AND volume_ratio_20 <= +2 AND last_bar_direction is up | 22/28 / 20/39 | 8/14 / 3/16 | 0 / 0 | No | reversed/failed |
| 3 | volume_ratio_20 >= +1 AND macd_histogram_direction is falling | 24/28 / 18/39 | 9/14 / 10/16 | 5 / 0 | Yes | survived |
| 3 | price_vs_entry_pct <= +2 AND max_down_so_far_pct >= -5 AND volume_ratio_20 >= +1 | 21/28 / 22/39 | 6/14 / 9/16 | 5 / 0 | No | reversed/failed |
| 3 | max_up_so_far_pct <= +4 AND max_down_so_far_pct >= -5 AND volume_ratio_20 >= +1 | 21/28 / 22/39 | 7/14 / 9/16 | 5 / 0 | No | survived |
| 3 | price_vs_entry_pct <= +2 AND max_up_so_far_pct <= +4 AND volume_ratio_20 >= +1 | 20/28 / 23/39 | 7/14 / 10/16 | 5 / 0 | No | survived |
| 3 | price_vs_entry_pct <= +0 AND max_up_so_far_pct <= +1 AND volume_ratio_20 >= +0.5 | 22/28 / 20/39 | 6/14 / 6/16 | 5 / 0 | No | reversed/failed |
| 5 | max_up_so_far_pct <= +4 AND dot_consensus <= +2 | 20/28 / 28/39 | 7/14 / 11/16 | 5 / 2 | No | survived |
| 5 | price_vs_entry_pct <= +0 AND max_up_so_far_pct <= +4 AND dot_consensus <= +2 | 20/28 / 27/39 | 7/14 / 11/16 | 5 / 2 | No | survived |
| 5 | max_up_so_far_pct <= +4 AND dot_consensus <= +2 AND price_vs_vwap_pct <= +2 | 21/28 / 25/39 | 7/14 / 9/16 | 5 / 2 | No | survived |
| 5 | max_up_so_far_pct <= +4 AND dot_consensus <= +2 AND atr_direction is long | 20/28 / 26/39 | 7/14 / 10/16 | 5 / 2 | No | survived |
| 5 | max_up_so_far_pct <= +4 AND volume_ratio_20 <= +1.5 AND dot_consensus <= +2 | 21/28 / 24/39 | 7/14 / 10/16 | 5 / 2 | No | survived |
| 10 | price_vs_entry_pct <= -1 AND max_up_so_far_pct <= +4 AND volume_ratio_20 <= +2 | 26/28 / 22/39 | 10/14 / 9/16 | 7 / 2 | Yes | survived |
| 10 | price_vs_entry_pct <= -1 AND max_up_so_far_pct <= +4 | 25/28 / 23/39 | 10/14 / 10/16 | 6 / 2 | Yes | survived |
| 10 | price_vs_entry_pct <= -1 AND max_up_so_far_pct <= +4 AND rsi >= +30 | 25/28 / 23/39 | 10/14 / 10/16 | 7 / 2 | Yes | survived |
| 10 | max_up_so_far_pct <= +4 AND volume_ratio_20 <= +2 AND stochastic <= +50 | 24/28 / 24/39 | 10/14 / 7/16 | 7 / 2 | Yes | survived |
| 10 | price_vs_entry_pct <= -1 AND max_up_so_far_pct <= +3 AND volume_ratio_20 <= +2 | 26/28 / 21/39 | 10/14 / 9/16 | 7 / 2 | Yes | survived |

A combination is called a survivor only when the unchanged pooled result keeps at least 30 winners, removes at least 25 losers, and its holdout separation remains positive. **Survivors among the preselected rules: 6.**

## The 55 Trades Below +5%

`After-peak fall` is the executable-bid drawdown from the trade's highest bid to the lowest later bid before ATR SELL/session close.

| Maximum-up bucket | Build | Holdout | All |
|---|---:|---:|---:|
| below +1 | 19 | 5 | 24 |
| +1 to +2 | 4 | 5 | 9 |
| +2 to +3 | 6 | 5 | 11 |
| +3 to +4 | 5 | 1 | 6 |
| +4 to +5 | 5 | 0 | 5 |

| Threshold touched by the 55 | Build | Holdout | All |
|---|---:|---:|---:|
| +1% | 20/39 | 11/16 | 31/55 |
| +2% | 16/39 | 6/16 | 22/55 |
| +3% | 10/39 | 1/16 | 11/55 |
| +4% | 5/39 | 0/16 | 5/55 |

## Full Exit at +2%, +3%, or +4% With -8% Stop

A target fills at the target price on first executable-bid touch. A -8% stop triggers on a bid and fills at the next captured bid, matching the prior study's live-style stop model. If neither occurs, the locked ATR SELL/session-close exit remains.

| Target | Split | Reached | Stops | Total return | Mean | Ran farther | Available upside capped |
|---:|---|---:|---:|---:|---:|---:|---:|
| +2% | build | 43/67 | 9 | -32.39 pts | -0.48% | 44 | 379.77 pts |
| +2% | holdout | 20/30 | 3 | -11.75 pts | -0.39% | 20 | 192.86 pts |
| +2% | all | 63/97 | 12 | -44.15 pts | -0.46% | 64 | 572.63 pts |
| +3% | build | 37/67 | 12 | -42.08 pts | -0.63% | 38 | 339.66 pts |
| +3% | holdout | 15/30 | 5 | -30.08 pts | -1.00% | 15 | 174.90 pts |
| +3% | all | 52/97 | 17 | -72.16 pts | -0.74% | 53 | 514.55 pts |
| +4% | build | 32/67 | 14 | -52.47 pts | -0.78% | 33 | 304.28 pts |
| +4% | holdout | 14/30 | 5 | -19.54 pts | -0.65% | 14 | 159.95 pts |
| +4% | all | 46/97 | 19 | -72.01 pts | -0.74% | 47 | 464.22 pts |

Largest runner: BTCT on 2026-08-24 had +68.10% available before its ATR-segment exit. Available upside above each fixed target was capped by +2% target: 66.10 points, +3% target: 65.10 points, +4% target: 64.10 points.

### Per-day target totals

| Date | +2% reached / total return | +3% reached / total return | +4% reached / total return |
|---|---:|---:|---:|
| 2026-08-24 | 12/25 / -36.66 pts | 11/25 / -28.58 pts | 10/25 / -25.46 pts |
| 2026-08-25 | 8/12 / -10.73 pts | 7/12 / -13.73 pts | 7/12 / -6.73 pts |
| 2026-08-26 | 15/18 / +15.21 pts | 11/18 / -7.56 pts | 9/18 / -16.70 pts |
| 2026-08-27 | 7/11 / -2.21 pts | 7/11 / +4.79 pts | 5/11 / -7.58 pts |
| 2026-08-28 | 1/1 / +2.00 pts | 1/1 / +3.00 pts | 1/1 / +4.00 pts |
| 2026-08-31 | 9/16 / -20.37 pts | 5/16 / -44.07 pts | 5/16 / -39.07 pts |
| 2026-09-01 | 11/14 / +8.62 pts | 10/14 / +13.99 pts | 9/14 / +19.53 pts |

The upside-cost column is `natural ATR-segment max-up minus target`, summed only for trades that ran beyond the target. It measures available upside capped, not a claim that the full maximum was executable as an exit.

Exact 388 checkpoint rows, all 97 plain-language trade records, all 55 loser paths and touch times, the complete qualifying combination census, and every target outcome are in the companion CSV files.
