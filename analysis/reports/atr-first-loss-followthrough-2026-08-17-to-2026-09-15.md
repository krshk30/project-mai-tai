# First ATR loss and rest-of-day follow-through

Range: 2026-08-17 through 2026-09-15, ET. Read-only; no scanner or trading changes.

## Population

| Measure | Result | Denominator |
|---|---:|---|
| Confirmed symbol-days | 341 | distinct day/name pairs with CONFIRM |
| Bar-measurable | 213/341 | confirmed symbol-days |
| No live Schwab bars | 128/341 | confirmed symbol-days |
| At least one eligible real BUY flip | 80/213 | bar-measurable symbol-days |
| No eligible BUY flip | 133/213 | bar-measurable symbol-days |
| Eligible BUY opportunities | 249 | 80 symbol-days with a flip |

An eligible flip is a canonical ATR BUY bar that began and closed while the symbol was in one scanner-confirmed membership window, and whose bar began after the watch start. Intrabar touches with no BUY close are false flips and do not enter the denominator.

## Literal first eligible BUY

| Outcome | Count | Denominator |
|---|---:|---|
| ATR_SELL | 20/80 | symbol-days with an eligible BUY flip |
| STOP_8 | 19/80 | symbol-days with an eligible BUY flip |
| TARGET_5 | 38/80 | symbol-days with an eligible BUY flip |
| UNKNOWN_BAR_GAP | 1/80 | symbol-days with an eligible BUY flip |
| UNKNOWN_INTRABAR_ORDER | 1/80 | symbol-days with an eligible BUY flip |
| UNKNOWN_NO_FUTURE_BAR | 1/80 | symbol-days with an eligible BUY flip |

## First decisive +5/-8 outcome (secondary)

This descriptive view skips known ATR-sell outcomes until the first +5/-8 result, matching the operator's wording and bringing MYSZ's 09:34 ET stop into view. It never skips an UNKNOWN. The pre-registered verdict below still uses the literal first eligible BUY and was not rewritten after results were seen.

| Outcome | Count | Denominator |
|---|---:|---|
| STOP_8 | 23/68 | symbol-days with a gradable first decisive outcome |
| TARGET_5 | 45/68 | symbol-days with a gradable first decisive outcome |
| First decisive STOP_8 with a later BUY | 16/23 | first-decisive-stop symbol-days |
| Median guaranteed upside before the stop | 1.02% | 23/23 first-decisive-stop rows |
| Guaranteed upside >= +3% before the stop | 5/23 | first-decisive-stop rows with measurable MFE |
| Guaranteed upside >= +4% before the stop | 2/23 | first-decisive-stop rows with measurable MFE |
| Later +5 targets after first decisive STOP_8 | 21/53 | gradable later opportunities |
| Days with any later +5 target | 11/16 | first-decisive-stop days with a later BUY |
| Later equal-weight return sum | -36.59 pts | 53 gradable later opportunities |

## Literal first -8% loss: upside before and after

The guaranteed MFE excludes the stop bar because one-minute OHLC cannot tell whether that bar's high came before or after its low. The possible MFE includes it. This prevents a +3% or +4% print after the stop from being reported as pre-stop opportunity.

| Measure | Result | Denominator |
|---|---:|---|
| First trade stopped -8% | 19/80 | symbol-days with an eligible BUY flip |
| Median guaranteed pre-stop MFE | 1.32% | 19/19 first-stop rows |
| Median possible pre-stop MFE | 1.32% | 19/19 first-stop rows |
| Guaranteed pre-stop MFE >= +1% | 11/19 | first-stop rows with measurable MFE |
| Guaranteed pre-stop MFE >= +2% | 7/19 | first-stop rows with measurable MFE |
| Guaranteed pre-stop MFE >= +3% | 4/19 | first-stop rows with measurable MFE |
| Guaranteed pre-stop MFE >= +4% | 1/19 | first-stop rows with measurable MFE |
| Possible pre-stop MFE >= +1% | 11/19 | first-stop rows with measurable MFE |
| Possible pre-stop MFE >= +2% | 7/19 | first-stop rows with measurable MFE |
| Possible pre-stop MFE >= +3% | 4/19 | first-stop rows with measurable MFE |
| Possible pre-stop MFE >= +4% | 1/19 | first-stop rows with measurable MFE |
| Reached original +5% after the stop | 6/10 | first-stop rows with complete negative evidence or a visible recovery |
| Post-stop recovery UNKNOWN | 9/19 | first-stop symbol-days |

## What happened later that day

| First outcome | Symbol-days | Days with later flip | Later opportunities | Later targets | Later stops | ATR sells | 16:00 closes | UNKNOWN | Target rate | Return sum | Days with later target |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| STOP_8 | 19 | 13/19 | 42 | 17/41 | 7/41 | 14/41 | 3/41 | 1/42 | 41.46% | -31.27 pts | 8/13 |
| TARGET_5 | 38 | 33/38 | 74 | 28/72 | 16/72 | 23/72 | 5/72 | 2/74 | 38.89% | -90.81 pts | 21/33 |
| ATR_SELL | 20 | 14/20 | 53 | 17/48 | 7/48 | 22/48 | 2/48 | 5/53 | 35.42% | -39.88 pts | 10/14 |
| SESSION_CLOSE | 0 | 0/0 | 0 | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | UNKNOWN% | UNKNOWN pts | 0/0 |
| UNKNOWN_BAR_GAP | 1 | 0/1 | 0 | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | UNKNOWN% | UNKNOWN pts | 0/0 |
| UNKNOWN_INTRABAR_ORDER | 1 | 0/1 | 0 | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | UNKNOWN% | UNKNOWN pts | 0/0 |
| UNKNOWN_NO_FUTURE_BAR | 1 | 0/1 | 0 | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | UNKNOWN% | UNKNOWN pts | 0/0 |

## Pre-registered rule test

A block-after-first-stop rule passes only with at least 30 gradable later opportunities across at least 20 first-stop symbol-days, negative equal-weight later return, and negative return after dropping every one symbol and every one session day.

| Later gradable | First-stop days with later flip | Return | Drop-one symbol | Drop-one day | Verdict |
|---:|---:|---:|---|---|---|
| 41/42 | 13/19 | -31.27 pts | True | True | FAIL |

## First-stop detail

| Day | Symbol | First BUY ET | Stop ET | Entry | Guaranteed MFE | Possible MFE | Post-stop day MFE | Original +5 later | Later flips | Later outcomes | Later return | Bar gaps |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---:|
| 2026-08-18 | AIXC | 12:14 | 12:17 | 1.6500 | 4.24% | 4.24% | 16.97% | True | 0 | none | 0.00 pts | 121 |
| 2026-08-19 | YJ | 09:59 | 10:03 | 4.9000 | 1.02% | 1.02% | 0.00% | False | 5 | 10:50 TARGET_5, 11:41 ATR_SELL, 12:48 STOP_8, 13:40 STOP_8, 14:52 TARGET_5 | -13.32 pts | 0 |
| 2026-08-20 | PCLA | 15:35 | 15:41 | 9.3000 | 2.15% | 2.15% | -2.04% | None | 0 | none | 0.00 pts | 138 |
| 2026-08-20 | SGLY | 08:09 | 08:20 | 6.9700 | 2.73% | 2.73% | 2.01% | None | 0 | none | 0.00 pts | 0 |
| 2026-08-24 | BTCT | 09:26 | 09:31 | 1.6805 | 0.54% | 0.54% | 63.64% | True | 6 | 10:01 TARGET_5, 11:27 STOP_8, 12:05 TARGET_5, 13:28 TARGET_5, 14:47 ATR_SELL, 15:46 ATR_SELL | -2.71 pts | 10 |
| 2026-08-24 | XPON | 09:33 | 09:34 | 8.7400 | 0.00% | 0.69% | 5.95% | True | 3 | 10:55 TARGET_5, 13:31 ATR_SELL, 14:51 TARGET_5 | 6.26 pts | 25 |
| 2026-08-26 | CRE | 08:57 | 09:01 | 7.0091 | 1.72% | 1.72% | 22.70% | True | 4 | 09:30 TARGET_5, 12:19 ATR_SELL, 12:49 TARGET_5, 15:24 TARGET_5 | 10.75 pts | 0 |
| 2026-08-26 | XPON | 11:36 | 12:01 | 9.5800 | 0.00% | 0.00% | -1.88% | False | 3 | 13:33 ATR_SELL, 14:47 TARGET_5, 15:51 TARGET_5 | 7.86 pts | 21 |
| 2026-08-27 | PPCB | 09:32 | 09:34 | 4.1000 | 0.73% | 0.73% | -3.17% | False | 4 | 10:59 STOP_8, 13:52 ATR_SELL, 14:15 ATR_SELL, 15:10 SESSION_CLOSE | -17.03 pts | 0 |
| 2026-08-31 | NCRA | 12:24 | 12:26 | 3.0450 | 3.45% | 3.45% | 4.11% | None | 3 | 14:35 STOP_8, 15:11 STOP_8, 15:56 SESSION_CLOSE | -18.78 pts | 218 |
| 2026-08-31 | WETO | 12:20 | 12:30 | 10.7000 | 0.00% | 0.00% | 10.93% | True | 1 | 12:46 TARGET_5 | 5.00 pts | 143 |
| 2026-09-01 | RDAC | 13:48 | 14:51 | 6.7184 | 3.60% | 3.60% | 2.85% | None | 1 | 15:05 UNKNOWN_BAR_GAP | 0.00 pts | 48 |
| 2026-09-02 | LHAI | 14:46 | 15:04 | 1.1700 | 0.00% | 0.00% | -5.98% | None | 0 | none | 0.00 pts | 235 |
| 2026-09-02 | PPBT | 10:19 | 10:26 | 2.4801 | 1.61% | 1.61% | -1.21% | None | 4 | 12:37 ATR_SELL, 13:10 ATR_SELL, 14:05 ATR_SELL, 14:52 ATR_SELL | -9.45 pts | 106 |
| 2026-09-03 | MIMI | 09:52 | 09:56 | 1.0793 | 0.99% | 0.99% | 0.06% | None | 1 | 10:50 STOP_8 | -8.00 pts | 0 |
| 2026-09-04 | CDTG | 11:40 | 12:07 | 1.4300 | 3.49% | 3.49% | 2.10% | False | 3 | 13:03 ATR_SELL, 13:35 TARGET_5, 15:02 SESSION_CLOSE | -3.90 pts | 14 |
| 2026-09-08 | BNC | 09:56 | 10:04 | 5.3000 | 1.32% | 1.32% | 5.28% | True | 4 | 10:59 TARGET_5, 12:11 TARGET_5, 13:24 ATR_SELL, 14:39 TARGET_5 | 12.04 pts | 100 |
| 2026-09-15 | IPW | 13:22 | 13:33 | 3.1800 | 2.20% | 2.20% | -3.14% | None | 0 | none | 0.00 pts | 56 |
| 2026-09-15 | SUGP | 09:44 | 09:48 | 1.2200 | 0.00% | 0.00% | -5.74% | None | 0 | none | 0.00 pts | 0 |

## First-decisive-stop detail (secondary)

| Day | Symbol | Decisive trade # | Earlier known outcomes | Stop BUY ET | Stop ET | Guaranteed MFE | Later flips | Later targets | Later outcomes | Later return |
|---|---|---:|---|---:|---:|---:|---:|---:|---|---:|
| 2026-08-18 | AIXC | 1 | none | 12:14 | 12:17 | 4.24% | 0 | 0 | none | 0.00 pts |
| 2026-08-18 | XOS | 2 | ATR_SELL | 09:31 | 09:34 | 0.21% | 5 | 1 | 10:30 TARGET_5, 11:43 ATR_SELL, 13:15 ATR_SELL, 13:45 ATR_SELL, 14:45 SESSION_CLOSE | 1.74 pts |
| 2026-08-19 | YJ | 1 | none | 09:59 | 10:03 | 1.02% | 5 | 2 | 10:50 TARGET_5, 11:41 ATR_SELL, 12:48 STOP_8, 13:40 STOP_8, 14:52 TARGET_5 | -13.32 pts |
| 2026-08-20 | PCLA | 1 | none | 15:35 | 15:41 | 2.15% | 0 | 0 | none | 0.00 pts |
| 2026-08-20 | SGLY | 1 | none | 08:09 | 08:20 | 2.73% | 0 | 0 | none | 0.00 pts |
| 2026-08-24 | BTCT | 1 | none | 09:26 | 09:31 | 0.54% | 6 | 3 | 10:01 TARGET_5, 11:27 STOP_8, 12:05 TARGET_5, 13:28 TARGET_5, 14:47 ATR_SELL, 15:46 ATR_SELL | -2.71 pts |
| 2026-08-24 | XPON | 1 | none | 09:33 | 09:34 | 0.00% | 3 | 2 | 10:55 TARGET_5, 13:31 ATR_SELL, 14:51 TARGET_5 | 6.26 pts |
| 2026-08-26 | CRE | 1 | none | 08:57 | 09:01 | 1.72% | 4 | 3 | 09:30 TARGET_5, 12:19 ATR_SELL, 12:49 TARGET_5, 15:24 TARGET_5 | 10.75 pts |
| 2026-08-26 | XPON | 1 | none | 11:36 | 12:01 | 0.00% | 3 | 2 | 13:33 ATR_SELL, 14:47 TARGET_5, 15:51 TARGET_5 | 7.86 pts |
| 2026-08-26 | YYGH | 2 | ATR_SELL | 09:39 | 09:49 | 4.04% | 4 | 2 | 10:32 ATR_SELL, 11:30 ATR_SELL, 13:23 TARGET_5, 15:35 TARGET_5 | 1.93 pts |
| 2026-08-27 | PPCB | 1 | none | 09:32 | 09:34 | 0.73% | 4 | 0 | 10:59 STOP_8, 13:52 ATR_SELL, 14:15 ATR_SELL, 15:10 SESSION_CLOSE | -17.03 pts |
| 2026-08-31 | NCRA | 1 | none | 12:24 | 12:26 | 3.45% | 3 | 0 | 14:35 STOP_8, 15:11 STOP_8, 15:56 SESSION_CLOSE | -18.78 pts |
| 2026-08-31 | WETO | 1 | none | 12:20 | 12:30 | 0.00% | 1 | 1 | 12:46 TARGET_5 | 5.00 pts |
| 2026-09-01 | RDAC | 1 | none | 13:48 | 14:51 | 3.60% | 1 | 0 | 15:05 UNKNOWN_BAR_GAP | 0.00 pts |
| 2026-09-02 | LHAI | 1 | none | 14:46 | 15:04 | 0.00% | 0 | 0 | none | 0.00 pts |
| 2026-09-02 | PPBT | 1 | none | 10:19 | 10:26 | 1.61% | 4 | 0 | 12:37 ATR_SELL, 13:10 ATR_SELL, 14:05 ATR_SELL, 14:52 ATR_SELL | -9.45 pts |
| 2026-09-03 | MIMI | 1 | none | 09:52 | 09:56 | 0.99% | 1 | 0 | 10:50 STOP_8 | -8.00 pts |
| 2026-09-04 | CDTG | 1 | none | 11:40 | 12:07 | 3.49% | 3 | 1 | 13:03 ATR_SELL, 13:35 TARGET_5, 15:02 SESSION_CLOSE | -3.90 pts |
| 2026-09-08 | BNC | 1 | none | 09:56 | 10:04 | 1.32% | 4 | 3 | 10:59 TARGET_5, 12:11 TARGET_5, 13:24 ATR_SELL, 14:39 TARGET_5 | 12.04 pts |
| 2026-09-09 | SUNE | 3 | ATR_SELL, ATR_SELL | 13:31 | 14:14 | 0.13% | 0 | 0 | none | 0.00 pts |
| 2026-09-15 | IPW | 1 | none | 13:22 | 13:33 | 2.20% | 0 | 0 | none | 0.00 pts |
| 2026-09-15 | MYSZ | 2 | ATR_SELL | 09:34 | 09:57 | 1.00% | 3 | 1 | 11:22 TARGET_5, 12:08 STOP_8, 12:51 ATR_SELL | -8.98 pts |
| 2026-09-15 | SUGP | 1 | none | 09:44 | 09:48 | 0.00% | 0 | 0 | none | 0.00 pts |

## Limits

- Entry is the canonical BUY-flip bar close. Live resting fills occur near the pre-flip ATR trail and can differ by spread and the 0.5% stop-limit band. This full-month table tests signal sequence, not exact broker execution.
- Target and stop are first-touch on one-minute Schwab OHLC. A bar touching both is UNKNOWN. Exact tick order is never invented.
- The canonical oracle reproduces the stored Schwab series and has no pre-entry bar-gap guard. Once a modeled trade is open, the first missing minute makes its outcome UNKNOWN_BAR_GAP. Every symbol-day's gap count is preserved in the JSON and shown for first-stop rows.
- Percent returns are equal-weight percentage points, not dollars and not a portfolio simulation.
