# ATR Flip Hold Study: 2026-09-01

Fresh study: one entry at the first executable ask after each scanner-eligible ATR BUY bar closes. Scanner removal blocks new entries but does not close an existing position. Every position exits by its policy, the next ATR SELL flip, or 16:00 ET.

ATR signals use the live gap-aware `SchwabV2Strategy._update_atr_state` implementation. There is no resting, reclaim, reactive, or prior bracket state in this population.

## Answer From This Session

There were **14 executable BUY flips**. Waiting for the next ATR SELL produced **3 winners and 11 losers**, with -1.4121 total percentage points and a -1.9228% median return.

The tape reached +5% on **9/14** entries, +8% on **6/14**, and +10% on **4/14**. The strongest tested structure sold 50% at +5%, then applied a 2% trail with a 0% earned floor to the remainder. It produced 9 winners and 5 losers, +32.5720 total points, +2.3266% mean, +4.1536% median, and 2.9778 profit factor.

This supports scaling at +5% and protecting the remainder; it does **not** support simply waiting for the SELL flip. It is one session with 14 entries, so it is evidence for the next test, not proof of a durable rule.

## Every ATR Entry

| Symbol | BUY ET | Entry ET | Entry | SELL ET | SELL return | MFE | MAE | +5 | +8 | +10 | 50%@5 + trail return | Trail exit |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|---:|---|
| BIAF | 09:03 | 09:03:00 | 6.7100 | 10:03 | -0.894% | +28.167% | -4.322% | Y | Y | Y | +5.108% | trail 09:18:04 |
| BIAF | 11:32 | 11:32:00 | 6.6000 | 11:53 | -3.030% | +7.879% | -4.242% | Y | N | N | +5.303% | trail 11:41:34 |
| BIAF | 12:43 | 12:43:00 | 6.5900 | 13:53 | -1.214% | +6.980% | -4.097% | Y | N | N | +4.169% | trail 13:33:19 |
| BIAF | 14:50 | 14:50:01 | 6.8400 | 15:55 | -4.532% | +1.023% | -4.678% | N | N | N | -4.532% | atr_sell 15:55:03 |
| FLYE | 11:38 | 11:38:00 | 1.8900 | 12:53 | +5.820% | +22.222% | -2.646% | Y | Y | Y | +8.320% | trail 12:10:43 |
| FLYE | 13:49 | 13:49:01 | 2.0600 | 15:18 | +6.796% | +17.961% | -1.942% | Y | Y | Y | +4.927% | trail 14:12:06 |
| RDAC | 13:47 | 13:47:00 | 6.4100 | 14:52 | -4.056% | +8.112% | -4.056% | Y | Y | N | +4.138% | trail 13:48:49 |
| RDAC | 15:06 | 15:06:00 | 6.5800 | - | -0.456% | +3.951% | -4.255% | N | N | N | -0.456% | session_close 15:59:56 |
| SSM | 08:52 | 08:52:00 | 3.8000 | 09:31 | -2.632% | +2.368% | -3.421% | N | N | N | -2.632% | atr_sell 09:31:00 |
| SSM | 09:38 | 09:38:00 | 4.0000 | 09:55 | -7.250% | +9.750% | -8.000% | Y | Y | N | +4.375% | trail 09:39:41 |
| SSM | 10:44 | 10:44:00 | 4.0900 | 10:49 | -5.623% | +1.956% | -6.846% | N | N | N | -5.623% | atr_sell 10:49:00 |
| SSM | 11:27 | 11:27:02 | 3.9200 | 12:46 | -0.510% | +5.612% | -1.531% | Y | N | N | +3.903% | trail 11:59:00 |
| SSM | 13:38 | 13:38:08 | 4.0300 | 13:55 | -3.226% | +0.000% | -3.226% | N | N | N | -3.226% | atr_sell 13:55:05 |
| SSM | 14:32 | 14:32:09 | 3.9700 | - | +19.395% | +24.937% | -2.519% | Y | Y | Y | +8.797% | trail 15:17:43 |

## Policy Comparison

| Policy | Wins | Losses | Total return | Mean | Median | Profit factor |
|---|---:|---:|---:|---:|---:|---:|
| `atr_sell_only` | 3 | 11 | -1.4121 | -0.1009% | -1.9228% | 0.9578 |
| `hold_stop-8` | 3 | 11 | -2.1621 | -0.1544% | -1.9228% | 0.9367 |
| `hold_stop-10` | 3 | 11 | -1.4121 | -0.1009% | -1.9228% | 0.9578 |
| `hold_stop-12` | 3 | 11 | -1.4121 | -0.1009% | -1.9228% | 0.9578 |
| `hold_stop-15` | 3 | 11 | -1.4121 | -0.1009% | -1.9228% | 0.9578 |
| `full_target+5_stop-10` | 9 | 5 | +28.5311 | +2.0379% | +5.0000% | 2.7324 |
| `full_target+8_stop-10` | 6 | 8 | +26.7766 | +1.9126% | -0.4831% | 2.2617 |
| `full_target+10_stop-10` | 4 | 10 | +7.4704 | +0.5336% | -1.9228% | 1.2297 |
| `scale0.4@+5_rest@+10_floor+2_stop-10` | 9 | 5 | +31.2912 | +2.2351% | +3.1525% | 2.9 |
| `scale0.5@+5_rest@+10_floor+2_stop-10` | 9 | 5 | +30.8311 | +2.2022% | +3.4604% | 2.8721 |
| `scale0.5@+5_trail2_floor+0_stop-10` | 9 | 5 | +32.5720 | +2.3266% | +4.1536% | 2.9778 |

The -10%, -12%, and -15% hard stops were never exercised before ATR SELL or 16:00. The -8% stop fired once and slightly worsened the aggregate result through next-quote slippage. This session therefore cannot choose among the wide stops.

## Population Census

| Symbol | Windows | Bars | Quotes | BUY flips | Eligible | Trades | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| AEHL | 1 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| BIAF | 1 | 470 | 146287 | 4 | 4 | 4 | EVALUATED |
| FLYE | 3 | 536 | 161951 | 4 | 2 | 2 | EVALUATED |
| GYGY | 1 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| KITT | 2 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| LABT | 1 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| LIDR | 4 | 245 | 111905 | 2 | 0 | 0 | NO_ELIGIBLE_BUY_FLIP |
| NCRA | 1 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| NWGL | 5 | 73 | 3385 | 0 | 0 | 0 | NO_BUY_FLIP |
| OLOX | 3 | 36 | 14165 | 0 | 0 | 0 | NO_BUY_FLIP |
| PETZ | 6 | 145 | 8093 | 0 | 0 | 0 | NO_BUY_FLIP |
| RDAC | 3 | 339 | 58152 | 2 | 2 | 2 | EVALUATED |
| SSM | 2 | 539 | 56320 | 6 | 6 | 6 | EVALUATED |
| UPC | 3 | 0 | 0 | 0 | 0 | 0 | NO_BARS |
| WETO | 2 | 164 | 8178 | 1 | 0 | 0 | NO_ELIGIBLE_BUY_FLIP |

All 14 accepted BUY decisions consumed adjacent one-minute bars. Earlier gaps existed in several symbols' Wilder history and were handled by the live true-range gap guard.

## Interpretation

The main failure mode is profit giveback, not an overly tight hard stop. For example, the first BIAF entry reached +28.17% but exited at -0.89% on ATR SELL; an SSM entry reached +9.75% but exited at -7.25%; and RDAC reached +8.11% but exited at -4.06%. Scaling at +5% directly addresses that observed mechanism.

The result still leaves five losing entries in one day, so exit design alone does not meet the goal of roughly five good trades for one bad trade. Entry filtering remains necessary after this exit structure is validated over additional sessions.
