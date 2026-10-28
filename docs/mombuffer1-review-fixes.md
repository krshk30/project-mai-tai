# MOMBUFFER1 ea24 Review Sequel (Author Only)

This supersedes the original contract's global ContextVar propagation and
post-permit local-refusal retention design. No worker tests/imports/replay/build
or runtime. Requires new protocol source review and all gates on the new head.
15:30ET status supersedes the old17ET checkpoint;16:05copy is not a gate waiver.
MOMSTUCK2/FixA/SPHL proceed independently. WBATTACH is explicitly preempted,
clean556-based worktree/resume note preserved, no lifecycle fix adopted here.

## Actual Failure Classification

Claude13:57:48 ACK withdraws the original claim that MOMSTUCK2 settlement broke.
All24 failures were the NEXT admission at original test line160, AFTER SPHL
terminal/slot/source-idempotence assertions passed. Shared quote4.00 exceeded
NEXT P3.00*1.015. Correct ONLY that separate admission's setup: fresh ask3.00 and
received_at=current fixture clock, after prior assertions and restored ports.
No production cap/freshness/settlement rule changed to accommodate that test.
Two Schwab tests used Mock as a context manager: corrected to MagicMock.

Receipts independently read/hashed, NOT worker execution:
RAW `MOMBUFFER1-REVIEW-ea24-RAW-20261021.log`
SHA2563cd7a491cc38e4462eb4b7c607b3a6e6782f125e0c510820f9666de4ec8d144b;
JUnit `MOMBUFFER1-REVIEW-ea24-JUNIT-20261021.xml`
SHA256e89dd5c31dfe80b3eb055219a9e414870ff1041c2ea0cd7bf7ac1651d7c3d632.
External ea24 result26FAIL/78PASS, baseline556 MOMSTUCK49PASS. Those receipts
do not pass this changed head. All sequel controls UNRUN.

## Six Original MOMSTUCK2 Line Edits Versus556

| current line | original -> ea24 | reason / retained contract |
|---|---|---|
|100|post-durable ask3.01 ->4.00|in-cap3.01 is now legal; fault must exceed3.045 cap|
|109|prewire ask3.01 ->4.00|same actual refusal stimulus at second checkpoint|
|143|quote_changed reason ->rounded_ask_above_cap|assert exact real refusal, not obsolete equality|
|152|receipt ask3.01 ->4.00|retain exact diagnostic receipt assertion for new fault|
|156|log ask3.01 ->4.00|retain age0/cap3.045/no-wire settlement log proof|
|278|boot test quote_changed ->rounded_ask_above_cap|same preserved terminal restart contract|

Every terminal/status/idempotence/slot/log/source/restart assertion remains.
Sequel adds fresh quote ONLY at line161 before independent NEXT acceptance.
Positive up/down same-row repricing controls remain separate. No weakening,
skipping or baseline assertion deletion to turn red green.

## Exact Native Capability, Not Context Inheritance

The ContextVar stores request identity, sealed non-permit request, exact episode
snapshot, original asyncio task, and one transport-attempt object. Inheriting a
context does NOT authorize a child task: exact same entry object in a different
task is fenced; a cloned same account/strategy/coid is fenced. Legitimately
different ATR/ORB/exit/account requests receive no capability and no guard.

Schwab submit_order resolves the capability against its actual OrderRequest;
passes it explicitly through authorization/token/JSON worker calls ONLY for
this exact entry. The blocking method no longer consults ContextVars or guards
all POSTs. A multileg unrelated POST without that argument is untouched.
Webull likewise resolves it against the actual request in the owning asyncio
task, passes it explicitly to _submit_blocking after the existing route branches.
Worker-thread context inheritance is not authority. No wire metadata or permit
payload is edited to carry the capability. Unrelated args stay unchanged.

The exact guard validates fresh request and actual serialized/setter qty/limit/
symbol. Immediately BEFORE order transport it marks dispatched=true. That mark
is monotonic: 401/retry, timeout, partial network attempt or later failed check
can never create a no-dispatch receipt. Existing transport retry policy is NOT
widened. Callback/token I/O remains unknown if no exact guard receipt exists.

## New Local No-Dispatch Terminal Protocol: Review Required

If and ONLY IF the explicit native guard refuses before first transport, it
creates a typed in-memory receipt tied to that attempt/request/task/episode.
Schwab/Webull may translate the exception into a client rejection, but executor
uses only the live receipt, NEVER report text, status or generic RuntimeError.
No receipt exists for generic errors, timeout/ACK uncertainty or prior I/O.

Runtime consumes that receipt once in the owning task. It verifies unchanged
sealed request and exact permit marker; persistence locks original parent/order
and retains ALL MOMSTUCK2 unsent identity/event/fill/VP/managed guards, with a
narrow additional option for exactly this already-committed permit. It compares
complete order payload except original prepared audit timestamp, parent/order
quantity and exact submitting episode snapshot. Conflicting/native/submitted/
ACK/event/position/terminal evidence refuses cleanup and keeps RAM reserved.

Only after guarded commit: order aborted, parent cancelled, episode terminal_no_fill,
same source/one-shot history retained, RAM slot released. Original MAY_HAVE_SENT
permit is retained verbatim with separate exact-native-before-first-transport
provenance. No report/fill allocation, broker cancel, new order or retry occurs.
This is NOT original MOMSTUCK2's generic unsent path, NOT a boot classifier,
NOT blanket permission to terminalize a permit or unknown ACK. Legacy pending
rows cannot manufacture this receipt; no restart inference from receipt absence.

Important remaining review: real PostgreSQL control/permit/STOP serialization,
trusted adapter boundary placement, cancellation/thread races, single entry
writer invariant and expected-payload seal at permit acquisition remain main
gates. New protocol is source-authored, not SOURCE approved or executed proof.
Control's existing full payload seal gap is not erased by these tests.

## Authored Controls / Pending Qualification

New review module: exact identity vs ATR/ORB/exit/account/equal-copy; inherited
child task and thread; unrelated multileg native POST; genericRuntimeError,
timeout and any prior transport retain uncertainty; conflicting durable permit/
payload/native/submitted/event/quantity refuses cleanup; actual native Schwab
token wait and Webull instrument wait cause exact local-refusal terminal state
with zero order transport calls. Existing final-wire controls now require that
specific local refusal end terminal, permit retained, no legacy settlement,
restore terminal and same-source no resend. Stable adapter paths retained.

MAIN-only after16 must run full MOMSTUCK2 and ALL three MOMBUFFER modules,
native/control/PG gates, actual27 episode +94 signal denominator before/after,
no new cap/stale send/no double episode, tick-rate/full/ALL-ON and Claude review.
No automatic3SEND or actual-cache proof from ±3s tape or source tests.
Actual recheck/cache/receipt clocks remain UNKNOWN. No new replay run here.
If not qualified in time, MOMBUFFER stays out; MOMSTUCK2 installs regardless.
