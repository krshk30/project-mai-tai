# LINESRC2: Draft, Not Activation-Ready

Base: d244f602491e5d316a05670b205315466aed2a4d.
As of 2026-10-08 08:21 ET. This side conversation uses its own
`codex/linesrc2-live-fallback` worktree; no shared source worktree or production
configuration was modified. No merge, pin, install or switch-on is requested.

## Step 0

AGREE: the old event handler retains only the exception class, invalidates
coverage, and exhausts a one-shot event budget. Restoration's early return
then blocks live ATR calculation without an accepted source publication.
Missing the latest closed candle already returns bars without a coverage proof;
it is not the cause of the observed ValueError by itself.

UNMEASURED: the exact 07:01 exception text and response envelopes were not
retained. Controlled parser responses below exercise the rejection branches;
they are not reconstructed Schwab responses from this morning.

Own read-only log check: `/var/log/project-mai-tai/schwab-1m-v2.log` reports
DKI 11:01:02.484Z, AIXI 11:01:02.769Z, SBFM 11:05:32.166Z and FLYE
11:26:18.997Z as `state=error reason=ValueError`; none of those four lines
contains exception text or response shape.

## Implemented Scope

- Error diagnostics retain exception text (bounded to 1,024 characters), HTTP
  status if observed, response symbol, candle count, first/last timestamps and
  envelope flags. No response body, credentials or pagination tokens are logged.
- While initial/re-add repair is pending, state-only legacy ATR mathematics
  consumes observed bars without trading signals. At 60 seconds after the first
  closed live callback, unsuccessful repair falls back to the ordinary live-bar
  path, including its existing confirmation-exit handling.
- At most five repair dispatches per epoch/membership, at least 60 seconds
  between dispatch starts. Requests remain off-loop and session-scoped; no
  all-day history poll is added. A late source publication cannot replay a
  historical buy or re-emit the current fallback bar as a fresh flip.
- Existing gap, removal/ownership, session and boot gates are not waived. The
  fallback has its own bar/version fence so a stale draft is still rejected.

## Three Receipts

1. Focused correctness: 247 passed across LINESRC2, LINESRC1 event/parser and
   review regressions, restoration integration and confirmation-exit tests.
   Twelve controlled parser rejection shapes
   produce `state=error`, diagnostic evidence, and a live fallback matching the
   flag-OFF state/trail/age over the next twelve recorded RETO bars. Missing the
   current candle is waiting, not error. Retry spacing, cap, late repair and
   gap/removal barriers have separate controls.

2. Mutation: disabling `LineRepair.fallback_due` caused 17 failures, three passes
   in the 20-test LINESRC2 file. Restoring the absent-current-candle raise
   caused its no-error control to fail. Removing the retained positive-trade
   barrier causes the two stale-reconciliation controls to fail. Mutations were
   in memory in separate processes, not written into the source checkout.

3. After-hours: with four attempts still available after one failed request,
   360 five-second turns after 20:00 ET made zero additional history requests;
   no `anchored poll failed` error. This is an isolated clock control, not a
   production observation or a replacement for tomorrow's morning watch.

## Morning Replay: Acceptance BLOCKED

Own read-only PostgreSQL pull on 2026-10-08, from `strategy_bar_history`,
strategy `schwab_1m_v2`, 60-second bars with bar_time 11:00 <= t < 11:13 UTC.
Transaction READ ONLY, statement timeout 10 seconds. All symbols queried; 35
retained rows across AIXI/DKI/SBFM. FLYE is absent in this interval. Arrival
order and replay clock use the recorded `created_at` timestamps, not fabricated
delivery times. Prices and timestamps are unmodified.

Fixture: `tests/fixtures/linesrc2_morning_20261008.json`.
SHA256: `7c39ae502938027063651b55fb0bc0d3a3a8736ef7dfcd47b94e78b97107755a`.

With controlled repair exceptions, fallback publishes and state/trail/age
match the flag-OFF control on the same bars. The replay produces **11 probes,
not 60**. It has no retained warm-up delivery history or actual 07:01 REST
envelopes. Therefore the requested >=60-probe acceptance is NOT PASS and this
draft must not activate the flag. No threshold was weakened to claim readiness.
The separate `scripts/linesrc2_morning_acceptance.py` enforces >=60 and exits 1:
`LINESRC2 ACCEPTANCE REFUSE: probes=11 required=60; keep flag OFF`.

## Full Suite

Fresh base d244f602: 48 failed / 7,490 passed. Intermediate source 5f5f09f3:
52 failed / 7,506 passed. Five head-only failures were found: one legacy
confirmation SimpleNamespace lacked the new attribute; two empty-response
controls asserted the superseded one-shot rule; two traded-gap controls exposed
a real fallback admission bypass. All five now pass in the expanded 247-test
receipt. The base-only HOTFIX1 performance case is not declared equivalent.

A revised full-unit run is in progress at `/tmp/linesrc2-revised-unit.log`
(XML `/tmp/linesrc2-revised-unit.xml`). The original base receipts are
`/tmp/linesrc2-main-unit.log` and `.xml`. No final failed-name equivalence or
CI-green result is claimed until measured. An earlier interrupted development
run is not a receipt. The GitHub main advanced while this isolated draft was
built; it has NOT been rebased or declared current/merge-ready.

## Activation / Rollback

Flag remains OFF on the box. Activation needs exact-head review/pin and a
reviewed after-close install plus the 07:00-07:15 next-morning observation.
Rollback remains restoration env=false plus the authorized v2 restart; no
rollback, recovery, production write or other lane was executed here.
