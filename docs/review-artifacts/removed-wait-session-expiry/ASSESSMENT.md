# [codex] Removed-wait follow-up assessment, 2026-10-07

**BLOCKED / STOP SOURCE WORK.** Subsequent own review disproved the safety of
the blanket prior-episode cancellation guard. Do not mark ready, pin, merge,
install or include this PR in an install batch. The request-retirement criteria
below are a conditional design agreement, not approval of this candidate.

Base: `f9c9bd332392e2c905fc39b954421c88970844d7`, tree
`0fa89d5d40473e29e1039cd6a80200b36945d696` (merged #1107).
Branch: `codex/removed-wait-session-expiry`. No edits to the pinned original
branch, shared handoff, production, or other lanes.

## Verdict and boundaries

DISAGREE with current guard: a positively identified prior-session working
BUY must retain canonical cancellation. `OLD_WORKING_RED.py` reproduces a
known exact OLOX-episode order in local SQLite using the retained identity and
quantity, with explicitly CONTROLLED accepted status (the real retained row
was cancelled). The existing OMS selector finds that exact old order; the real
removed-wait store returns `opening_order_not_terminal`. With the date guard,
restore/poll queues no cancellation. Guard-off counterfactual retains one.
Request and UNKNOWN ownership stay intact in both cases, which is insufficient
to satisfy the no-physical-order-abandonment requirement. No other canonical
cancellation is issued by this removal path. This is a new review-only RED
control, not evidence that historical OLOX actually remained working.

Receipt: `/tmp/removed-wait-followup-old-working-red.{xml,log}`: 1 FAIL / 1 PASS,
zero errors, 0.89 seconds. No production access. Remaining blockers: this
cancellation-path regression and the missing actual removal request/token.
Source implementation stops pending safe exact-target cancellation design and
the required historical receipt; green targeted/hosted tests cannot waive it.

Conditional AGREE: prior-session request bookkeeping can end independently of ownership,
but only with coherent prior-session request/episode timestamps and either a
positive matching current-session episode identity or a fresh canonical
`terminal_unfilled_removed_wait` verdict. Persistence must succeed first.
Filled/consumed/position state prevents terminal-unfilled retirement, including
a fill arriving after assessment. Supersession never modifies current ownership.

An old-episode cancellation barrier is not re-issued on restore or rollover.
Only queued BUY cancellations bearing the exact old removal token and episode
are dropped. Current-session unknown requests continue evidence polling and
retain their barriers/ownership block. Prior unknown requests without either
positive retirement criterion remain active and fail closed; age is not a clear
receipt. Scanner re-add, entry, sizing, retry, and ownership rules are unchanged.
The disabled flag does not perform retirement.

UNMEASURED: actual OLOX request retirement. The human supplied opportunity
`1791313204064` and reported stale fallback quantities 2/1. The retained read-only
census contains 12 exact-episode cancelled orders and 11 replacement jobs, but
no removal snapshot/token/request time and no complete dispatch journal. Those
terminal rows alone do not establish a current canonical clear proof. Tests use
the real opportunity/order/job records with explicitly CONTROLLED request
token/time, fallback quantities, clock, current episode and assessor verdicts.
They do not claim replay of the unavailable actual removal request. The exact
historical request remains one evidence blocker; unproved ownership stays held.

## Evidence

Retained census: `/private/tmp/install1-parent-expanded-sql-census-v2-20261006.json`.
Capture: `2026-10-06T20:16:18.401784+00:00` through
`2026-10-06T20:16:20.470399+00:00`; recorded read-only.
SHA256: `049a1e06c5b13aa6351ad03a7713b5989a3fbbc410e3d5328a549f30ad824359`.
Bounded extracted fixture:
`tests/fixtures/removed_wait_olox_20261006_retained.json`, SHA256
`07bad1031bd838e1d8aa0108ff5f4add593917aa7100fd7cfe46038fb59aba46`.
Other OLOX episode fills in that fixture are not attributed to this opportunity.
No new remote or production reads/writes were performed for this follow-up.

Frozen source SHA256:

- Strategy: `b113be9f2ceab225d55e84dab9b29f859c718aed27a6134a3bb36ac832e0a7da`.
- Service: `90528e2475e3d0275278e6aa4cde358aa9f3f6e87953e68718474c03e63b0652`.

## Verification checkpoint

Focused receipt: `/tmp/removed-wait-followup-final-focused.{xml,log}`:
273 PASS, 6.16 seconds, zero failures/errors. XML SHA256:
`df54f495107fcc8ce9d9de535d396dc32f90713930b39c9531095230539d54a5`.
Includes all existing CLEARWAIT controls, event-driven/reviewer controls, bot,
session rollover, ownership store and resting-reclaim tests; 24 new request
controls and the bounded refused-queue timeout control.

Mutation runner: `TIMEOUT_MUTATION.py`. Nine semantic controls cover the inner
test timeout, current-request date, unknown/future identity, post-proof fill,
stale proof, unrelated cancellation token, restart cancellation and failed
persistence. Isolated module compilation does not edit checkout source. Raw
receipts: `/tmp/removed-wait-followup-frozen-mutations.{json,log}`. Final result:
9/9 semantic kills, green baseline controls, zero harness/setup/runtime errors.

The empty-history worker test uses a two-second test-only timeout. Its negative
control refuses queuing and proves the inner timeout fires before the outer
watchdog. Production event behavior/timing is unchanged.

New-head full local suite has not run. Targeted proof is 273 PASS and 9/9
semantic RED mutation controls; hosted CI is pending at this checkpoint.
This is not a green full-suite or ready/install claim, and no human full-suite
waiver is claimed. Retain the same-day
tree-identical base receipt `/tmp/linesrc1-event-head-010b2119-20261007.{xml,log}`
(7076 PASS, 56 FAIL, zero errors) as baseline context, not as a new head run.
Historical #1107 proof totals are not new follow-up acceptance results.
