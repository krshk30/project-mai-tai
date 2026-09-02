# Polygon 30-Second Paper Exit Harness

This is one deployable strategy swap inside the existing `polygon_30s` harness. It is not a new
service. Before implementation, create `archive/polygon-30s-pre-paper-exit-20260902` at the last
pre-swap commit and record SHA-256 hashes for `strategy_core/polygon_30s.py`, its strategy-engine
wiring, settings, and tests. Restore those files from the archive in a clean worktree and verify the
hashes before deleting any old rule code.

## 1. Reuse What Exists

| Existing part | Verified behavior and verb |
|---|---|
| Scanner | `project-mai-tai-strategy.service` **reads** `mai_tai:snapshot-batches`; `process_snapshot_batch` **executes** the alert and confirmed-scanner pipeline, then **passes** the retained promoted-name watchlist to `polygon_30s`. |
| Market data | The service **reads** `mai_tai:market-data`. `polygon_30s` **imports** `Polygon30sBarBuilderManager`, **executes** it on Polygon `TIMESALE_EQUITY` ticks, and **constructs** 30-second bars. Live aggregate bars are disabled; tick-built mode with historical hydration and live fallback is active. |
| Runtime | The existing systemd unit **executes** `mai-tai-strategy` from the repository, loads its environment file, and restarts after five seconds on failure. Live configuration has exactly one bot, `polygon_30s`, on `paper:polygon_30s` with simulated execution. The unit, scanner subscription, market-data subscription, and restart behavior remain. |
| Observability | The runtime **writes** startup, hydration, replay, decision, and error markers to `strategy.log`; it **publishes** heartbeat and full bot snapshots to `mai_tai:heartbeats` and `mai_tai:strategy-state`. |
| Screen | The control plane **reads** the latest strategy-state snapshot and persisted bars, its API **serves** `/botpolygon`, and its HTML route **renders** `/bot/30s-polygon`. That page remains the operator surface. |
| Database | The runtime **writes** 30-second OHLCV, indicators, and decision fields to `strategy_bar_history` when history persistence is enabled. The current generic rule path can also **publish** intents that the OMS turns into `trade_intents`, `broker_orders`, `fills`, and `virtual_positions`; the replacement must not invoke that path. |

## 2. Genuinely Missing

The harness lacks: the operator exit state machine (`+5%`, `-8%`, ATR SELL, then 16:00 in time
order); a read-only live-fill mirror; an independent resting-fill simulator; executable-bid quote
handling; duplicate and restart-safe paper identities; an append-only `paper_exit_runs` /
`paper_exit_positions` store; reconciliation of mirror entries against live fills; explicit
`MISSED_LIVE_ENTRY`, `PHANTOM_PAPER_ENTRY`, `NO_EXECUTABLE_BID`, and `UNANSWERABLE` markers; and two
separate dashboard panels with daily comparison rows. These pieces ship together or the strategy
swap remains documentation only.

## 3. Entry Coupling Decision

Run both arms, never pooled. The **primary mirror arm** consumes filled `order_event` records for
`schwab_1m_v2` BUY opens whose stamped metadata says first/resting. Its immutable identity is
`broker_fill_id`; price, quantity, and timestamp are copied exactly. A read-only fill-table
reconciler detects a dropped stream event and may restore the same fill later, marked `LATE_MIRROR`,
but may not invent one. Missing coupling fails closed: a live fill with no mirror creates
`MISSED_LIVE_ENTRY` and no paper position. The opposite direction is also closed: a mirror row must
reference an actual live broker fill, so an unmatched paper entry is `PHANTOM_PAPER_ENTRY`, excluded
and paged. Matched/live, missed/live, and phantom/paper denominators appear separately.

The **independent arm** receives the same promoted watchlist and bars, arms from the scanner, and
models a resting fill only from timestamped executable asks. It measures refused names and orders
that never caught, but entry and exit both differ from live and are therefore confounded. Its rows
are labelled `INDEPENDENT`; they never contribute to the mirror comparison.

## 4. Structural No-Order Boundary

Replace `Polygon30sEntryEngine` with a paper engine whose return type is `PaperDecision`, not
`TradeIntentEvent`, and give it only read-only market/fill repositories plus the dedicated paper
writer. It has no broker-adapter import, broker credential object, strategy-intent publisher, or
dynamic provider dispatch. At the enclosing service boundary, an allowlist accepts paper writes
only to the two paper tables and rejects any `polygon_30s` attempt to publish to
`mai_tai:strategy-intents`. The OMS allowlist independently rejects the paper identity. CI uses AST
and dispatch tests to prove the paper package cannot import adapters or construct/publish a trade
intent. A configuration flag or `paper:` account name is not counted as protection.

## 5. Forbidden Shared State

Neither arm consumes first/reclaim/fan-out slots, writes `virtual_positions` or
`oms_managed_positions`, polls a broker, shares live client-order or position keys, or calls the live
status poller. Paper identities are namespaced by arm plus live `broker_fill_id` or independent
attempt ID. The existing scanner, bars, history persistence, heartbeat, logs, and display snapshot
are the only shared machinery.

## 6. Grading And Surface

`/bot/30s-polygon` shows Mirror and Independent panels. Each entry displays source identity, entry,
first-trigger decision, paper exit, live realized exit when available, return delta, and evidence
status. The daily Mirror line is `matched and gradable / live resting fills`, followed by paper-rule
and live-realized totals on those same matched entries; missing, phantom, and unanswerable counts are
never folded into P&L. Independent gets its own denominator and no live comparison. Logs emit one
entry and one exit marker per paper identity, plus a daily reconciliation marker.

## 7. Limits And Falsifiers

The harness cannot know executable size until bid/ask size provenance is established, cannot infer a
fill inside a quote gap, cannot turn the independent arm into an entry comparison without accepting
confounding, and cannot grade a live position without a closing broker fill. The approach is
falsified if any mirror price or timestamp differs from its broker fill; a live resting fill is
silently absent; a paper-only entry appears in the mirror total; either arm reaches
`strategy-intents`, broker code, shared slot/state tables, or the live poller; restart changes an
existing paper decision; the two arms are pooled; or the screen reports a result without its stated
denominator and evidence status.
