# October7 Event-Driven LINESRC1 Step0

[codex] Own source-read assessment: AGREE with the new human card. Implementation
and new-card acceptance are PENDING, not proved by the old polling receipts.
Checkpoint local HEAD: 5c2cd48ad8cb02ff60db50322be9f42d98d79c09, verified base
5b8b4f642bbc3c312be436d0e92adbc22d9e9f95. Remote draft PR1107 remains
35380b9b76745896b90afd43eca7c0f09011bd12. No source push, review/pin or deploy.

## Independently Read Failure Mechanism

- The REST client's `_bar_loop_pass` currently diverts the ordinary incremental
  fallback into `_anchored_bar_loop_pass` whenever `_session_request` is wired.
  Remove that diversion; normal recent-bar delivery is not the rejected poll.
- Every changed live observation increments the ledger revision. Readiness also
  demands exact published revision/current-bar equality. The old provider
  fingerprint cannot attest a later live append. Merely removing the full
  history poll therefore freezes buys.
- With restoration enabled, the strategy callback intentionally returns before
  normal ATR evaluation. Its off-callback worker publishes line mathematics.
  The replacement must advance this worker's immutable snapshot on contiguous
  live bars, using the existing ATR calculation, not waive the gate or recompute
  full-session history on every bar.
- Existing stored-bar reconciliation deliberately does not rewrite the provider
  fingerprint. Preserve that property: DB additions/corrections cannot become
  proof merely because a SELECT succeeded.
- `_apply_strategy_state_event` returns early for an unchanged selected feed.
  Scanner transitions must be inspected before that return, independently of
  protected-position feed retention. Repeated full snapshots are not new events.

## Proposed Event And Provenance Contract

1. Scanner absent-to-confirmed/re-add needing a hole repair enqueues one full
   04:00-anchored fetch. Confirmed membership or a real confirmation identity,
   not snapshot publication time or updated quote prices, identifies an event.
   A no-hole re-add with still-valid same-session evidence makes no GET.
2. Initially listed names receive one startup event after their first actual
   eligible candle at or after 07:00 is observed closed (including quiet names
   with a first 07:02/07:10 candle). Earliest 07:01 ET. No timer
   creates history requests, and wall-clock 07:00 alone is not a receipt.
3. Coalesce by symbol/session/event identity. One in-flight request per symbol;
   callbacks only enqueue. Bound network work off-callback, and fence late
   responses by session, epoch, membership generation and request identity.
4. Valid empty/zero-candle results consume the one event and remain waiting, not
   an exception. Strict ONCE interpretation: no retry from subsequent callbacks
   or ordinary worker passes. Another qualifying event is required. If the human
   intended an extra next-bar retry, that is an explicit policy clarification,
   not permission for an all-day callback-driven poll.
5. Keep the provider prefix's IDs and value hash immutable. Record separately
   validated, actually closed live-source values. Only an exact contiguous
   suffix matching those observations extends completeness. Historical replay,
   DB reads, an unknown gap, or a correction cannot manufacture live provenance.
6. Advance immutable line mathematics off-callback from the last admitted
   snapshot on a contiguous live append. Preserve exact current/revision/reset
   and stale-draft fences. Re-add/correction/full-history repair is not a new
   signal and must not replay historical flips or consumed ownership.
7. Parent narrowed the proposed correction mechanism: a newly detected conflict
   immediately revokes readiness but NEVER authorizes a new GET. A DB fill of
   already provider-listed IDs with values matching the immutable hash may
   repair locally. New prefix IDs or changed values remain held until the next
   qualifying re-confirmed-hole event. Failed/empty callbacks cannot replenish
   a spent event, nor can a correction create an extra event.
8. Inactive same-session evidence may be retained solely to judge no-hole
   re-add; it does not subscribe, feed, manage or enable an off-list symbol.
   Session changes and new membership generations fence stale work.

## Required New Controls

- One event fetch, then multiple recorded contiguous live appends: exact line
  mathematics, current proof/readiness/version and confirmation advance with
  zero further full-history GETs. Compare incremental mathematics against the
  unchanged full-session reference; do not invent raw prices.
- No-hole re-add zero GET; hole re-add exactly one GET; held feed does not hide
  scanner removal/re-add; duplicate snapshots and concurrent callbacks coalesce.
- 07:00 unclosed/no event; 07:01 closed 07:00 receipt triggers once; no received
  candle means no invented trigger. Explicit time-boundary controls may shift
  timestamps on retained prices only when labeled counterfactual.
- Replay each actual BIYA/MI/MTEN/SXTC empty envelope: no exception, no retry
  from later callbacks, no false-ready publication, no fabricated candles.
- Late DB addition/correction, live correction/gap, stale response and epoch
  rollover: revoke/fence; re-attest only on a qualifying scanner event. R6 positive tape
  holes and ten-clean-live-bar requirement remain fail closed.
- Ordinary incremental fallback, source waiting, quote/confirmation exits,
  flag OFF, seed paths, ownership/retry budget and final draft fences.
- New semantic mutations, unchanged 123-case recorded acceptance, exact-head
  full unit comparison and hosted CI. All remain PENDING for the replacement.

## Retained Measurements And Superseded Proof

Actual own four serialized GETs: October7 06:39:07.647899 through
06:39:11.733507 ET, HTTP200 / empty=true / zero candles for BIYA, MI, MTEN,
SXTC. Parsed metadata SHA256:
b5c1a11553c6b229350ea1e97f9b81d46837bb65fc045f7503aecc49b7464661.
Fixture: tests/fixtures/linesrc1_oct7_empty_history_own_receipt.json.
Earlier setup PermissionError is retained as official FAIL / zero requests.
No later recovery, raw prices, execution, morning-gate PASS or deploy inferred.

Old 149/199/203/205 focused results, 20/27 mutation controls, full pairs and
CI certify only the superseded polling scope. The unchanged 123-case result
is a preservation baseline, not acceptance of the unbuilt replacement. Source
and production changes remain separate; this lane performs no production,
handoff, activation, merge, pin or install writes.

Implementation checkpoint: see EVENT_BUILD.md for the replacement's exact
source hashes, review RED/GREEN controls, preservation results and remaining
full-head/CI gates. This Step0 retains its original assessment timing; its
PENDING language is not a current test-count receipt.
