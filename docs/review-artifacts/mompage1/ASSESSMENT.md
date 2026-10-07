# MOMPAGE1

## Step 0: AGREE

The two Momentum HTML pages inherit completed trades and daily P&L from the
running service snapshot. Those fields must instead come from the current
Eastern session's durable paper record; open positions and listening state
must remain on their existing snapshot path. Display-only: no bot, broker,
flag, label, other-page or service change.

Own bounded read on 2026-10-07 found 42 FILLED/EXITED/FINAL events representing
14 completed trades: momentum_30s 5, P&L 31.8044; momentum_60s 9, P&L 175.9955.
The events are joined by logical_id, never counted as three trades. A FILLED
row without an exit is not completed. FINAL retains its recorded P&L; a
FILLED/EXITED pair can reconstruct the same prices/quantity before FINAL.

Recorded fixture: `tests/fixtures/mompage1_20261007_terminal.json`, SHA256
`2e4f03672e33e620cd27bf581e10263bdda19c0858ed6410f3b0310008f56f6c`.
It contains the actual event keys, logical IDs, strategy/date/symbol/time
and the relevant payload fields, not invented trades or PATH_PRINT data.
Source: read-only SQL on mai-tai-vps `momentum_paper_events`, date 2026-10-07,
types FILLED/EXITED/FINAL, LIMIT 43 (42 returned).

## Cost

The new read is scoped by session_date, strategy_code and event_type, ordered
by observed_at/created_at/event_key, limited to 4,097 rows. More than 4,096
terminal rows refuses partial P&L rather than silently truncating it. It runs
in a worker thread, only for `/bot/momentum-30` and `/bot/momentum-60`.
The page gets a copy: shared cached snapshots and other pages remain unchanged.

Existing indexes include session_date, event_type, and
(session_date, strategy_code, observed_at); no migration is needed.
Own production EXPLAIN ANALYZE of both strategies' terminal read on 10-07:
42 returned, event_type index scan, 119 other terminal rows filtered,
execution 1.781 ms, no PATH_PRINT payload read. This is query cost, not
post-install page-load evidence.

Local TestClient, same 42-event fixture, 20 requests per page:

| Page | Before Median ms | After Median ms |
| --- | --- | --- |
| Momentum 30 | 0.532 | 1.138 |
| Momentum 60 | 0.551 | 2.007 |

Cold first Momentum 30 request was 46.191 ms before / 44.899 ms after.
These are local SQLite measurements, not production performance claims.
Production baseline, direct local HTTP, five sequential requests per page at
2026-10-07 20:16:58 UTC: median 30.128 ms (30), 25.070 ms (60).
Repeat this same read-only measurement after the reviewed install; no
production after result exists yet.

## Tests

184 focused tests passed: MOMPAGE1, control-plane, Momentum service and engine.
Ruff and diff checks passed. Recorded 14-trade replay covers service stopped,
running, reload stability, correct ET day, empty day, incomplete fill, no
PATH_PRINT/foreign-strategy/prior-day contamination, row bound, cached-snapshot
isolation and no history query for another page.

| Mutation Applied Alone | Result |
| --- | --- |
| Remove session filter | 1 failed |
| Remove strategy filter | 7 failed |
| Remove event-type filter | 1 failed |
| Group by event key instead of logical ID | 1 failed |
| Remove row-bound refusal | 1 failed |
| Skip history overlay | 5 failed |
| Mutate shared cached bot instead of copying | 4 failed |

All mutations restored; final focused rerun 184 passed. Full-unit suite and
independent pin are not claimed here.

## Install Boundary

Build only. No production files, configuration, database or runtime changed
for MOMPAGE1; no labels or other pages changed. Ship with the next reviewed
batch after pin. Only the control-plane display code needs loading; the
Momentum bot and daily guard do not need a restart. Verify the recorded 5/9
rows and P&L after 09:40 plus reload, and repeat the page-load measurement.
