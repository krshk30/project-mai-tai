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

All first-useful-day rows below are required and deploy together; intermediate rows are not useful
deployments. The estimate is 9-12 engineering days, or roughly two elapsed weeks including review,
replay, and an operator-approved deployment.

| Missing work | Rough effort | First useful day? |
|---|---:|---|
| Archive and hash-restore proof for the old rules | 0.5 day | Required |
| `PaperDecision` seam, structural no-order boundaries, and policy tests | 1.5-2 days | Required |
| All-venue fill mirror, exact duplicate collapse, restart recovery, and reconciliation | 1.5-2 days | Required |
| Operator exit state machine, executable-bid handling, and restart-safe state | 1.5-2 days | Required |
| Independent scanner/resting-fill arm, kept separate from Mirror | 1-1.5 days | Required |
| Append-only paper tables, screen panels, empty state, and daily grader | 1.5-2 days | Required |
| Replays, failure injection, deployment checklist, and first-session acceptance | 1.5-2 days | Required |

Size-qualified quote execution, richer multi-session charts, and alert-threshold tuning are
follow-ons at about 1-2 days each. They do not block bid-price grading or the first useful day.

## 3. Entry Coupling Decision

Run both arms, never pooled. The **primary mirror arm** intercepts every filled BUY-open
`order_event` before the existing strategy-code dispatch, because both venue legs retain
`strategy_code=schwab_1m_v2` and would otherwise never reach `polygon_30s`. A candidate is eligible
only when its stamped metadata has `cw_entry_slot=first` and one enumerated resting source:
`CW-v2-resting`, `rth_resting`, `rth_resting_mirror`, or `eh_resting`. `resting_entry=true` alone is
insufficient because reclaim carries it too; `reactive`, unknown, missing, or contradictory source
and slot combinations are `UNANSWERABLE`, never inferred. The filter applies regardless of venue
or live account.
The reconciled
82-entry reference population contains 106 broker-fill legs: 71 Webull and 35 Schwab, including two
sessions with no Schwab resting fill. Restricting Mirror to Schwab would therefore idle it on those
sessions and omit most fill legs.

Each source leg keeps its immutable database `fills.id`, nullable `broker_fill_id`, venue, price,
quantity, and timestamp. A missing `broker_fill_id` cannot be used as identity and is reported
`UNANSWERABLE`; it is never synthesized. Legs
become one logical paper position only when session, symbol, and non-empty `fanout_slot_id` match.
That identity handles both cross-broker pairs and same-broker duplicate fills without temporal
transitivity. The reviewed historical `106 -> 82` replay uses an immutable audited mapping only for
legacy fills that predate the slot stamp; the live runtime has no time-only fallback. An A-B-C timing
chain can therefore never merge separate entries. The reference contains 20 cross-broker pairs and
four duplicate Webull fills. The logical identity is derived from the slot plus sorted source fill
IDs; it retains every leg rather than synthesizing a fill, and grades the per-leg rule results with
actual-quantity weighting.

A read-only fill-table census is authoritative for the live denominator and runs independently of
Redis, whose consumer begins at `$` and can silently lose an event. It detects a dropped stream
event and may restore the same database fill later, marked `LATE_MIRROR`, but may not invent one.
Missing coupling fails closed: a live fill with no
mirror creates `MISSED_LIVE_ENTRY` and no paper position. The opposite direction is also closed: a
mirror row must reference actual live broker fills, so an unmatched paper entry is
`PHANTOM_PAPER_ENTRY`, excluded and paged. Report matched/live and missed/live legs separately for
Webull and Schwab, then logical matched/expected entries after collapse; report phantom paper rows
separately.

The **independent arm** receives the same promoted watchlist and bars, arms from the scanner, and
models a resting fill only from timestamped executable asks. It measures refused names and orders
that never caught, but entry and exit both differ from live and are therefore confounded. Its rows
are labelled `INDEPENDENT`; they never contribute to the mirror comparison.

## 4. Structural No-Order Boundary

Replace the whole `polygon_30s` `StrategyBotRuntime`, not merely `Polygon30sEntryEngine`, with a
dedicated paper runtime whose methods return only `PaperDecision` or paper state. It does not own
the generic runtime's `ExitEngine`, `PositionTracker`, or any method whose return type is
`TradeIntentEvent`. Give it only read-only market/fill repositories plus the dedicated paper writer.
It has no broker-adapter import, broker credential object, strategy-intent publisher, or dynamic
provider dispatch. At the enclosing service boundary, an allowlist accepts paper writes only to
the two paper tables and rejects any `polygon_30s` attempt to publish to
`mai_tai:strategy-intents`. The OMS independently rejects every intent whose
`strategy_code=polygon_30s`, regardless of account name, provider, or execution mode, before order
creation or adapter dispatch. CI uses AST
and dispatch tests to prove the paper package cannot import adapters or construct/publish a trade
intent. A configuration flag or `paper:` account name is not counted as protection.

The locked v1 exit rule is first trigger in timestamp order: `+target_pct`, `-stop_pct`, ATR SELL,
then 16:00 ET as the backstop. Initial values are `target_pct=5` and `stop_pct=8`; these are not code
constants. `paper_exit_rule_configs` is append-only and stores both values, `effective_at`, author,
and creation time. The operator updates it from the existing Polygon screen; the control plane
commits the new version and publishes a runtime-control event, so no code change, PR, restart, or
redeploy is required. The runtime also polls the durable latest-effective version so a dropped
control event self-heals. Every paper entry pins the exact config row effective at its fill time;
an already-open measurement window never changes when a later version becomes effective. The
screen and every decision show the config ID, values, and effective timestamp.

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
entry and one exit marker per paper identity, plus a daily reconciliation marker. Before the first
paper fill, the page renders `WAITING FOR FIRST LIVE RESTING FILL`, service/feed/scanner health,
Webull and Schwab live-fill denominators at zero, Mirror as `UNEXERCISED`, Independent's current
watch/arm state, and the last reconciliation time. It never renders an unexplained empty panel.
Mirror acceptance is a dedicated durable tri-state derived only from the fill-table census:
`UNEXERCISED` when live=0 and mirror=0, `FAIL` when live>0 and any live fill is unmatched or when any
phantom exists, and `PASS` only when live>0, every eligible fill is classified, and no phantom
exists. Service/feed health is displayed separately and cannot promote the acceptance verdict.

## 7. Limits And Falsifiers

The harness cannot know executable size until bid/ask size provenance is established, cannot infer a
fill inside a quote gap, cannot turn the independent arm into an entry comparison without accepting
confounding, and cannot grade a live position without a closing broker fill. The approach is
falsified if any mirror price or timestamp differs from its broker fill; a live resting fill is
silently absent; a paper-only entry appears in the mirror total; either arm reaches
`strategy-intents`, broker code, shared slot/state tables, or the live poller; restart changes an
existing paper decision; the two arms are pooled; or the screen reports a result without its stated
denominator and evidence status.

Day-one acceptance is one complete session with `N` live first/resting fill legs reconciled by venue,
all `N` classified as matched or missed, zero unexplained phantom rows, and a logical expected count
derived by the stated collapse. Every matched logical entry must have one durable entry marker and,
by close, an exit or explicit `UNANSWERABLE` state. If `N = 0`, Mirror is `UNEXERCISED`, never
`PASS`; service, feed, scanner, reconciliation, independent-arm, and structural no-order checks are
reported separately and cannot turn that zero into an exercised mirror result.
