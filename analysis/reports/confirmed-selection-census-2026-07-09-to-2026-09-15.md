# Confirmed-stock ATR selection census

Range: 2026-07-09 through 2026-09-15, ET. Read-only; no scanner or strategy changes.

## Population

| Measure | Result | Denominator |
|---|---:|---|
| Confirmed symbol-days | 855 | distinct days/names with a CONFIRM row |
| Any live Schwab bar that day | 649/855 | confirmed symbol-days |
| Analysis-window bar-measurable | 506/855 | confirmed symbol-days with a 04:00-15:59 ET bar |
| No live Schwab bars | 206/855 | confirmed symbol-days |
| No 04:00-15:59 ET bar | 349/855 | confirmed symbol-days |
| Confirmation memberships | 2020 | repeated snapshots collapsed |
| PATH_C_EXTREME_MOVER | 2005/2020 | confirmation memberships |
| Pre-07:00 features measurable | 75/506 | bar-measurable symbol-days |
| Canonical BUY flips | 689 | 506 bar-measurable symbol-days |
| Outcome measured | 683/689 | canonical BUY flips |
| Reached +5% | 289/683 (42.31%) | measured outcomes |

Pre-09-01 session-high features begin at 07:00 ET. From 09-01 onward they include Massive trade-built bars from 04:00-06:59 ET. Missing features remain UNKNOWN, never zero.

## Pre-registered candidate

Block only when high age >90 minutes AND the last pre-flip close is >5% below that high. PASS requires blocked hit rate < half kept hit rate, at least 60 flips in each group, and survival after dropping every one name and every one day.

| Result | Blocked | Kept | Count floor | Separation | Drop-one name | Drop-one day | Verdict |
|---|---:|---:|---|---|---|---|---|
| +5 hit rate | 185/440 (42.05%) | 104/243 (42.80%) | True | False | False | False | FAIL |
| Eligibility | 683/683 | candidate-measurable / measured outcomes | - | - | - | - | - |
| Largest blocked day | 2026-08-12: 31/440 | blocked flips | - | - | - | - | - |

## Feature coverage

| Feature | Measured | UNKNOWN | Gradient / buckets | Drop-one name | Drop-one day | Largest day | Retained |
|---|---:|---:|---|---|---|---:|---|
| minutes_since_high | 683/683 | 0/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/683 | False |
| below_session_high_pct | 683/683 | 0/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/683 | False |
| lower_high_streak_30m | 620/683 | 63/683 | non_monotonic / 5 | False | False | 2026-08-12: 39/620 | False |
| kaufman_efficiency_60 | 631/683 | 52/683 | non_monotonic / 10 | False | False | 2026-08-12: 37/631 | False |
| prior_120m_range_pct | 679/683 | 4/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/679 | False |
| prior_120m_swing_count | 679/683 | 4/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/679 | False |
| volume_trend_30_over_60 | 661/683 | 22/683 | non_monotonic / 10 | False | False | 2026-08-12: 40/661 | False |
| change_since_confirm_pct | 680/683 | 3/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/680 | False |
| confirm_change_pct | 680/683 | 3/683 | non_monotonic / 10 | False | False | 2026-08-12: 41/680 | False |

## Deciles

| Feature | Decile | Value range | Hits | Hit rate |
|---|---:|---:|---:|---:|
| minutes_since_high | 1 | 1.0000 to 40.0000 | 28/68 | 41.18% |
| minutes_since_high | 2 | 41.0000 to 61.0000 | 31/70 | 44.29% |
| minutes_since_high | 3 | 62.0000 to 84.0000 | 30/67 | 44.78% |
| minutes_since_high | 4 | 85.0000 to 105.0000 | 38/70 | 54.29% |
| minutes_since_high | 5 | 106.0000 to 137.0000 | 28/67 | 41.79% |
| minutes_since_high | 6 | 138.0000 to 170.0000 | 38/68 | 55.88% |
| minutes_since_high | 7 | 171.0000 to 216.0000 | 23/68 | 33.82% |
| minutes_since_high | 8 | 217.0000 to 271.0000 | 28/70 | 40.00% |
| minutes_since_high | 9 | 272.0000 to 347.0000 | 19/67 | 28.36% |
| minutes_since_high | 10 | 348.0000 to 622.0000 | 26/68 | 38.24% |
| below_session_high_pct | 1 | 0.0000 to 5.7576 | 15/69 | 21.74% |
| below_session_high_pct | 2 | 5.7592 to 9.7289 | 22/68 | 32.35% |
| below_session_high_pct | 3 | 9.7298 to 13.1626 | 27/68 | 39.71% |
| below_session_high_pct | 4 | 13.2353 to 16.9909 | 29/69 | 42.03% |
| below_session_high_pct | 5 | 17.0370 to 20.3738 | 29/68 | 42.65% |
| below_session_high_pct | 6 | 20.4093 to 24.6667 | 29/68 | 42.65% |
| below_session_high_pct | 7 | 24.7093 to 29.0937 | 36/69 | 52.17% |
| below_session_high_pct | 8 | 29.0993 to 35.8073 | 36/68 | 52.94% |
| below_session_high_pct | 9 | 35.9524 to 45.1987 | 31/68 | 45.59% |
| below_session_high_pct | 10 | 45.2113 to 81.2325 | 35/68 | 51.47% |
| lower_high_streak_30m | 2 | 0.0000 to 0.0000 | 77/198 | 38.89% |
| lower_high_streak_30m | 5 | 1.0000 to 1.0000 | 89/205 | 43.41% |
| lower_high_streak_30m | 8 | 2.0000 to 2.0000 | 58/110 | 52.73% |
| lower_high_streak_30m | 9 | 3.0000 to 3.0000 | 24/56 | 42.86% |
| lower_high_streak_30m | 10 | 4.0000 to 8.0000 | 16/51 | 31.37% |
| kaufman_efficiency_60 | 1 | 0.0000 to 0.0147 | 26/64 | 40.62% |
| kaufman_efficiency_60 | 2 | 0.0154 to 0.0327 | 25/63 | 39.68% |
| kaufman_efficiency_60 | 3 | 0.0331 to 0.0481 | 27/63 | 42.86% |
| kaufman_efficiency_60 | 4 | 0.0484 to 0.0643 | 21/63 | 33.33% |
| kaufman_efficiency_60 | 5 | 0.0644 to 0.0832 | 23/63 | 36.51% |
| kaufman_efficiency_60 | 6 | 0.0834 to 0.1012 | 29/63 | 46.03% |
| kaufman_efficiency_60 | 7 | 0.1013 to 0.1267 | 30/63 | 47.62% |
| kaufman_efficiency_60 | 8 | 0.1268 to 0.1544 | 31/63 | 49.21% |
| kaufman_efficiency_60 | 9 | 0.1549 to 0.2049 | 25/63 | 39.68% |
| kaufman_efficiency_60 | 10 | 0.2073 to 0.5535 | 30/63 | 47.62% |
| prior_120m_range_pct | 1 | 1.6878 to 7.1835 | 14/68 | 20.59% |
| prior_120m_range_pct | 2 | 7.1942 to 10.3751 | 16/68 | 23.53% |
| prior_120m_range_pct | 3 | 10.4478 to 13.5468 | 27/68 | 39.71% |
| prior_120m_range_pct | 4 | 13.6395 to 17.6822 | 25/68 | 36.76% |
| prior_120m_range_pct | 5 | 17.6846 to 21.3974 | 34/68 | 50.00% |
| prior_120m_range_pct | 6 | 21.4619 to 27.4286 | 26/68 | 38.24% |
| prior_120m_range_pct | 7 | 27.4510 to 32.4415 | 30/68 | 44.12% |
| prior_120m_range_pct | 8 | 32.7586 to 41.9056 | 37/68 | 54.41% |
| prior_120m_range_pct | 9 | 42.1238 to 60.1308 | 36/68 | 52.94% |
| prior_120m_range_pct | 10 | 60.1449 to 392.7438 | 43/67 | 64.18% |
| prior_120m_swing_count | 1 | 0.0000 to 0.0000 | 19/84 | 22.62% |
| prior_120m_swing_count | 2 | 1.0000 to 1.0000 | 21/68 | 30.88% |
| prior_120m_swing_count | 3 | 2.0000 to 2.0000 | 28/72 | 38.89% |
| prior_120m_swing_count | 4 | 3.0000 to 3.0000 | 26/49 | 53.06% |
| prior_120m_swing_count | 5 | 4.0000 to 4.0000 | 14/48 | 29.17% |
| prior_120m_swing_count | 6 | 5.0000 to 6.0000 | 27/75 | 36.00% |
| prior_120m_swing_count | 7 | 7.0000 to 8.0000 | 34/69 | 49.28% |
| prior_120m_swing_count | 8 | 9.0000 to 12.0000 | 33/78 | 42.31% |
| prior_120m_swing_count | 9 | 13.0000 to 17.0000 | 42/69 | 60.87% |
| prior_120m_swing_count | 10 | 18.0000 to 37.0000 | 44/67 | 65.67% |
| volume_trend_30_over_60 | 1 | 0.0000 to 0.2598 | 29/67 | 43.28% |
| volume_trend_30_over_60 | 2 | 0.2611 to 0.3505 | 29/66 | 43.94% |
| volume_trend_30_over_60 | 3 | 0.3516 to 0.4288 | 30/66 | 45.45% |
| volume_trend_30_over_60 | 4 | 0.4310 to 0.5151 | 33/66 | 50.00% |
| volume_trend_30_over_60 | 5 | 0.5152 to 0.5908 | 29/66 | 43.94% |
| volume_trend_30_over_60 | 6 | 0.5909 to 0.6771 | 22/66 | 33.33% |
| volume_trend_30_over_60 | 7 | 0.6772 to 0.7784 | 27/66 | 40.91% |
| volume_trend_30_over_60 | 8 | 0.7787 to 0.9429 | 33/66 | 50.00% |
| volume_trend_30_over_60 | 9 | 0.9430 to 1.5025 | 22/66 | 33.33% |
| volume_trend_30_over_60 | 10 | 1.5125 to 52.0786 | 30/66 | 45.45% |
| change_since_confirm_pct | 1 | -46.2970 to -16.4062 | 32/68 | 47.06% |
| change_since_confirm_pct | 2 | -16.3630 to -6.6504 | 36/68 | 52.94% |
| change_since_confirm_pct | 3 | -6.5693 to -2.3983 | 25/68 | 36.76% |
| change_since_confirm_pct | 4 | -2.3419 to -0.0054 | 24/67 | 35.82% |
| change_since_confirm_pct | 5 | 0.0000 to 2.1739 | 18/69 | 26.09% |
| change_since_confirm_pct | 6 | 2.2047 to 5.1185 | 23/68 | 33.82% |
| change_since_confirm_pct | 7 | 5.1492 to 11.6788 | 27/68 | 39.71% |
| change_since_confirm_pct | 8 | 11.7553 to 21.7513 | 28/68 | 41.18% |
| change_since_confirm_pct | 9 | 21.7666 to 42.5801 | 35/68 | 51.47% |
| change_since_confirm_pct | 10 | 42.6774 to 608.3861 | 41/68 | 60.29% |
| confirm_change_pct | 1 | 30.4960 to 33.7980 | 20/70 | 28.57% |
| confirm_change_pct | 2 | 33.8270 to 35.6720 | 26/65 | 40.00% |
| confirm_change_pct | 3 | 35.8800 to 38.1880 | 30/72 | 41.67% |
| confirm_change_pct | 4 | 38.2610 to 40.4100 | 27/67 | 40.30% |
| confirm_change_pct | 5 | 40.4500 to 45.7060 | 22/67 | 32.84% |
| confirm_change_pct | 6 | 46.3410 to 58.0790 | 30/68 | 44.12% |
| confirm_change_pct | 7 | 58.2090 to 69.2350 | 32/65 | 49.23% |
| confirm_change_pct | 8 | 69.5700 to 85.6660 | 31/71 | 43.66% |
| confirm_change_pct | 9 | 88.6260 to 129.6300 | 28/65 | 43.08% |
| confirm_change_pct | 10 | 132.7170 to 482.7510 | 43/70 | 61.43% |

## Live-entry subset

This table labels actual logical entry fills but does not select thresholds. A fill matches the BUY-flip minute or the following minute, covering an intrabar resting trigger and its immediately following fill window.

| Logical fills | Matched fills | Unmatched fills | Matched flips | +5 hits |
|---:|---:|---:|---:|---:|
| 858 | 270/858 | 588/858 | 233 | 125/233 (53.65%) |

| Day | Symbol | Flip ET | Entry IDs | +5 | MFE | MAE | High age | Below high |
|---|---|---|---|---|---:|---:|---:|---:|
| 2026-07-23 | SKYQ | 15:44 | schwab_1m_v2-SKYQ-open-32e03e5-rb463b43e | False | 0.61% | -2.69% | 344.00m | 9.73% |
| 2026-07-27 | BIYA | 14:00 | schwab_1m_v2-BIYA-open-a10ef4fc34de | True | 23.12% | -1.38% | 223.00m | 15.92% |
| 2026-07-27 | ENTX | 14:13 | schwab_1m_v2-ENTX-open-02ce196b143f | True | 5.29% | -3.44% | 221.00m | 11.31% |
| 2026-07-27 | ENTX | 14:44 | schwab_1m_v2-ENTX-open-1ef045204512 | False | 3.19% | -2.50% | 252.00m | 9.95% |
| 2026-07-27 | FIEE | 13:00 | schwab_1m_v2-FIEE-open-c9d1289f1e36, schwab_1m_v2-FIEE-open-85eb123c53bf | True | 6.79% | -30.39% | 58.00m | 45.80% |
| 2026-07-28 | CNET | 15:57 | schwab_1m_v2-CNET-open-d638745f3c84 | False | 1.40% | -2.62% | 21.00m | 1.43% |
| 2026-07-28 | EGG | 12:58 | schwab_1m_v2-EGG-open-9487b02ed6fd | False | 1.07% | -12.69% | 132.00m | 27.50% |
| 2026-07-28 | INLF | 09:36 | schwab_1m_v2-INLF-open-7eb2bb66ed65 | True | 60.08% | -4.31% | 151.00m | 1.55% |
| 2026-07-28 | INLF | 11:47 | schwab_1m_v2-INLF-open-c85642badae5 | True | 7.05% | -3.86% | 74.00m | 36.37% |
| 2026-07-28 | INLF | 12:21 | schwab_1m_v2-INLF-open-f8fb3346eecf | False | 2.46% | -7.78% | 108.00m | 34.78% |
| 2026-07-28 | INLF | 14:42 | schwab_1m_v2-INLF-open-ff51130d8a8e | True | 13.99% | -5.37% | 249.00m | 44.01% |
| 2026-07-28 | STKH | 14:55 | schwab_1m_v2-STKH-open-647ee8fe1e12 | True | 25.68% | -9.46% | 80.00m | 42.78% |
| 2026-07-29 | AMIX | 10:58 | schwab_1m_v2-AMIX-open-1d585eff8a76 | False | 0.20% | -11.72% | 75.00m | 30.48% |
| 2026-07-29 | GMM | 09:47 | schwab_1m_v2-GMM-open-e560e6993c4a | True | 26.82% | -3.26% | 143.00m | 10.74% |
| 2026-07-29 | NCRA | 10:26 | schwab_1m_v2-NCRA-open-bd519c4280c3 | True | 31.57% | -11.40% | 104.00m | 37.25% |
| 2026-07-29 | NCRA | 12:03 | schwab_1m_v2-NCRA-open-9890d865acd9 | True | 12.05% | -6.84% | 201.00m | 34.40% |
| 2026-07-29 | NCRA | 13:47 | schwab_1m_v2-NCRA-open-90100a283483 | True | 12.55% | -6.89% | 305.00m | 36.71% |
| 2026-07-29 | STFS | 09:34 | schwab_1m_v2-STFS-open-73b01da4c9d7 | True | 22.31% | -4.38% | 153.00m | 26.69% |
| 2026-07-29 | STFS | 11:15 | schwab_1m_v2-STFS-open-5bb49cfac693 | False | 0.41% | -8.87% | 254.00m | 23.41% |
| 2026-07-30 | AXTX | 12:50 | schwab_1m_v2-AXTX-open-1f14832f5403 | True | 13.53% | 0.00% | 26.00m | 2.25% |
| 2026-07-30 | CRWU | 12:05 | schwab_1m_v2-CRWU-open-17df2e51ef84 | False | 1.15% | -0.16% | 14.00m | 0.82% |
| 2026-07-30 | IRE | 12:55 | schwab_1m_v2-IRE-open-318d280d8bc2 | False | 0.94% | -1.11% | 36.00m | 3.07% |
| 2026-07-30 | IREG | 13:19 | schwab_1m_v2-IREG-open-7aae18ace8f5 | False | 0.00% | -0.76% | 60.00m | 2.86% |
| 2026-07-30 | NUWE | 11:55 | schwab_1m_v2-NUWE-open-e9848c3937d1 | False | 4.61% | -3.69% | 133.00m | 32.85% |
| 2026-07-30 | NUWE | 13:56 | schwab_1m_v2-NUWE-open-4374c74089c4 | True | 10.04% | -13.37% | 254.00m | 35.47% |
| 2026-07-30 | NUWE | 15:38 | schwab_1m_v2-NUWE-open-507c399d325a | False | 3.53% | -2.62% | 356.00m | 33.12% |
| 2026-07-30 | SNDG | 12:51 | schwab_1m_v2-SNDG-open-3ae2eeaa96c0 | False | 2.79% | -0.32% | 35.00m | 2.98% |
| 2026-07-30 | SNDG | 14:21 | schwab_1m_v2-SNDG-open-a69238043936 | False | 0.80% | -0.32% | 61.00m | 3.03% |
| 2026-07-31 | AXTX | 12:01 | schwab_1m_v2-AXTX-open-cb54d532864b | False | 3.78% | -0.67% | 96.00m | 4.83% |
| 2026-07-31 | FCUV | 10:42 | schwab_1m_v2-FCUV-open-9323e248a3eb, schwab_1m_v2-FCUV-open-61ec74db9633 | True | 13.57% | -18.92% | 46.00m | 24.64% |
| 2026-07-31 | FCUV | 12:39 | schwab_1m_v2-FCUV-open-dd453df0994b, schwab_1m_v2-FCUV-open-5be8a4307219 | True | 14.48% | -16.04% | 163.00m | 24.96% |
| 2026-07-31 | KUST | 09:11 | schwab_1m_v2-KUST-open-ca09a5694276, schwab_1m_v2-KUST-open-497034986e21 | True | 9.30% | -5.81% | 46.00m | 12.31% |
| 2026-08-03 | EZRA | 13:48 | schwab_1m_v2-EZRA-open-d739faf4a31a | False | 4.80% | -5.51% | 244.00m | 40.00% |
| 2026-08-03 | FUSE | 11:36 | schwab_1m_v2-FUSE-open-88c47890bc74, schwab_1m_v2-FUSE-open-9ef1d90a71c7 | False | 2.13% | -14.54% | 48.00m | 27.77% |
| 2026-08-03 | HYFM | 12:09 | schwab_1m_v2-HYFM-open-aadcb40bc61f | True | 32.77% | -2.26% | 227.00m | 59.95% |
| 2026-08-03 | HYFM | 13:31 | schwab_1m_v2-HYFM-open-6b6d07576daa | True | 7.98% | -7.51% | 309.00m | 49.76% |
| 2026-08-03 | HYFM | 14:13 | schwab_1m_v2-HYFM-open-0888532e8e26 | True | 17.59% | -7.87% | 351.00m | 49.24% |
| 2026-08-03 | UPC | 09:21 | schwab_1m_v2-UPC-open-681d7ad80534 | False | 2.30% | -5.28% | 140.00m | 17.85% |
| 2026-08-03 | UPC | 10:47 | schwab_1m_v2-UPC-open-e75250cce01b | True | 28.13% | -9.29% | 226.00m | 27.85% |
| 2026-08-04 | AMIX | 10:12 | schwab_1m_v2-AMIX-open-c0b9e263f9f3, schwab_1m_v2-AMIX-open-bb2e8bfd96d7 | True | 241.19% | -14.44% | 35.00m | 16.96% |
| 2026-08-05 | BJDX | 08:49 | schwab_1m_v2-BJDX-open-270e6079e62a, schwab_1m_v2-BJDX-open-cc9d42db72e4 | True | 10.64% | -3.55% | 54.00m | 8.05% |
| 2026-08-05 | BJDX | 15:24 | schwab_1m_v2-BJDX-open-8d1d9a8f121e | False | 3.38% | -11.49% | 144.00m | 13.91% |
| 2026-08-05 | GTE | 09:30 | schwab_1m_v2-GTE-open-c0937391da13 | True | 10.17% | -4.33% | 24.00m | 3.02% |
| 2026-08-05 | GTE | 11:15 | schwab_1m_v2-GTE-open-2d6b3ec5e46a | False | 1.51% | -3.71% | 94.00m | 14.18% |
| 2026-08-05 | GTE | 12:45 | schwab_1m_v2-GTE-open-724378f75628 | False | 0.86% | -1.83% | 184.00m | 19.73% |
| 2026-08-05 | INLF | 13:02 | schwab_1m_v2-INLF-open-07159f6df2dc | False | 4.71% | -14.17% | 96.00m | 30.06% |
| 2026-08-05 | INLF | 14:03 | schwab_1m_v2-INLF-open-01d15576c7d6 | True | 7.90% | -2.07% | 157.00m | 35.41% |
| 2026-08-05 | INLF | 15:17 | schwab_1m_v2-INLF-open-8f29fa0f492b | True | 10.45% | -7.78% | 231.00m | 32.59% |
| 2026-08-05 | YXT | 10:38 | schwab_1m_v2-YXT-open-956d4b3a2534 | True | 147.81% | -1.42% | 139.00m | 33.63% |
| 2026-08-05 | YXT | 12:42 | schwab_1m_v2-YXT-open-9e19a1048cb1 | True | 7.94% | -22.46% | 73.00m | 25.13% |
| 2026-08-05 | YXT | 13:36 | schwab_1m_v2-YXT-open-37f57ed5f2a8 | True | 130.06% | -9.81% | 127.00m | 37.63% |
| 2026-08-05 | ZYBT | 14:14 | schwab_1m_v2-ZYBT-open-4336ad7fce36 | True | 37.44% | -6.06% | 158.00m | 52.91% |
| 2026-08-05 | ZYBT | 15:45 | schwab_1m_v2-ZYBT-open-18245effd0a6 | False | 3.21% | -8.84% | 249.00m | 43.59% |
| 2026-08-06 | AZI | 11:22 | schwab_1m_v2-AZI-open-3a4c2296f705 | False | 1.14% | -15.72% | 57.00m | 32.95% |
| 2026-08-06 | AZI | 12:46 | schwab_1m_v2-AZI-open-840a8231445d | True | 6.21% | -6.21% | 141.00m | 45.74% |
| 2026-08-06 | AZI | 14:21 | schwab_1m_v2-AZI-open-8a0be6d33cd0 | True | 13.31% | -0.33% | 236.00m | 43.99% |
| 2026-08-06 | CLRO | 08:21 | schwab_1m_v2-CLRO-open-5ba5e71bafe0, schwab_1m_v2-CLRO-open-1485b2ae3b67 | True | 18.84% | -5.58% | 52.00m | 20.94% |
| 2026-08-06 | CLRO | 11:01 | schwab_1m_v2-CLRO-open-1bd44f613f5b | True | 9.59% | -6.18% | 212.00m | 30.39% |
| 2026-08-06 | CLRO | 12:37 | schwab_1m_v2-CLRO-open-3decccfe721f, schwab_1m_v2-CLRO-open-18c5098f92e5 | True | 6.64% | -2.50% | 308.00m | 34.73% |
| 2026-08-06 | PAVS | 08:12 | schwab_1m_v2-PAVS-open-b6299062034a | False | 4.08% | -14.00% | 72.00m | 19.16% |
| 2026-08-06 | PAVS | 11:35 | schwab_1m_v2-PAVS-open-2239952d45f5 | False | 3.77% | -8.94% | 275.00m | 39.07% |
| 2026-08-06 | WYHG | 12:31 | schwab_1m_v2-WYHG-open-39e4b4a455a7, schwab_1m_v2-WYHG-open-7d4978acf3a1 | True | 25.04% | -4.96% | 60.00m | 28.58% |
| 2026-08-06 | WYHG | 13:32 | schwab_1m_v2-WYHG-open-798fae2aa790 | False | 4.71% | -22.61% | 121.00m | 21.09% |
| 2026-08-07 | DSY | 08:15 | schwab_1m_v2-DSY-open-55ab48f32080 | True | 41.95% | -6.29% | 70.00m | 14.24% |
| 2026-08-07 | DSY | 10:59 | schwab_1m_v2-DSY-open-3b8ece5b1c9c | False | 0.18% | -5.45% | 144.00m | 29.43% |
| 2026-08-07 | DSY | 12:43 | schwab_1m_v2-DSY-open-4b42f08c9eab | False | 1.49% | -4.67% | 248.00m | 35.81% |
| 2026-08-07 | DSY | 15:59 | schwab_1m_v2-DSY-open-3a59ccd02daf | None | UNKNOWN% | UNKNOWN% | 444.00m | 40.11% |
| 2026-08-07 | HUIZ | 15:50 | schwab_1m_v2-HUIZ-open-a91033cfa50a | False | 0.26% | -10.88% | 141.00m | 39.08% |
| 2026-08-07 | MB | 15:42 | schwab_1m_v2-MB-open-59dd061a8a8d | None | UNKNOWN% | UNKNOWN% | 202.00m | 15.90% |
| 2026-08-07 | NAMI | 11:26 | schwab_1m_v2-NAMI-open-373268d4fbad, schwab_1m_v2-NAMI-open-af283990ae16 | False | 2.81% | -8.24% | 162.00m | 50.05% |
| 2026-08-07 | NAMI | 12:02 | schwab_1m_v2-NAMI-open-bb9962c2a0be | False | 0.74% | -8.18% | 198.00m | 47.95% |
| 2026-08-10 | AUUD | 08:52 | schwab_1m_v2-AUUD-open-69c6e7d27f6a | False | 3.89% | -10.05% | 38.00m | 16.67% |
| 2026-08-10 | AUUD | 10:04 | schwab_1m_v2-AUUD-open-f345ea227854 | True | 18.46% | -6.15% | 110.00m | 31.66% |
| 2026-08-10 | AUUD | 13:04 | schwab_1m_v2-AUUD-open-ed86d25f9484 | True | 5.53% | -4.68% | 290.00m | 36.12% |
| 2026-08-10 | AUUD | 14:59 | schwab_1m_v2-AUUD-open-c902143478e9 | True | 10.46% | -1.25% | 405.00m | 34.72% |
| 2026-08-10 | JWEL | 08:32 | schwab_1m_v2-JWEL-open-b5e2b028520f | False | 4.01% | -15.65% | 53.00m | 20.99% |
| 2026-08-10 | JWEL | 09:42 | schwab_1m_v2-JWEL-open-c574c1d26907 | True | 5.51% | -27.14% | 123.00m | 29.33% |
| 2026-08-10 | JWEL | 10:53 | schwab_1m_v2-JWEL-open-68320f776fec | False | 1.32% | -9.07% | 194.00m | 45.20% |
| 2026-08-10 | JWEL | 12:15 | schwab_1m_v2-JWEL-open-ab6b9dfdab4a | True | 8.54% | -3.48% | 276.00m | 50.80% |
| 2026-08-10 | JWEL | 13:14 | schwab_1m_v2-JWEL-open-9b1b5bf45f45 | False | 0.15% | -5.61% | 335.00m | 47.60% |
| 2026-08-10 | JWEL | 15:28 | schwab_1m_v2-JWEL-open-bead19be1571, schwab_1m_v2-JWEL-open-89843088b7d1 | True | 18.10% | -6.82% | 469.00m | 48.08% |
| 2026-08-10 | SCKT | 11:59 | schwab_1m_v2-SCKT-open-c0245a68ce8e | True | 50.60% | -16.47% | 162.00m | 42.80% |
| 2026-08-10 | SCKT | 14:42 | schwab_1m_v2-SCKT-open-9f7b89da908c | True | 10.10% | -8.65% | 325.00m | 30.88% |
| 2026-08-10 | SCKT | 15:36 | schwab_1m_v2-SCKT-open-2de17cd02930, schwab_1m_v2-SCKT-open-636f4ec9fdae | True | 6.29% | -6.28% | 379.00m | 29.12% |
| 2026-08-10 | STKH | 09:33 | schwab_1m_v2-STKH-open-ae4214fc4bf3, schwab_1m_v2-STKH-open-4104176229ec | True | 66.28% | -7.96% | 152.00m | 22.37% |
| 2026-08-10 | STKH | 11:20 | schwab_1m_v2-STKH-open-836da4dbfd8c | True | 10.96% | -7.73% | 62.00m | 42.11% |
| 2026-08-10 | STKH | 13:08 | schwab_1m_v2-STKH-open-4237c3d7f64e, schwab_1m_v2-STKH-open-de9b4b5c3ef5 | True | 16.59% | -3.52% | 170.00m | 45.21% |
| 2026-08-10 | XHLD | 10:50 | schwab_1m_v2-XHLD-open-c2170481a100 | True | 21.69% | -19.44% | 57.00m | 26.01% |
| 2026-08-10 | XHLD | 12:41 | schwab_1m_v2-XHLD-open-76ce2a2b08a7 | False | 1.23% | -6.79% | 168.00m | 32.20% |
| 2026-08-10 | XHLD | 14:55 | schwab_1m_v2-XHLD-open-c8c988860833, schwab_1m_v2-XHLD-open-ab9c05c688bb | False | 3.23% | -4.84% | 302.00m | 40.32% |
| 2026-08-11 | FRTT | 09:16 | schwab_1m_v2-FRTT-open-766556b05a6b | False | 3.33% | -8.00% | 71.00m | 55.52% |
| 2026-08-11 | MSGY | 14:18 | schwab_1m_v2-MSGY-open-a8acadc99cf0 | False | 3.46% | -6.91% | 157.00m | 37.07% |
| 2026-08-11 | PLAG | 09:43 | schwab_1m_v2-PLAG-open-3c2dfbcdd85d | True | 223.20% | -13.53% | 59.00m | 30.86% |
| 2026-08-11 | WXM | 11:12 | schwab_1m_v2-WXM-open-b27ef67d595e, schwab_1m_v2-WXM-open-691eefbdd072 | True | 7.53% | -9.23% | 132.00m | 42.34% |
| 2026-08-11 | WXM | 13:59 | schwab_1m_v2-WXM-open-d24139dfa960, schwab_1m_v2-WXM-open-7ccfbd86ac99 | True | 8.60% | -7.99% | 299.00m | 42.56% |
| 2026-08-12 | BAOS | 07:23 | schwab_1m_v2-BAOS-open-ab76a9cd550f | False | 1.75% | -9.65% | 22.00m | 17.04% |
| 2026-08-12 | BAOS | 08:04 | schwab_1m_v2-BAOS-open-f321d7fb3454 | True | 7.83% | -2.61% | 63.00m | 17.04% |
| 2026-08-12 | BAOS | 09:30 | schwab_1m_v2-BAOS-open-b9a9649018d9 | False | 0.08% | -20.02% | 149.00m | 18.52% |
| 2026-08-12 | BIVI | 13:02 | schwab_1m_v2-BIVI-open-d4aa09684192 | False | 3.62% | -10.30% | 140.00m | 54.09% |
| 2026-08-12 | BOXL | 07:34 | schwab_1m_v2-BOXL-open-20d68ea6642b | True | 7.24% | -3.07% | 24.00m | 3.87% |
| 2026-08-12 | BOXL | 11:49 | schwab_1m_v2-BOXL-open-2646a476a26e, schwab_1m_v2-BOXL-open-0cf1eedd1a8f | True | 12.42% | -0.67% | 132.00m | 19.31% |
| 2026-08-12 | BOXL | 14:30 | schwab_1m_v2-BOXL-open-1777df0a2cf9 | True | 96.62% | -0.99% | 293.00m | 8.58% |
| 2026-08-12 | BQ | 11:59 | schwab_1m_v2-BQ-open-05d12b68b671 | True | 17.39% | -10.14% | 47.00m | 39.06% |
| 2026-08-12 | BQ | 13:00 | schwab_1m_v2-BQ-open-7294c0d8fba1 | True | 5.42% | -12.60% | 108.00m | 32.62% |
| 2026-08-12 | BQ | 13:59 | schwab_1m_v2-BQ-open-ae8a6207eee5 | False | 2.96% | -6.60% | 167.00m | 35.95% |
| 2026-08-12 | CRWU | 12:50 | schwab_1m_v2-CRWU-open-8b434844b8e8 | False | 2.87% | 0.00% | 191.00m | 8.09% |
| 2026-08-12 | CRWU | 13:18 | schwab_1m_v2-CRWU-open-5b9f2859836d | False | 1.04% | -0.34% | 219.00m | 6.87% |
| 2026-08-12 | DOGZ | 10:32 | schwab_1m_v2-DOGZ-open-7c5c76920908 | True | 7.75% | -2.33% | 31.00m | 12.68% |
| 2026-08-12 | DOGZ | 11:36 | schwab_1m_v2-DOGZ-open-3652d2e09732 | False | 1.06% | -5.70% | 95.00m | 10.21% |
| 2026-08-12 | DOGZ | 15:48 | schwab_1m_v2-DOGZ-open-f993a5302e1d | False | 1.56% | -5.86% | 347.00m | 11.27% |
| 2026-08-12 | OFAL | 12:03 | schwab_1m_v2-OFAL-open-2f18a51e21e2 | False | 3.17% | -20.81% | 127.00m | 44.91% |
| 2026-08-12 | OFAL | 15:22 | schwab_1m_v2-OFAL-open-6db113a796b6, schwab_1m_v2-OFAL-open-b91bb0636dc9 | False | 2.05% | -8.22% | 326.00m | 63.32% |
| 2026-08-12 | RMCF | 09:48 | schwab_1m_v2-RMCF-open-6e7c2649e925 | True | 11.40% | -9.32% | 146.00m | 29.84% |
| 2026-08-12 | RMCF | 11:12 | schwab_1m_v2-RMCF-open-c2ae30e2cf8c | True | 22.91% | -4.73% | 230.00m | 46.77% |
| 2026-08-12 | RMCF | 15:41 | schwab_1m_v2-RMCF-open-7045df1ccfb5 | False | 4.85% | -4.68% | 499.00m | 43.10% |
| 2026-08-12 | XHLD | 15:47 | schwab_1m_v2-XHLD-open-86fc0e7e5888 | True | 6.48% | -2.10% | 91.00m | 16.93% |
| 2026-08-13 | DFSC | 11:04 | schwab_1m_v2-DFSC-open-eaad1a9708b7 | False | 0.72% | -8.17% | 79.00m | 36.41% |
| 2026-08-13 | DFSC | 12:29 | schwab_1m_v2-DFSC-open-d0003404d525 | True | 29.98% | -3.07% | 164.00m | 38.38% |
| 2026-08-13 | DFSC | 15:47 | schwab_1m_v2-DFSC-open-6e5a6068537c | False | 1.65% | -5.79% | 362.00m | 34.87% |
| 2026-08-13 | FGI | 08:10 | schwab_1m_v2-FGI-open-0e146bcb4ad5, schwab_1m_v2-FGI-open-45e087cb48ed | True | 10.91% | -5.44% | 22.00m | 6.95% |
| 2026-08-13 | FGI | 08:52 | schwab_1m_v2-FGI-open-31f9a228c8e8 | False | 0.20% | -15.15% | 32.00m | 10.00% |
| 2026-08-13 | FGI | 10:01 | schwab_1m_v2-FGI-open-210b4301e8a0 | True | 137.83% | -0.27% | 101.00m | 15.93% |
| 2026-08-13 | FGI | 13:44 | schwab_1m_v2-FGI-open-3328b77c01b1 | True | 6.15% | -9.49% | 66.00m | 22.58% |
| 2026-08-13 | XHG | 15:36 | schwab_1m_v2-XHG-open-7ed964542368 | False | 2.95% | -7.12% | 412.00m | 48.90% |
| 2026-08-14 | AKAN | 10:11 | schwab_1m_v2-AKAN-open-f928480516a4 | False | 3.49% | -7.36% | 188.00m | 11.36% |
| 2026-08-14 | STKH | 09:20 | schwab_1m_v2-STKH-open-98c256885e6b | True | 16.89% | -14.33% | 35.00m | 39.51% |
| 2026-08-14 | STKH | 10:30 | schwab_1m_v2-STKH-open-8b666c85856f | False | 4.77% | -4.85% | 105.00m | 48.17% |
| 2026-08-14 | STKH | 12:35 | schwab_1m_v2-STKH-open-05505ec12870 | True | 17.83% | -3.39% | 230.00m | 46.22% |
| 2026-08-14 | SXTC | 14:27 | schwab_1m_v2-SXTC-open-a91698dc9f0d | False | 1.69% | -9.09% | 353.00m | 13.09% |
| 2026-08-14 | VWAV | 13:39 | schwab_1m_v2-VWAV-open-72faff178ca1 | True | 15.86% | -4.44% | 20.00m | 0.82% |
| 2026-08-14 | WETO | 08:47 | schwab_1m_v2-WETO-open-7b3d2f8cf025 | True | 7.47% | -5.91% | 64.00m | 16.44% |
| 2026-08-14 | WETO | 12:19 | schwab_1m_v2-WETO-open-b0b167c3b069 | False | 0.31% | -5.37% | 142.00m | 38.84% |
| 2026-08-17 | IPST | 09:30 | schwab_1m_v2-IPST-open-3dfe503c6c25, schwab_1m_v2-IPST-open-ad8a8c5e1f5d | True | 9.31% | -12.99% | 35.00m | 8.73% |
| 2026-08-17 | IPST | 12:28 | schwab_1m_v2-IPST-open-8e457df9b13b | True | 5.03% | -8.46% | 88.00m | 19.57% |
| 2026-08-17 | IPST | 14:32 | schwab_1m_v2-IPST-open-abdc00a84b5e | False | 1.74% | -5.50% | 212.00m | 24.74% |
| 2026-08-17 | IPST | 15:14 | schwab_1m_v2-IPST-open-214355bd1174 | True | 5.87% | -3.50% | 254.00m | 24.84% |
| 2026-08-17 | IVF | 08:26 | schwab_1m_v2-IVF-open-49258440c41a | False | 3.95% | -7.56% | 86.00m | 21.31% |
| 2026-08-17 | IVF | 12:02 | schwab_1m_v2-IVF-open-448492240d3a | False | 0.24% | -6.86% | 302.00m | 33.34% |
| 2026-08-17 | IVF | 12:26 | schwab_1m_v2-IVF-open-e12469017c95 | False | 2.78% | -17.31% | 326.00m | 34.61% |
| 2026-08-17 | IVF | 13:50 | schwab_1m_v2-IVF-open-690530dacd14 | False | 4.74% | -3.62% | 410.00m | 44.23% |
| 2026-08-17 | WFF | 13:42 | schwab_1m_v2-WFF-open-f92ac2311403 | True | 7.56% | -26.43% | 94.00m | 79.41% |
| 2026-08-18 | AIXC | 12:14 | schwab_1m_v2-AIXC-open-ee79dd34a7b7, schwab_1m_v2-AIXC-open-eb76e31cd8ee | True | 16.97% | -9.09% | 75.00m | 50.64% |
| 2026-08-18 | CAST | 12:54 | schwab_1m_v2-CAST-open-42499c0f47a4, schwab_1m_v2-CAST-open-29b840bf97e6 | True | 17.35% | -4.94% | 49.00m | 27.53% |
| 2026-08-18 | CAST | 14:26 | schwab_1m_v2-CAST-open-e790bbbad008 | True | 27.07% | -1.10% | 141.00m | 16.91% |
| 2026-08-18 | CAST | 15:58 | schwab_1m_v2-CAST-open-72fab2ae80a8 | False | 0.46% | -5.02% | 33.00m | 7.40% |
| 2026-08-18 | IPST | 12:42 | schwab_1m_v2-IPST-open-2d013d6b52b2 | True | 42.83% | -8.41% | 89.00m | 29.68% |
| 2026-08-18 | SLE | 10:54 | schwab_1m_v2-SLE-open-ca78520b1572 | True | 13.98% | -1.11% | 79.00m | 41.59% |
| 2026-08-18 | XOS | 13:45 | schwab_1m_v2-XOS-open-b276b4a15d63 | False | 3.94% | -1.85% | 390.00m | 22.30% |
| 2026-08-19 | MSS | 11:37 | schwab_1m_v2-MSS-open-d7cce7211a2e | True | 5.59% | -8.72% | 113.00m | 30.45% |
| 2026-08-19 | MSS | 12:29 | schwab_1m_v2-MSS-open-0c13ad23e57b | True | 12.27% | -3.18% | 165.00m | 30.77% |
| 2026-08-19 | MSS | 14:21 | schwab_1m_v2-MSS-open-e990dab01908 | False | 4.26% | -6.38% | 277.00m | 25.96% |
| 2026-08-19 | TNON | 09:35 | schwab_1m_v2-TNON-open-22871f339e6c | True | 40.53% | -22.09% | 119.00m | 5.18% |
| 2026-08-19 | TNON | 12:20 | schwab_1m_v2-TNON-open-a97aa980c4b9 | False | 2.70% | -8.81% | 93.00m | 22.59% |
| 2026-08-19 | TNON | 13:54 | schwab_1m_v2-TNON-open-b5b5055fd991 | False | 2.45% | -8.45% | 187.00m | 26.12% |
| 2026-08-19 | TNON | 15:15 | schwab_1m_v2-TNON-open-c04add4c0146 | False | 4.30% | -5.71% | 268.00m | 35.54% |
| 2026-08-19 | YJ | 09:59 | schwab_1m_v2-YJ-open-c859bf286d7b | False | 1.02% | -20.41% | 35.00m | 27.76% |
| 2026-08-19 | YJ | 11:41 | schwab_1m_v2-YJ-open-95ac399d8544 | False | 0.92% | -7.32% | 137.00m | 33.92% |
| 2026-08-19 | YJ | 13:40 | schwab_1m_v2-YJ-open-978348e75710 | False | 1.76% | -9.80% | 256.00m | 41.23% |
| 2026-08-20 | BIVI | 14:05 | schwab_1m_v2-BIVI-open-ad0124cf533e | False | 2.40% | -3.46% | 188.00m | 24.26% |
| 2026-08-20 | BRLS | 14:37 | schwab_1m_v2-BRLS-open-18a5192c5147 | True | 39.26% | -10.36% | 119.00m | 45.45% |
| 2026-08-20 | BTCT | 11:05 | schwab_1m_v2-BTCT-open-ec2da468bed8 | True | 13.25% | -5.30% | 48.00m | 17.61% |
| 2026-08-20 | JZ | 15:34 | schwab_1m_v2-JZ-open-7fb8a16719c7 | True | 32.19% | -0.96% | 347.00m | 54.65% |
| 2026-08-20 | PCLA | 15:35 | schwab_1m_v2-PCLA-open-686dff300732 | False | 2.15% | -15.27% | 76.00m | 31.30% |
| 2026-08-20 | SGLY | 08:09 | schwab_1m_v2-SGLY-open-78a605630eda | False | 2.73% | -44.62% | 68.00m | 18.63% |
| 2026-08-21 | EXYN | 13:13 | schwab_1m_v2-EXYN-open-4032f9571f58, schwab_1m_v2-EXYN-open-09019c33c5c5 | True | 8.18% | -7.76% | 38.00m | 16.98% |
| 2026-08-21 | JUNS | 10:01 | schwab_1m_v2-JUNS-open-db2c5e9a6528, schwab_1m_v2-JUNS-open-93237d303bc3 | True | 17.28% | -14.14% | 159.00m | 15.81% |
| 2026-08-21 | SUGP | 08:07 | schwab_1m_v2-SUGP-open-84d82d83c854 | False | 0.69% | -6.67% | 64.00m | 12.16% |
| 2026-08-21 | SUGP | 09:37 | schwab_1m_v2-SUGP-open-89ff037eff1e | True | 7.87% | -7.87% | 154.00m | 21.24% |
| 2026-08-21 | USDE | 12:42 | schwab_1m_v2-USDE-open-852ac86f44f7, schwab_1m_v2-USDE-open-89dd0cb11069 | True | 19.35% | -6.59% | 50.00m | 13.09% |
| 2026-08-24 | BTCT | 09:26 | schwab_1m_v2-BTCT-open-42b38d7a502e | False | 0.54% | -8.36% | 57.00m | 9.88% |
| 2026-08-24 | LUCY | 15:19 | schwab_1m_v2-LUCY-open-54ae0a33cc32 | False | 0.01% | -6.30% | 180.00m | 20.00% |
| 2026-08-24 | PMI | 10:02 | schwab_1m_v2-PMI-open-1ebb7697dea0, schwab_1m_v2-PMI-open-60b71e0c48b5 | True | 20.77% | -2.71% | 182.00m | 5.37% |
| 2026-08-24 | PMI | 15:13 | schwab_1m_v2-PMI-open-21e6345eb912, schwab_1m_v2-PMI-open-93d69e2fcae5 | True | 13.25% | -2.99% | 270.00m | 14.31% |
| 2026-08-24 | XPON | 09:33 | schwab_1m_v2-XPON-open-7ddd6dd0902b, schwab_1m_v2-XPON-open-7ad1a730665d | False | 1.72% | -24.94% | 24.00m | 16.93% |
| 2026-08-24 | XPON | 13:31 | schwab_1m_v2-XPON-open-432d825312b5 | False | 4.16% | -6.79% | 262.00m | 30.13% |
| 2026-08-25 | AIXI | 14:12 | schwab_1m_v2-AIXI-open-d9f535b21e04 | False | 2.36% | -8.66% | 230.00m | 22.93% |
| 2026-08-25 | BTCT | 11:48 | schwab_1m_v2-BTCT-open-b909e1cde522 | True | 7.27% | -2.02% | 98.00m | 16.45% |
| 2026-08-25 | DAIC | 14:02 | schwab_1m_v2-DAIC-open-14583d57a476 | True | 12.30% | -5.19% | 265.00m | 31.62% |
| 2026-08-25 | DAIC | 15:44 | schwab_1m_v2-DAIC-open-cf10f0a24fc7 | True | 8.44% | -0.24% | 367.00m | 30.43% |
| 2026-08-26 | CRE | 08:57 | schwab_1m_v2-CRE-open-a2bc4c6d2e51 | False | 1.72% | -18.96% | 68.00m | 22.76% |
| 2026-08-26 | CRE | 12:49 | schwab_1m_v2-CRE-open-5ee860e6188c | True | 7.35% | -3.99% | 143.00m | 28.02% |
| 2026-08-26 | CRE | 15:24 | schwab_1m_v2-CRE-open-19a3cc0d03b9 | True | 10.95% | -2.58% | 298.00m | 29.77% |
| 2026-08-26 | DAIC | 08:34 | schwab_1m_v2-DAIC-open-ec0b378cebdc, schwab_1m_v2-DAIC-open-088f9a22ad8d | True | 13.97% | -1.19% | 84.00m | 17.17% |
| 2026-08-26 | XPON | 11:36 | schwab_1m_v2-XPON-open-8c4579fcd44b, schwab_1m_v2-XPON-open-5ab5964fc638 | False | 0.00% | -9.71% | 81.00m | 21.54% |
| 2026-08-26 | XPON | 14:47 | schwab_1m_v2-XPON-open-cb97b9996735, schwab_1m_v2-XPON-open-5eb9ed08e3cc | True | 7.99% | -1.67% | 272.00m | 30.83% |
| 2026-08-26 | YYGH | 09:39 | schwab_1m_v2-YYGH-open-26094216a405 | False | 4.04% | -11.04% | 159.00m | 26.82% |
| 2026-08-26 | YYGH | 10:32 | schwab_1m_v2-YYGH-open-a3c9e2946923 | False | 4.88% | -2.31% | 212.00m | 26.05% |
| 2026-08-26 | YYGH | 11:30 | schwab_1m_v2-YYGH-open-d4fcbc90c3dc | False | 3.38% | -7.01% | 270.00m | 25.67% |
| 2026-08-26 | YYGH | 13:23 | schwab_1m_v2-YYGH-open-6187805d8ea1 | True | 5.70% | -1.55% | 383.00m | 27.01% |
| 2026-08-27 | CELU | 12:35 | 60a88168-fe74-5952-8c85-29160fd0a1c2, schwab_1m_v2-CELU-open-0a338f7c4bb0 | True | 8.64% | -2.72% | 88.00m | 22.36% |
| 2026-08-27 | CELU | 14:15 | schwab_1m_v2-CELU-open-37d18e86a462, c9ebf51f-d559-53c8-ae99-5354c203df98 | True | 8.91% | -7.59% | 188.00m | 18.86% |
| 2026-08-27 | MIMI | 11:29 | be9efe99-04e1-5b1b-80f7-0444b72a557a | False | 4.83% | -2.97% | 87.00m | 21.43% |
| 2026-08-27 | PPCB | 10:59 | dfcc2a77-3d66-5ad6-b72f-0f6aa97811d9 | False | 3.53% | -9.85% | 120.00m | 39.19% |
| 2026-08-27 | PPCB | 14:15 | aa31a212-d9ce-5a67-a5ef-d794a29c45bd | False | 1.80% | -3.83% | 316.00m | 51.15% |
| 2026-08-31 | AEHL | 08:18 | 20bb1d2a-0217-504d-bd1b-e02125d83aea | True | 27.02% | -1.48% | 78.00m | 14.67% |
| 2026-08-31 | AEHL | 14:14 | c835d862-23ee-5f8b-ad39-911e51d94b5b | True | 8.95% | -1.63% | 93.00m | 26.14% |
| 2026-08-31 | NCRA | 13:13 | 44f444cf-6433-53ae-ae64-a2ea67bca58a | True | 7.69% | -5.90% | 108.00m | 21.88% |
| 2026-08-31 | WETO | 12:46 | f6982aac-fd9f-550a-bfc7-5fde0f0b0552 | True | 14.13% | -1.44% | 108.00m | 27.86% |
| 2026-08-31 | YDDL | 14:35 | e6341e3d-9979-5cd0-ad84-8d5635f40ccf | False | 0.43% | -7.69% | 455.00m | 18.31% |
| 2026-09-01 | BIAF | 11:31 | 38e96df0-d3f0-518e-b80d-b97ebdbd1927 | True | 8.10% | -4.18% | 113.00m | 24.07% |
| 2026-09-01 | FLYE | 11:37 | c2f27f50-1178-5087-9f36-1773ceaaf79f | True | 22.83% | -2.58% | 102.00m | 33.87% |
| 2026-09-01 | FLYE | 13:48 | fe8fa8a2-ad36-5248-b2cb-a5677b70645b | True | 19.22% | -1.70% | 233.00m | 28.31% |
| 2026-09-01 | RDAC | 13:48 | 86fbe7f9-72dc-55ea-bd3d-b033a7318fb3 | False | 3.60% | -8.46% | 157.00m | 24.83% |
| 2026-09-01 | SSM | 09:37 | 1c3cfcaf-d0f2-55cf-9c2f-27ea23ceb4f1 | True | 10.28% | -7.77% | 240.00m | 12.27% |
| 2026-09-01 | SSM | 10:43 | d16310c8-a1a7-54b5-8f2e-2edc18f15989 | False | 2.67% | -6.40% | 55.00m | 8.75% |
| 2026-09-02 | BIAF | 13:55 | aabdf49f-29b6-5022-883e-42fece27fc2c | False | 3.56% | -4.81% | 64.00m | 14.89% |
| 2026-09-02 | LHAI | 14:46 | 6a168eef-b665-5c49-93c4-f8fc1fe6087b | False | 0.00% | -8.55% | 332.00m | 12.31% |
| 2026-09-02 | VIOT | 11:58 | d7ebdb4d-4ab4-5c57-b997-da52403698b9 | True | 9.62% | -1.92% | 45.00m | 11.11% |
| 2026-09-03 | CHPT | 10:06 | 9a69fa26-4760-5c82-9a16-86637810cc62 | True | 10.91% | -1.15% | 19.00m | 2.68% |
| 2026-09-04 | CDTG | 11:40 | ed1f05db-874d-5981-9899-e441f94b67fa | False | 3.49% | -9.09% | 32.00m | 12.81% |
| 2026-09-04 | CDTG | 13:35 | ebb5b256-9add-59ac-9c57-17957f9400df | True | 9.77% | -3.01% | 147.00m | 18.75% |
| 2026-09-04 | IMRN | 09:16 | 8fc26b5f-2145-5ff7-9bed-2b5fa023cee4 | True | 10.56% | -7.78% | 105.00m | 10.53% |
| 2026-09-04 | IMRN | 13:33 | 2381c027-43de-561c-a2ef-74f0c60c0c05 | True | 10.03% | -2.13% | 228.00m | 20.85% |
| 2026-09-08 | BNC | 10:59 | a861fce1-415a-5eca-8f10-c3711fd88d67 | True | 13.43% | -0.62% | 398.00m | 29.12% |
| 2026-09-08 | BNC | 13:24 | ae282df5-9c29-559d-a75b-26433172e0f1 | False | 1.35% | -3.38% | 543.00m | 21.56% |
| 2026-09-08 | NUR | 12:05 | 16108230-0f8c-54ab-9a80-ae599b4c7131 | True | 24.93% | -7.89% | 95.00m | 54.86% |
| 2026-09-09 | FTFT | 11:58 | 66ba41eb-3175-5c7b-a969-ce440696732b | True | 44.61% | -6.43% | 43.00m | 37.44% |
| 2026-09-09 | FTFT | 13:31 | 5ac4c150-b460-54be-92e6-1d3521ac613a | False | 0.12% | -10.79% | 136.00m | 26.86% |
| 2026-09-09 | FTFT | 14:37 | dc3df1d1-ef11-524f-ba0a-013d922a13de | False | 0.81% | -6.94% | 202.00m | 29.29% |
| 2026-09-09 | SUNE | 11:34 | 74833f70-50ae-50fa-b254-60b7e220e5be | False | 1.62% | -5.68% | 53.00m | 5.76% |
| 2026-09-09 | YMAT | 09:20 | b0b75407-2a08-5bc8-9887-bc5af25ba731 | False | 1.59% | -4.23% | 313.00m | 28.89% |
| 2026-09-10 | TNON | 07:48 | 31489e8e-ded6-51f3-ab95-a1f8286c6793 | True | 40.27% | -0.01% | 201.00m | 10.61% |
| 2026-09-11 | FTFT | 12:02 | 4851bac9-88e3-5fa5-84b3-75807d3e6a51 | True | 12.44% | -2.91% | 144.00m | 18.97% |
| 2026-09-11 | FTFT | 14:08 | 8e0fa671-8c87-5de6-8df2-ae82ea4ae934 | False | -0.02% | -6.42% | 270.00m | 14.70% |
| 2026-09-11 | FTFT | 15:11 | 6da3a185-4be2-5b78-9e72-7cbe341f8fa9 | False | 2.51% | -3.18% | 333.00m | 13.67% |
| 2026-09-14 | BMGL | 15:01 | 2ac06f3f-9ca8-5c06-9f30-9b1d003db7d7 | False | 0.65% | -8.44% | 309.00m | 18.87% |
| 2026-09-14 | FTFT | 12:22 | 62463789-45ad-5115-a3d9-405b3a0d8c00 | True | 17.20% | -1.78% | 140.00m | 5.44% |
| 2026-09-14 | FTFT | 14:42 | 137e1f81-6fd0-531f-aa8b-7a238e343bca | True | 63.45% | -0.86% | 48.00m | 7.38% |
| 2026-09-15 | MYSZ | 09:34 | 7b79275b-8246-5e4b-8c86-eb76c63e7ee6 | False | 1.00% | -12.97% | 327.00m | 26.89% |
| 2026-09-15 | MYSZ | 12:08 | 5e49300f-1fa3-5ba0-9bb6-d28d35318407 | False | 0.79% | -10.46% | 481.00m | 28.85% |
| 2026-09-15 | MYSZ | 12:51 | ec5ebefc-5be6-55f2-95eb-7f0841fde85c | False | 0.04% | -5.98% | 524.00m | 30.21% |
| 2026-09-15 | VEEA | 11:47 | cf44421d-7fb5-5732-b2f3-5465866a871a | True | 27.38% | -6.29% | 102.00m | 14.22% |
| 2026-09-15 | VEEA | 13:35 | 25be6cab-324e-5aab-8597-ca23cad2a7ba | False | -0.00% | -9.79% | 66.00m | 22.16% |
| 2026-09-15 | VEEA | 14:19 | 0c4fc6f9-6b97-5c42-b491-129115e8bb4c | True | 6.07% | -4.18% | 110.00m | 26.10% |
| 2026-09-15 | VEEA | 15:12 | 4b8313db-fa60-56f9-85a2-56ed76b87ee1 | True | 6.09% | -7.11% | 163.00m | 23.63% |

## Interpretation caveats

- Outcome entry is the BUY-flip bar close. Live resting entries occur at the ATR trail below that close, so +5% from the close is stricter than the live entry geometry.
- Integer features can collapse into only a few tie-preserving buckets. The feature table prints the actual bucket count next to every gradient.
- The canonical oracle has no bar-gap guard. Results describe the stored Schwab series as it exists; they do not prove continuity across a missing bar.

## Existing production facts

The scanner removes a confirmed name below +30% day change. While a name remains confirmed, feed-retention evaluation resets it active. This census changes neither behavior.
