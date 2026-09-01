# September 1 ATR Trade Detail (Superseded)

> **WITHDRAWN:** this version contains the invalid second BIAF first-slot entry at 09:02 ET.
> Use the [corrected two-slot report](atr-two-slot-study-2026-09-01-corrected.md). The corrected
> replay has 18 trades and zero segments with more than one first plus one reclaim.

Policy: current live ATR entry logic, research exit at +1% target / -2% floor.

All times are America/New_York. A floor triggers on the first captured bid at or below the floor and
fills at the next captured bid. A target fills at its stated price when the bid reaches it.

## Summary

| Population | Trades | Targets | Floors | Sum return | Mean return |
|---|---:|---:|---:|---:|---:|
| September 1 | 19 | 6 | 13 | -22.4439 pts | -1.1813% |

| Symbol | Trades | Wins | Losses | Sum return | Mean return |
|---|---:|---:|---:|---:|---:|
| BIAF | 9 | 2 | 7 | -11.9983 pts | -1.3331% |
| SSM | 6 | 4 | 2 | +0.1681 pts | +0.0280% |
| FLYE | 3 | 0 | 3 | -6.6138 pts | -2.2046% |
| RDAC | 1 | 0 | 1 | -4.0000 pts | -4.0000% |

| Slot / execution mode | Trades | Wins | Losses | Sum return | Mean return |
|---|---:|---:|---:|---:|---:|
| First / resting | 8 | 3 | 5 | -7.2034 pts | -0.9004% |
| Reclaim / reactive | 3 | 2 | 1 | -0.0642 pts | -0.0214% |
| Reclaim / resting | 8 | 1 | 7 | -15.1763 pts | -1.8970% |

## Two-slot audit

The intended cap is two entries per ATR segment: one `first` resting slot and one `reclaim` slot.
`reactive` is an execution mode for reclaim, not a third slot.

The replay found 19 trades across 12 segment identities. Six segments produced one entry, five
produced the valid `first + reclaim` pair, and one BIAF segment produced three entries:

| Segment | Entry 1 | Entry 2 | Entry 3 | Verdict |
|---|---|---|---|---|
| BIAF 08:59 ET | 09:00:26 first/resting | 09:02:22 first/resting | 09:07:47 reclaim/reactive | breach: duplicate first slot |

This is not a report-label artifact. The first BIAF trade hit the modeled `-2%` floor at 09:01:40,
before the BUY arm bar closed. The live state transition consumed `cw_resting_taken`, but the
first-resting placement path does not read that consumed flag before placing another order. The
existing `+2% / -5%` exit can cover this gap by keeping the first position open through the arm;
the tighter research exit exposed it. No live strategy change was made in this study.

## Executions

`Trigger` and `Exit` are identical for target orders. For floor orders, their difference is the
modeled market-stop trigger-to-next-bid interval.

`Slot` is `first` or `reclaim`. `Mode` is `resting` or `reactive`; a reclaim can use either mode.

| # | Symbol | Slot | Mode | Entry ET | Entry | Target | Floor | Trigger ET | Exit ET | Exit | Result | Return | Hold |
|---:|---|---|---|---|---:|---:|---:|---|---|---:|---|---:|---:|
| 1 | BIAF | first | resting | 09:00:26.214 | 6.7000 | 6.7670 | 6.5660 | 09:01:40.447 | 09:01:40.464 | 6.5500 | floor | -2.239% | 74.3s |
| 2 | BIAF | first | resting | 09:02:22.382 | 6.7000 | 6.7670 | 6.5660 | 09:03:27.497 | 09:03:27.499 | 6.5600 | floor | -2.090% | 65.1s |
| 3 | BIAF | reclaim | reactive | 09:07:47.323 | 6.8200 | 6.8882 | 6.6836 | 09:08:23.012 | 09:08:23.012 | 6.8882 | target | +1.000% | 35.7s |
| 4 | SSM | reclaim | reactive | 09:25:20.136 | 3.8400 | 3.8784 | 3.7632 | 09:26:56.187 | 09:26:56.187 | 3.8784 | target | +1.000% | 96.1s |
| 5 | SSM | first | resting | 09:38:12.723 | 3.9600 | 3.9996 | 3.8808 | 09:38:53.112 | 09:38:53.112 | 3.9996 | target | +1.000% | 40.4s |
| 6 | SSM | reclaim | reactive | 09:48:02.717 | 4.3600 | 4.4036 | 4.2728 | 09:48:04.358 | 09:48:04.359 | 4.2700 | floor | -2.064% | 1.6s |
| 7 | SSM | first | resting | 10:43:50.601 | 4.0600 | 4.1006 | 3.9788 | 10:44:26.866 | 10:44:26.866 | 4.1006 | target | +1.000% | 36.3s |
| 8 | SSM | reclaim | resting | 11:30:42.528 | 3.9600 | 3.9996 | 3.8808 | 11:30:44.486 | 11:30:44.487 | 3.8900 | floor | -1.768% | 2.0s |
| 9 | BIAF | first | resting | 11:31:53.572 | 6.6000 | 6.6660 | 6.4680 | 11:33:19.820 | 11:33:19.820 | 6.6660 | target | +1.000% | 86.2s |
| 10 | BIAF | reclaim | resting | 11:34:09.703 | 6.8000 | 6.8680 | 6.6640 | 11:34:23.031 | 11:34:23.032 | 6.6500 | floor | -2.206% | 13.3s |
| 11 | FLYE | first | resting | 11:37:54.135 | 1.8900 | 1.9089 | 1.8522 | 11:38:07.693 | 11:38:07.693 | 1.8500 | floor | -2.116% | 13.6s |
| 12 | FLYE | reclaim | resting | 11:41:13.048 | 1.8900 | 1.9089 | 1.8522 | 11:43:23.232 | 11:43:23.232 | 1.8500 | floor | -2.116% | 130.2s |
| 13 | BIAF | first | resting | 12:39:33.085 | 6.5300 | 6.5953 | 6.3994 | 12:39:40.872 | 12:39:40.873 | 6.4200 | floor | -1.685% | 7.8s |
| 14 | BIAF | reclaim | resting | 12:47:38.131 | 6.6200 | 6.6862 | 6.4876 | 12:47:48.436 | 12:47:48.436 | 6.5100 | floor | -1.662% | 10.3s |
| 15 | FLYE | reclaim | resting | 13:57:53.300 | 2.1000 | 2.1210 | 2.0580 | 13:59:40.901 | 13:59:40.901 | 2.0500 | floor | -2.381% | 107.6s |
| 16 | BIAF | first | resting | 14:30:13.361 | 6.7500 | 6.8175 | 6.6150 | 14:33:35.229 | 14:33:35.229 | 6.6100 | floor | -2.074% | 201.9s |
| 17 | SSM | reclaim | resting | 14:58:19.154 | 3.9900 | 4.0299 | 3.9102 | 15:00:15.388 | 15:00:15.388 | 4.0299 | target | +1.000% | 116.2s |
| 18 | BIAF | reclaim | resting | 15:06:47.347 | 6.8500 | 6.9185 | 6.7130 | 15:07:10.559 | 15:07:10.695 | 6.7100 | floor | -2.044% | 23.3s |
| 19 | RDAC | reclaim | resting | 15:38:51.958 | 6.7500 | 6.8175 | 6.6150 | 15:38:51.969 | 15:38:53.697 | 6.4800 | floor | -4.000% | 1.7s |

## Entry Context

`Schwab vol` is the latest Schwab one-minute volume divided by the preceding five-bar average.
`Polygon vol` is the latest fully closed 30-second volume divided by its preceding 20-bar average.
MACD values are normalized by price.

| # | Symbol | Symbol entry # | Scanner age | Schwab vol | Polygon vol | From VWAP | MACD delta | Histogram | Spread |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | BIAF | 1 | 53.3m | 2.30x | 1.03x | +41.33% | +0.077% | +0.469% | 0.15% |
| 2 | BIAF | 2 | 55.3m | 1.13x | 1.94x | +41.33% | -0.019% | +0.406% | 0.30% |
| 3 | BIAF | 3 | 60.7m | 1.87x | 1.55x | +43.86% | +0.110% | +0.074% | 0.59% |
| 4 | SSM | 1 | 289.5m | 1.70x | 1.49x | +43.91% | -0.042% | -0.069% | 0.26% |
| 5 | SSM | 2 | 302.3m | 3.28x | 7.50x | +4.12% | +0.480% | +0.575% | 1.26% |
| 6 | SSM | 3 | 312.2m | 2.14x | 1.70x | +6.99% | +0.211% | +0.144% | 0.23% |
| 7 | SSM | 4 | 368.0m | 2.61x | 2.71x | +0.32% | +0.068% | +0.189% | 0.25% |
| 8 | SSM | 5 | 414.8m | 0.83x | 1.17x | -1.85% | -0.087% | +0.127% | 1.77% |
| 9 | BIAF | 4 | 204.8m | 2.24x | 1.89x | -9.05% | +0.026% | +0.160% | 1.06% |
| 10 | BIAF | 5 | 207.0m | 1.44x | 5.61x | -6.17% | +0.184% | +0.365% | 0.74% |
| 11 | FLYE | 1 | 14.8m | 2.52x | 0.53x | -16.50% | +0.122% | +0.161% | 0.53% |
| 12 | FLYE | 2 | 18.1m | 1.51x | 1.94x | -16.46% | +0.040% | +0.208% | 0.53% |
| 13 | BIAF | 6 | 272.4m | 4.59x | 4.15x | -8.90% | +0.160% | +0.272% | 0.92% |
| 14 | BIAF | 7 | 280.5m | 1.03x | 0.59x | -7.59% | +0.007% | +0.003% | 1.36% |
| 15 | FLYE | 3 | 154.7m | 2.98x | 0.79x | -5.84% | +0.045% | +0.053% | 0.48% |
| 16 | BIAF | 8 | 383.1m | 8.89x | 0.48x | -5.21% | +0.046% | +0.098% | 1.63% |
| 17 | SSM | 6 | 622.5m | 5.75x | 1.63x | -0.89% | -0.001% | +0.064% | 1.00% |
| 18 | BIAF | 9 | 419.7m | 4.87x | 0.57x | -3.72% | +0.063% | +0.033% | 1.02% |
| 19 | RDAC | 1 | 112.4m | 17.31x | 22.97x | -5.55% | +0.298% | +0.251% | 3.85% |

## Later Session Path

These columns ignore the modeled exit and continue observing bids until 16:00 ET. They answer
whether an exited entry later rallied, not whether it was a winner under the +1/-2 policy.

| # | Symbol | First bid | Later maximum | Later minimum | Reached +5 | Drawdown before +5 | Time to +5 |
|---:|---|---:|---:|---:|---|---:|---:|
| 1 | BIAF | -0.15% | +28.36% | -8.51% | yes | -4.18% | 17.0m |
| 2 | BIAF | -0.30% | +28.36% | -8.51% | yes | -4.18% | 15.1m |
| 3 | BIAF | -1.32% | +26.10% | -10.12% | yes | -3.23% | 9.9m |
| 4 | SSM | -0.26% | +29.17% | -4.43% | yes | -4.43% | 13.6m |
| 5 | SSM | -1.26% | +25.25% | -7.07% | yes | -1.52% | 1.0m |
| 6 | SSM | -0.23% | +13.76% | -15.60% | yes | -15.60% | 329.6m |
| 7 | SSM | -0.25% | +22.17% | -7.88% | yes | -7.88% | 273.6m |
| 8 | SSM | -1.77% | +25.25% | -4.04% | yes | -4.04% | 221.7m |
| 9 | BIAF | -1.06% | +7.88% | -6.82% | yes | -1.52% | 8.6m |
| 10 | BIAF | -0.74% | +4.71% | -9.56% | no | n/a | n/a |
| 11 | FLYE | -0.53% | +28.57% | -2.65% | yes | -2.65% | 30.2m |
| 12 | FLYE | -0.53% | +28.57% | -2.12% | yes | -2.12% | 26.9m |
| 13 | BIAF | -0.92% | +7.96% | -3.22% | yes | -3.22% | 52.7m |
| 14 | BIAF | -1.36% | +6.50% | -4.53% | yes | -4.53% | 56.0m |
| 15 | FLYE | -0.48% | +15.71% | -3.81% | yes | -3.81% | 13.8m |
| 16 | BIAF | -1.48% | +2.37% | -4.74% | no | n/a | n/a |
| 17 | SSM | -1.00% | +24.31% | -1.25% | yes | -1.25% | 14.2m |
| 18 | BIAF | -1.46% | +0.88% | -5.11% | no | n/a | n/a |
| 19 | RDAC | -4.00% | +1.33% | -4.44% | no | n/a | n/a |

## Immediate Observations

- SSM was the only net-positive symbol. Its four wins were trades 4, 5, 7, and 17.
- Reclaim/reactive was the strongest path at 2/3 wins and nearly flat total return.
- Reclaim/resting accounted for 7 of the 13 floor exits and -15.1763 points, the largest path-level cost.
- Nine BIAF entries produced only two winners. A per-symbol entry cap would have removed substantial
  churn, although which two BIAF trades won cannot be identified by entry count alone.
- FLYE lost all three bracket trades even though each later rallied more than 5%. Each first crossed
  the -2% neighborhood, so those are delayed reversals rather than +1/-2 winners.
- RDAC is the clearest rejection candidate: 3.85% entry spread, -4% first bid, and only +1.33% later
  maximum. Very high volume did not make it safe.
- High volume alone is insufficient. BIAF trade 16 had 8.89x Schwab volume and lost; RDAC had 17.31x
  and lost. Volume must be paired with execution quality and entry structure.

The companion CSV contains all 66 raw and derived fields, including UTC/ET timestamps, scanner
window, arm timestamp, MFE/MAE threshold timestamps, volume ratios, MACD, VWAP, and spread.
