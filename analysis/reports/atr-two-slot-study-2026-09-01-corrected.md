# September 1 ATR Two-Slot Study (Corrected)

Policy: production V2 entry lifecycle, research exit at +1% target / -2% floor. Entry hours are
07:00-16:00 ET and entries exist only inside captured MACD scanner CONFIRM windows.

## Entry Contract

Each ATR segment has at most two economic slots:

1. `first`: the ATR-line resting entry.
2. `reclaim`: the later segment-high entry, after the first position has closed.

The current deployed V2 implementation price-commits the RTH reclaim as a broker-resting order when
possible and retains reactive execution as fallback. Therefore `reclaim/resting` and
`reclaim/reactive` are two execution styles for the same second slot, not separate trade slots.

The prior replay incorrectly allowed a modeled early exit to reopen `first`. That row is withdrawn.
The corrected population is 18 trades across 12 segments: six one-entry segments, six two-entry
segments, and zero slot violations.

## Summary

| Trades | Wins | Losses | Sum return | Mean return | Slot violations |
|---:|---:|---:|---:|---:|---:|
| 18 | 6 | 12 | -20.3544 pts | -1.1308% | 0 |

## Trades

| # | Symbol | Segment ET | Slot | Execution | Entry ET | Entry | Exit ET | Exit | Reason | Return | Hold |
|---:|---|---|---|---|---|---:|---|---:|---|---:|---:|
| 1 | BIAF | 08:59 | first | resting | 09:00:26.214 | 6.7000 | 09:01:40.464 | 6.5500 | floor | -2.239% | 74.3s |
| 2 | BIAF | 08:59 | reclaim | reactive | 09:07:47.323 | 6.8200 | 09:08:23.012 | 6.8882 | target | +1.000% | 35.7s |
| 3 | BIAF | 10:05 | first | resting | 11:31:53.572 | 6.6000 | 11:33:19.820 | 6.6660 | target | +1.000% | 86.2s |
| 4 | BIAF | 10:05 | reclaim | resting | 11:34:09.703 | 6.8000 | 11:34:23.032 | 6.6500 | floor | -2.206% | 13.3s |
| 5 | BIAF | 11:55 | first | resting | 12:39:33.085 | 6.5300 | 12:39:40.873 | 6.4200 | floor | -1.685% | 7.8s |
| 6 | BIAF | 11:55 | reclaim | resting | 12:47:38.131 | 6.6200 | 12:47:48.436 | 6.5100 | floor | -1.662% | 10.3s |
| 7 | BIAF | 13:55 | first | resting | 14:30:13.361 | 6.7500 | 14:33:35.229 | 6.6100 | floor | -2.074% | 201.9s |
| 8 | BIAF | 13:55 | reclaim | resting | 15:06:47.347 | 6.8500 | 15:07:10.695 | 6.7100 | floor | -2.044% | 23.3s |
| 9 | FLYE | 11:33 | first | resting | 11:37:54.135 | 1.8900 | 11:38:07.693 | 1.8500 | floor | -2.116% | 13.6s |
| 10 | FLYE | 11:33 | reclaim | resting | 11:41:13.048 | 1.8900 | 11:43:23.232 | 1.8500 | floor | -2.116% | 130.2s |
| 11 | FLYE | 12:55 | reclaim | resting | 13:57:53.300 | 2.1000 | 13:59:40.901 | 2.0500 | floor | -2.381% | 107.6s |
| 12 | RDAC | 15:05 | reclaim | resting | 15:38:51.958 | 6.7500 | 15:38:53.697 | 6.4800 | floor | -4.000% | 1.7s |
| 13 | SSM | 08:51 | reclaim | reactive | 09:25:20.136 | 3.8400 | 09:26:56.187 | 3.8784 | target | +1.000% | 96.1s |
| 14 | SSM | 09:33 | first | resting | 09:38:12.723 | 3.9600 | 09:38:53.112 | 3.9996 | target | +1.000% | 40.4s |
| 15 | SSM | 09:33 | reclaim | reactive | 09:48:02.717 | 4.3600 | 09:48:04.359 | 4.2700 | floor | -2.064% | 1.6s |
| 16 | SSM | 09:57 | first | resting | 10:43:50.601 | 4.0600 | 10:44:26.866 | 4.1006 | target | +1.000% | 36.3s |
| 17 | SSM | 10:51 | reclaim | resting | 11:30:42.528 | 3.9600 | 11:30:44.487 | 3.8900 | floor | -1.768% | 2.0s |
| 18 | SSM | 13:58 | reclaim | resting | 14:58:19.154 | 3.9900 | 15:00:15.388 | 4.0299 | target | +1.000% | 116.2s |

The machine-readable source is
`atr-bracket-study-2026-09-01-to-2026-09-01-trades.csv`, which includes the stable segment identity
used for the cap audit.
