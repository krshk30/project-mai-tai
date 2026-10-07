# RETAIN1 — data retention for project-mai-tai (approved by the operator 2026-10-07 ~17:00 ET)

Written by claude-1. Builder: codex-2. Measured on the box 10-07 16:5x ET (`pg_stat_user_tables`, `pg_total_relation_size`, existing timers).

## 1. The rule, in the operator's words
"Keep the trades, P&L, everything I need every day. Anything we store only to calculate something — bars, ticks, path prints — is plumbing; purge it every week, it doesn't matter. You make the call." Approved as proposed below.

## 2. What is on the disk today

| Table | Size | Rows | What it holds | Existing prune |
|---|---|---|---|---|
| market_capture_trades | 7,072 MB | 30.6 M | raw trade tape (READ C captures) since 09-23 | 14 days (prune-capture timer, 05:30 ET daily) |
| market_capture_quotes | 2,855 MB | 14.1 M | raw quote tape since 09-23 | 14 days (same timer) |
| momentum_paper_events | 1,379 MB | 1.30 M | paper bot events; 1.29 M are PATH_PRINT (per-second path prints), 1.5 k FEED_GAP, ~250 real trade events | none |
| market_trade_ticks | 1,248 MB | 2.03 M | gateway trade ticks since 09-08 | 30 days (prune-ticks timer, 05:00 ET daily) |
| strategy_bar_history | 1,052 MB | 1.35 M | 1-minute bars per watched symbol since 04-24 | none |
| reconciliation_findings | 997 MB | 1.46 M | reconciler detections (no consumer acts on them) | none |
| market_quote_ticks | 661 MB | 0.93 M | gateway quote ticks | 30 days |
| dashboard_snapshots | 254 MB | 30 k | durable trade state: ownership, fan-out outcomes, retry budget, hand-offs, holds | none |
| scanner_confirmed_events | 146 MB | 574 k | scanner cycle history | none |
| broker_order_events | 113 MB | 126 k | every broker status event | none |
| reconciliation_runs | 90 MB | 284 k | reconciler run log | none |
| trade_intents | 86 MB | 154 k | every intent the bots emitted | none |
| broker_orders | 67 MB | 84 k | every order at either broker | none |
| risk_checks | 66 MB | 154 k | risk decisions per intent | none |
| fills | 27 MB | 20 k | every fill, with price | none |
| account_positions | 17 MB | 1.2 k | broker position snapshot | none |

Disk: 30 of 116 GB used (26%). Growth drivers: capture tape (~0.7 GB/day), PATH_PRINT (~460 k rows ≈ 0.5 GB/day since 10-06), ticks (~60 MB/day), bars (~6 MB/day).

## 3. The decision

| Class | Tables | Retention | Why |
|---|---|---|---|
| **Trades — keep forever** | fills, broker_orders, broker_order_events, trade_intents, risk_checks, oms_managed_positions, account_positions, dashboard_snapshots (trade state), momentum_paper_events rows of type FILLED / EXITED / FINAL / DETECTED | never purged | The record of what happened and why; every page and every day ledger reads these. |
| **Calculation tape — weekly** | market_capture_trades, market_capture_quotes, market_capture_bars, market_trade_ticks, market_quote_ticks | keep 7 days (from 14 / 30) | Used for studies and after-the-fact reads; a week covers every scan and incident read. |
| **Detections nobody acts on — weekly** | reconciliation_findings, reconciliation_runs, scanner_cycle_history, scanner_confirmed_events | keep 7 days | Operational noise after a week. |
| **Plumbing — daily** | momentum_paper_events rows of type PATH_PRINT and FEED_GAP | keep 1 day; and assess stopping the per-second PATH_PRINT writer (sampled or off) | 1.29 M rows in two days for a paper observer's internal path. |
| **Bars — 45 days** | strategy_bar_history | keep 45 days, weekly purge beyond | The ATR seed needs ~1 session (250 bars); LINESRC1 repair needs today; the pullback-scalping study needs 30 sessions. 45 days ≈ 250 MB. |

## 4. Mechanics (codex)
- `scripts/prune_market_ticks.py` already exists and is driven by two timers (`prune-ticks` 05:00 ET, `prune-capture` 05:30 ET). Extend it: a table list with a `--keep-days` per table, a `--where "event_type in (...)"` filter for the paper table, batches with a row cap per run, `VACUUM (ANALYZE)` after. Add one weekly timer (Sunday 05:45 ET) for the 7-day and 45-day classes; the daily timer handles PATH_PRINT/FEED_GAP.
- First run of the new rule will delete roughly: capture tape ~5 GB, ticks ~1.5 GB, PATH_PRINT ~1.3 GB, reconciliation ~1 GB, bars ~0.8 GB. Run the first purge after the close, in batches, and report the before/after sizes.
- PATH_PRINT writer: Step 0 decides sampled (e.g. once per 10 s) or off; the paper trade rows are unaffected either way.

## 5. Must not break
- db-seed at (re)start: 250 bars per symbol (within 45 days ✔).
- LINESRC1 session repair: today's bars ✔.
- The 30-min scan's bar replay (today) and the pullback study export (30 sessions) ✔ within 45 days.
- The ATR / ORB pages: read trade tables only ✔.
- The prune must never touch the "keep forever" tables; a dry-run count per table is printed before each delete.

## 6. Not read
Whether any backtest or study job references captures older than 7 days (the backtest engine is parked by the operator's "stability first" ruling); the exact PATH_PRINT consumer (codex confirms in Step 0 before switching it off).
