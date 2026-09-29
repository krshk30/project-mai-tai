# Schwab tick coverage census (read-only)

Collected on 2026-09-29 at approximately 09:03 ET, while the 09-29 session was still in progress. The operator explicitly allowed this bounded, low-priority market-hours coverage read in protocol Amendment 1. No trading service, subscription, flag, or broker order was changed.

## Scope and result

The retained `market_trade_ticks` Schwab/LEVELONE_EQUITIES rows run from 2026-08-31 04:06:23.993 ET through the live 2026-09-29 session. Thus there are **21 retained trading sessions, 20 completed plus today's partial session**, not 30 retained sessions. The core denominator is **290 symbol-days with at least one persisted live v2 one-minute bar** (285 completed-session symbol-days). Of these, **112** had their first live bar before 09:30 ET (107 completed-session symbol-days). This is evidence of v2 bar processing, not a claim that every scanner confirmation reached v2.

For those 112 pre-market-first symbol-days:

| Evidence before 07:00 ET | Stock-days |
| --- | ---: |
| Any genuine field-35 print in 04:00-06:59 | 52 / 112 |
| At least 10 distinct printed minutes in 04:00-06:59 | 47 / 112 |
| Any genuine field-35 print in 06:00-06:59 | 46 / 112 |
| At least 10 distinct printed minutes in 06:00-06:59 (primary candidate coverage) | **42 / 112** |
| All 60 distinct printed minutes in 06:00-06:59 | 28 / 112 |

A 10-minute count is only a **candidate for seed input**, not proof of ATR parity, rest placement, an accepted order, a fill, or P&L. The 04:00 sensitivity adds five stock-days to the at-least-10-minute group versus the 06:00 primary start. Stock-days with no captured pre-07:00 print cannot be repaired from this *already captured* tick table. This does not prove Schwab had no trades; tick capture is subscription-dependent.

Across all 290 bar-evidenced symbol-days, there were 214,870 Schwab records in the 04:00-to-first-live-bar interval: 214,867 carried a matching positive field 35 and were received by the first bar plus 90 seconds. The three without field 35 are excluded. Sixty-seven symbol-days had at least one such pre-first-bar record, including post-07:00 windows for names that started later. The core pre-07:00 totals above deliberately do not treat those later prints as early history.

BKYI on 09-29 has 8,347 eligible pre-first-bar ticks, 173 distinct printed minutes from 04:06-06:59, and 60/60 from 06:00-06:59. This explains why it is a strong BKYI signal test, but it is **not representative of all names**. The separate [BKYI control](BKYI-CONTROL.md) established a 07:47 BUY signal with a tick seed; it did not establish a fill.

## Scanner cross-check

The scanner has 359 distinct CONFIRM symbol-days in this calendar window, including 17 weekend-only rows. On weekdays, 278 overlap the 290 v2-live-bar symbol-days, 64 have a scanner CONFIRM but **no v2 live bar**, and 12 have a v2 live bar but no scanner CONFIRM row. Of the 64 scanner-only weekdays, 59 have some genuine 04:00-06:59 Schwab prints, six have at least 10 printed minutes from 04:00, and two have at least 10 from 06:00. A scanner confirmation alone does not prove v2 watched the symbol or received a first CHART bar, so these 64 are **UNKNOWN for v2 seedability**, not silently counted as covered or uncovered. The 17 weekend scanner-only rows are not trading-session candidates. Watch-interval reconstruction remains required for the fill-aware study.

## Per-session census

| Session ET | V2 live-bar stock-days | First bar before 09:30 | Any 06:00-06:59 print | At least 10 06:00-06:59 minutes | At least 10 04:00-06:59 minutes |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-08-31 | 17 | 7 | 2 | 2 | 2 |
| 2026-09-01 | 11 | 5 | 3 | 3 | 3 |
| 2026-09-02 | 13 | 5 | 1 | 1 | 1 |
| 2026-09-03 | 6 | 3 | 0 | 0 | 0 |
| 2026-09-04 | 8 | 3 | 1 | 1 | 1 |
| 2026-09-08 | 8 | 4 | 2 | 1 | 2 |
| 2026-09-09 | 6 | 5 | 2 | 2 | 2 |
| 2026-09-10 | 9 | 2 | 1 | 1 | 1 |
| 2026-09-11 | 10 | 2 | 1 | 1 | 1 |
| 2026-09-14 | 9 | 3 | 1 | 1 | 1 |
| 2026-09-15 | 21 | 6 | 2 | 2 | 2 |
| 2026-09-16 | 17 | 6 | 4 | 3 | 3 |
| 2026-09-17 | 21 | 9 | 4 | 3 | 4 |
| 2026-09-18 | 15 | 8 | 3 | 3 | 5 |
| 2026-09-21 | 17 | 5 | 2 | 2 | 2 |
| 2026-09-22 | 16 | 5 | 2 | 2 | 2 |
| 2026-09-23 | 21 | 8 | 4 | 4 | 4 |
| 2026-09-24 | 23 | 8 | 4 | 3 | 4 |
| 2026-09-25 | 20 | 6 | 2 | 2 | 2 |
| 2026-09-28 | 17 | 7 | 2 | 2 | 2 |
| 2026-09-29* | 5 | 5 | 3 | 3 | 3 |

*09-29 is partial as of collection.

The complete 290-row [v2 bar-evidenced CSV](COVERAGE-V2-BAR-STOCK-DAYS.csv) and 64-row [scanner-only CSV](COVERAGE-SCANNER-ONLY.csv) retain the individual symbol-day counts. Primary-source tables: `strategy_bar_history`, `market_trade_ticks`, and `scanner_confirmed_events` in the production PostgreSQL database. Query bounds were 2026-08-31 04:00 ET through 2026-09-30 04:00 ET for bar population, each symbol-day 04:00 ET to its first live bar for ticks, and 04:00-07:00 ET for premarket-only coverage. Predicates: `strategy_code='schwab_1m_v2'`, `interval_secs=60`, `source='live'`; `provider='schwab'`, `service='LEVELONE_EQUITIES'`, positive raw field 35 equal to `event_ts` in milliseconds. A tick was timely only when `received_at <= first_live_bar + 90 seconds`. The query was `BEGIN READ ONLY`, `nice -n 19`, and statement-timeout bounded at 120 seconds (90 seconds for the scanner-only cross-check).

**Not yet measured:** live-vs-tick-seeded flip agreement across all stock-days, the Massive chart-proxy comparison, entry/exit replay, fills-based added/removed trades, and P&L. No live-seed build or enable decision follows from this coverage census.
