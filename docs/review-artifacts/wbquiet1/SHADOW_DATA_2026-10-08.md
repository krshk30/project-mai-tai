# WBQUIET1 shadow data: log-only draft

Owner: codex-2. Branch: `codex/wbquiet1-shadow-log`.
Application base: main `d244f602491e5d316a05670b205315466aed2a4d`.
Step 0 read: `b8fd3c3e57ba0edec06e4fd13c1ad7c299dc1857` on
`codex/wbquiet1-step0-impact`, `docs/review-artifacts/wbquiet1/STEP_0_2026-10-08.md`.
The study/evidence commit stays separate; this PR contains only the shadow files.

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

Read-only audit at 07:18 ET confirms this is DRAFT-only reader coverage, not
fulfillment of the requested downstream-consumption window. The ContextVar is
reset before the immediate receipt; no following 60-second window is observed.
Tags describe invocation completion before transaction commit, not a committed
generation consumed by another task/process. Missing external reader tags must
not be interpreted as no reader activity.

Untagged paths include OMS settlement, SELL quantity/reservation admission,
startup protection, stale-held reconciliation, direct-flat and emergency reads,
fill-accounting changes to AccountPosition, RESERVE1, EOD/exit checks, reconciler,
control-plane pages/leg quantities, V2 feed/exit coverage, CLEARWAIT1 and strategy
position propagation. Ops preflight/restart/evidence, phantom/shape/quantity and
census/health checks also have no per-generation reader tag. ORB/EOD readers
identified in Step 0 are Schwab-scoped, not consumers of Webull's cadence.

Completing the requested reader study requires a bounded account-generation
registry for 60 seconds after successful commit, memory-only tags from already
loaded source metadata, and matching external hooks/offline joins. Ambiguous
generations and lost observations must remain UNMEASURED. No such extension
is represented as implemented in this draft. Likewise, B/C need observation
of already-fetched list evidence at the existing cancel/verification decisions;
historical missing response bodies cannot be reconstructed by this observer.

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

Final frozen-source suite on `ce3b51a3a14e406de52c76cbf74aaa357e0b6f91`
completed at 07:21 ET: 47 failed / 7,535 passed in 609.51 seconds. Main
`d244f602491e5d316a05670b205315466aed2a4d` has 47 failed / 7,491 passed;
failed-name sets are identical, with no added or missing names. Set hash:
`17d68105856e998ab629144135fd40b4ec5b3b4a53fe197c069a0cc1f25eb419`.
Raw `/tmp/wbquiet1-final-unit.log` SHA256
`9c97db04ac0a36a9a17cc5057d3f0196af145603633081d228e32951bea6bc83`;
XML `/tmp/wbquiet1-final-unit.xml` SHA256
`d56ee379058d048cd5d435cf684e4ae72282f2add5d858c715187c5d9e566f2d`.
Both paired runs use the same Python environment and explicit Homebrew-first
PATH for subprocess helpers. Earlier worktree system-Python failures are not
used as a final failed-name comparison; no tests were weakened for that error.

Hosted push Validate passed (run 37766767665). PR run 37766796016 failed only
the existing NFQ2 duplicate-quote timing control (744 ms versus 50 ms); rerun
requested without threshold/source edits and pending at this receipt. Local
final suite has no new timing failure. This does not prove the CI cause or
claim hosted-green on the documentation follow-up head. Independent pin absent.
The receipt/coverage follow-up changes docs only; source stays frozen.

A one-time Friday October 9 17:00 ET evidence-report heartbeat is registered
as `wbquiet1-friday-evidence-report`; it cannot deploy the shadow or alter any
trading behavior. Missing installation, historical bodies and reader coverage
remain explicit in that report. This is not a box logging job.

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
