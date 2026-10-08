# LINESRC2: Draft, Not Activation-Ready

Base: d244f602491e5d316a05670b205315466aed2a4d.
As of 2026-10-08 08:09 ET. This side conversation uses its own
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

1. Focused correctness: 210 passed across LINESRC2, LINESRC1 event and parser,
   and restoration integration tests. Twelve controlled parser rejection shapes
   produce `state=error`, diagnostic evidence, and a live fallback matching the
   flag-OFF state/trail/age over the next twelve recorded RETO bars. Missing the
   current candle is waiting, not error. Retry spacing, cap, late repair and
   gap/removal barriers have separate controls.

2. Mutation: disabling `LineRepair.fallback_due` caused 17 failures, two passes
   in the then-19-test LINESRC2 file. Restoring the absent-current-candle raise
   caused its no-error control to fail. Mutations were in memory in separate
   processes, not written into the source checkout.

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

## Full Suite

Fresh main and final-source unit runs are in progress. An earlier development
head run was interrupted and is not a final-head receipt. No failed-name
equivalence or CI-green result is claimed until measured.

## Activation / Rollback

Flag remains OFF on the box. Activation needs exact-head review/pin and a
reviewed after-close install plus the 07:00-07:15 next-morning observation.
Rollback remains restoration env=false plus the authorized v2 restart; no
rollback, recovery, production write or other lane was executed here.
