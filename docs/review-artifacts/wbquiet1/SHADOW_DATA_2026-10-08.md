# WBQUIET1 shadow data: log-only draft

Owner: codex-2. Branch: `codex/wbquiet1-shadow-log`.
Base: `b8fd3c3e57ba0edec06e4fd13c1ad7c299dc1857` (Step 0 over main d244f602).

Operator disposition 10-08 supersedes Step 0's five-session wait: collect today
and tomorrow, report Friday evening, operator decides the trading change this
weekend. The cadence/cancel change is NOT built or approved by this PR.

## Scope and receipt

The observer runs only for the periodic control-loop broker-state sync. Startup,
intent reconciliation and quote/trade exit reconciliation do not sample or log
WBQUIET data. Existing broker calls, their order, persistence, virtual clearing,
restoration, exceptions and results are unchanged. No additional HTTP call exists.
No settings, catalog, strategy or bot-service file changes.

Before the existing pass, a separate off-loop, rollback-only DB session samples
one bounded EXISTS query for configured Webull routing names. It observes open
managed rows, nonzero virtual/account books, nonterminal orders, pending intents
and buy OR sell fills in the preceding ten minutes. These are extra **periodic
observation SQL**, not trading SQL, and are not on a per-tick path. PostgreSQL
uses READ ONLY, 100ms statement timeout and 25ms lock timeout. Missing records
are UNKNOWN, never flat. This snapshot is not a transactionally synchronized
account-state proof and does not certify that a later reader's decision is equal.

`[WBQUIET-SHADOW]` emits one JSON receipt per successfully observed periodic pass.
`process_pid` + `pass_id` join the existing `[OMS-BROKER-SYNC-PASS]` identity.
Facts carry the pre-pass observation time; actual read completion and reader
consumption carry elapsed times. Hypothetical clocks are maintained independently
per account: held / working / fresh-fill / UNKNOWN nominal 15s; known flat 60s;
known flat 20:00-04:00 ET 300s; unconfigured Webull credentials ignore. No clock
ever controls the real scheduler or broker operations.

An absent/unconstructed adapter, missing cache metadata, contested cache lock,
nonfinite timestamp, stale cache or active 429 backoff prevents quiet-flat
classification. Cache age is obtained from the adapter's actual monotonic
acquisition timestamp, using its existing throttle for freshness; it is **not**
the sync completion time. No adapter is instantiated by observation. Accounts
not explicitly discovered in the existing routing map are outside coverage.
Other routed providers, including uncredentialed Alpaca aliases, are excluded;
this PR does not change their actual reads or warnings.

Conservative consequence: normal 15s polling with a 10s cache TTL will often
produce UNKNOWN at the next pass rather than a flat skip. That is intentional,
not a measured inability to slow polling. Improving source attribution or the
causal quiet-state model requires separately reviewed instrumentation; this
draft does not infer fresh flatness from a stale cache.

## Reader coverage, not zero-impact proof

| Reader | Observable relationship | Coverage limit |
| --- | --- | --- |
| sync_account_positions | Existing successful write consumes this pass's returned snapshots; tagged after the call | An adapter return is not proof of a new wire call |
| virtual clear | Existing call evaluates fetched account IDs against the same transaction's persisted backing; tagged after return | An invocation may evaluate zero rows; not proof of a decision change |
| virtual restore | Existing call evaluates those account IDs/managed backing; tagged after return | Not an external-read census |
| Other OMS readers, pages, reconciler, health, separate services | UNMEASURED | No added per-reader query or falsely zero consumption count |

Reader records state whether invocation occurred within 60s of observation start.
They are sync-transaction coverage, not an assertion that every DB reader saw
this exact generation. A failed pass retains `outcome=failed`; tags are attempted
consumption and do not assert commit. Unreadable accounts are excluded from
the existing persistence exactly as before. An unreached account is not evaluated
and does not advance its nominal clock.

`wire_calls_saved=UNMEASURED`, `other_oms_and_external_readers=UNMEASURED` and
`policy_applied=false` are explicit on every receipt. Historical positions and
list-today bodies remain absent. This does not claim zero impact, exact saved
calls, B/C historical terminal membership, or five clean sessions.

## Bounds and verification

At most 16 accounts, 48 reader tags and 16 nominal account clocks. One observer
worker can be outstanding; no backlog. Preparation/logging run through
`asyncio.to_thread`, each caller wait bounded to 50ms. A timed-out worker retains
the busy lock until it really finishes; later observation drops instead of
queuing more workers. Blocked logger/DB cannot create unbounded shadow tasks.
Drops are counted in the next successful receipt. Missing receipts are lost
coverage, NEVER zero broker work. At worst, two bounded waits add 100ms to a
periodic pass; existing trading work is not bypassed on observation failure.

Focused verification: 191 passed (44 shadow tests plus sync SPOF, failed-read,
Schwab never-synthesize-flat, list-primary and RESERVE1 controls). Ruff passed
on both source files and the test file. Tests exercise identical operation
sequence/return/exception with observation disabled, enabled and broken; separate
off-loop SQL, recent BUY/SELL fills, state priorities, stale/contested cache,
logging errors, timeout/no-backlog, memory bounds and truthful reader coverage.
The separate OMS risk-service file passed 103 tests (294 total across these runs).
The 240-event bypass test proves non-periodic calls perform zero observer SQL;
it is not a live throughput or decision-equivalence receipt. Full-suite and
production observation are not yet measured.

## After-Close Install Plan (Docs Only)

No staging, timer, approval file, merge or production restart is performed by
this task. After exact-head review/pin and a candidate approval, include this
log-only source in the parent's after-close OMS install. Gates and application
SHA come from that reviewed joint plan, not this document. No new env key,
cadence switch or cancel behavior is installed.

Post-install: verify new OMS identity/SHA and existing flags; find shadow receipts
joined to actual periodic sync IDs, identify drops/UNKNOWN denominators, and
confirm broker/store calls and exception paths remain unchanged. Collect live
plus rotated log paths. Friday evening report must separate nominal skip counts
from actual endpoint counts and incomplete reader/history coverage. Operator's
weekend decision is required before any WBQUIET trading change.
