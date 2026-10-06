# CLEARWAIT1 RELEASED card - own Step 0

AGREE with the revised card. The prior never-submitted restriction is withdrawn.
Before a fill, removal revokes scanner permission and cancels a waiting buy;
only settled terminal cancellation/rejection and no-fill/no-position proof
permit retirement. After a fill, the owned episode and managed feed survive
removal until the existing exit lifecycle ends. Unknown cancellation stays
blocked. The default-ON switch is for rollback only; no deploy is performed.

Clean evidence-only branch was rebased onto `c21d8274` (main including #1097
and ORBPURPLE1); no pinned runtime source existed on this branch. Only this
worktree and branch are written; parent owns shared C-rows/handoff.

## Fresh Own Counts

SSH stdin, nice19, 90-second process bound; read-only SQL transactions with
8-second statements and 500ms lock timeout. Capture at 13:31 ET (see fixture
for the exact UTC timestamp) from production head
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`. No production file/state was written.

| Claim or population | Own evidence |
|---|---|
| Six today | AGREE: 6 unknown-owner removal markers, across 3 episodes: AIXI four, XHG one, AIFA one. |
| Six class examples today | Six events, not six independent episodes. All three have terminal order rows and no recorded exact-episode buy fill. This alone does not prove every retry/cancel owner resolved. |
| 62 blocks / 11 sessions | 63 retained unknown-owner removal markers / 11 distinct trading dates from 09-22 through 10-06; 43 symbol/opportunity episodes. The 62 count is not reproduced and is not used as a receipt. |
| Wider class decomposition | 32 events with terminal-unfilled order rows; 24 without a matched order; 7 with fill or durable position identity. These are final retained records, not all historical as-of terminal confirmations. |
| Admission log denominator | 48 logged `owner_phase_unknown` admission blocks / 4 dates; log throttling means these are not all admission evaluations. |

16 v2 log files, 43 symbols. SQL returned 706 orders, 200 fills, 1578 intents,
100 managed rows, 1154 owner/identity/retry snapshots, 104 account positions,
104 virtual positions and 46 retry owners; no query/row limit exhausted.
The raw capture is preserved locally with its hash, and the source-backed
review fixture includes the complete removal census and exact-case rows.

## Recorded Cases And Safety Limits

| Case ET | Evidence and required disposition |
|---|---|
| AIXI 10:18:59, 10:32:57, 11:04:52, 11:10:28 | Same opportunity `1791294242804`. Webull order `8271449a-be0a-4325-9216-c8ead327376b` was submitted 09:57:42 and confirmed cancelled 10:03:03, explicit zero. Its quote wait at 10:18 belongs to that same still-active episode. 52 exact-episode intents are terminal: 51 pre-wire refusals and one cancelled open. No episode fill or open managed row. A settled removal acknowledgement on both accounts can clear this wait; re-add must allocate a different opportunity and use current bars, line readiness and normal entry gates. |
| XHG 10:41:17 | Opportunity `1791297123018`; Schwab order `b8d3c8cc-59da-4086-9c8f-40e1b7ed7567` broker-rejected at 10:32. No episode fill. However its exact episode also has RPG ticket `b725a133-9840-5016-8a6c-4ef7e4680aab`, phase `held_unknown`, reason `exact_old_order_unproven`, `local_no_wire=false`. That ticket must stay blocked: a rejected order does not erase unresolved cancellation ownership. Terminal-order replay and this recorded unknown-ticket negative are both required; no ticket repair/clearing is in this lane. |
| AIFA 11:22:11 | Opportunity `1791299283099`; 8 exact-episode buy orders, 4 per account, all cancelled. The 11:11 cancel was followed by later replacement/cancel generations: final Schwab cancel 11:22:03 and Webull cancel 11:22:09. A six-order or 11:11-only receipt would be false clean. No episode fill; all 9 episode intents terminal. Preserve the 15-second settle interval: 11:22:11 itself is too early to clear from the last cancel. |
| JAGX 09:30 foreign fill | ORB-Schwab 2-share fill precedes the V2 unknown owner. Empty V2 owner fields are not an ownership receipt; this ambiguous owner shape remains blocked. Foreign fills are never inserted into V2 `fill_accounts` or assigned a V2 managed identity. |
| MI 10-05 owned/foreign shapes | Recorded V2 and ORB entries share an account/symbol, but distinct order/managed identities. Any exact own fill/position identity prevents wait retirement, while foreign rows are not manufactured into own fills. Existing held/managed subscription and fade/purge carve-outs remain authoritative. |

XHG's pre-existing unknown ticket is an explicit fail-closed control, not
permission to waive a guard to make all six examples green. AIXI has an older
Schwab unknown ticket in a different, already-filled/closed opportunity;
this lane will not alter it or bypass RPG admission. It only retires the
current proven-unfilled removed opportunity. No later entry/fill is promised
when any current strategy guard refuses.

## Safe Implementation Boundary

Record a removal request before cancelling; revoke entry permission while
the request is pending. Route account-specific cancellations through the
ordinary serial OMS lane, including software/quote waits that have no broker
order. Require durable acknowledgements for both configured active fan-out
accounts, settled terminal opening rows, no episode fill/position identity,
no unresolved current retry ticket and fresh readable evidence. A cancel
draft or failed DB read cannot clear an episode. Cancel generations and
queued deferred retries must not outlive the old removal token.

Retire the fan-out identity first, then owner state, then the removal request;
partial persistence stays closed. Retain line history/restoration epochs,
seed-cap markers and RETRY-ONE SELL-cycle budgets; clearing a no-fill wait
does not replenish a used filled-entry allowance. RETRY-OFF must produce the
first legitimate fresh opportunity on re-add. No historical BUY replay or
authorization age waiver. KEEPREST's on-list waiting flip remains intact;
removal is a distinct cancellation trigger. Rollback bypasses only this new
path. Required mutation negatives cover cancellation unknown, either-leg
fill, position identities, stale proof, and old opportunity reuse.

Ready-for-review target: **2026-10-06 17:00 ET**, including recorded replays,
mutations, full-unit immutable-baseline pair and both Validate runs.
Implementation blockers: **0**; recorded XHG unknown ownership is deliberately
held as a negative control. This checkpoint precedes any runtime edits.
