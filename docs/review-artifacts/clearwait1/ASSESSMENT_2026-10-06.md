# CLEARWAIT1 Step 0 - DISAGREE; runtime build stopped

Lane C only, branch `codex/clearwait1-unplaced-owner-removal`, exact base
`3ebde364d4634fdad45992e2ab1cbdf43ffeb221` (includes #1097). Shared handoff
files have not been edited. Remaining blocker count: **1**, the recorded
acceptance population contradicts the mandated no-order-sent scope.

**AGREE** that removing a provably never-submitted, never-filled waiting
episode should clear that wait, while uncertain or submitted/filled episodes
remain blocked. **DISAGREE** that the named AIXI and XHG episodes qualify.
Per the explicit stop-on-disagreement instruction, no runtime code, flag,
production state, deployment, service, broker, ledger or Redis was changed.
The implementation and its acceptance/mutation/full-suite/CI stages are stopped.

## Exact Case Evidence

All human times below are Eastern on 2026-10-06; capture timestamps are UTC.

| Case | Durable evidence | Verdict under the requested card |
|---|---|---|
| AIXI removal 10:18:59.275, re-add supplied as 10:21:26, missed 10:22 flip | Opportunity `1791294242804` already had Webull order `8271449a-be0a-4325-9216-c8ead327376b`, client `schwab_1m_v2-AIXI-open-d1c79f2d643b`, broker `U1G26C05OVO9ES2BPGHEIENOVB`, 115 shares. Submitted 09:57:42.781992; accepted event 09:57:42.999357 includes wire timestamp 09:57:42.797. Broker-sourced cancel 10:03:03.454717, reason `atr_reprice_terminal_cancel_explicit_zero`. Same exact opportunity and resting slot as removal. | **DISAGREE: an order was sent.** Zero recorded fills does not make it never submitted. |
| AIXI removal 10:32:57.333 | Same opportunity `1791294242804` and same previously submitted Webull order. | **DISAGREE: same submitted episode**, not a second clean episode. |
| XHG removal 10:41:17.025 | Opportunity `1791297123018`; Schwab order `b8d3c8cc-59da-4086-9c8f-40e1b7ed7567`, client `schwab_1m_v2-XHG-open-572eed004d33`, broker `1008187019347`, 195 shares. Submitted 10:32:03.558143; broker-sourced reject at 10:32:03, opening transactions must be placed with a broker. | **DISAGREE: an order was sent and broker-rejected.** Existing terminal-unfilled recovery released it at 10:41:22.158; not persistently stuck. |
| JAGX 09:30 foreign fill | ORB-Schwab order `0be05de1-9744-4d1a-aef2-de19ec5bf2bb`, client `orb_schwab-JAGX-open-3a8263e1999d`, account `live:schwab_1m_v2`, buy fill `1fe9f814-b3a2-468b-a5b5-ecacd2330b62` at 09:30:14, 2 shares at 6.67. V2 owner minted at 09:30:19.343, unknown reason `schwab_first_rest_filled_not_first_rest`, with empty own fill/position fields. | **AGREE: removal cannot clear based on empty V2 fields.** Historical foreign-fill evidence precedes the minted owner. |

AIXI really was quote-waiting immediately before removal: stale quote at
10:14:02.406, line rederived at 10:16 and 10:17, gave-up at 10:18:59.275.
That waiting substate did **not** begin a new durable ownership episode. Its
quote-wait segment is the SELL-cycle timestamp `1791294000000`, whereas the
durable ownership opportunity is `1791294242804`; conflating them hides the
earlier submission. Admission reads `owner_phase_unknown` after removal.
Re-added quote wait is visible at 10:21:27.830 and ends at 10:21:29.277 as
`slot_owned_or_consumed`. The 10:22:02.337 CW arm is present. The exact
10:21:26 list-add second comes from the request, not independent list history.
An entry attempt or fill at 10:22 under changed code is **UNMEASURED**.

JAGX's foreign fill closed at 09:31:07; existing SELL-flip logic retired its
unknown owner at 09:34:02.153935. This assessment preserves that existing
lifecycle, and does not claim it stayed unknown all day.

## Retained Class Sweep Since September 28

Own log scan covers the current v2 log and nine retained dated logs beginning
09-28, filtered by timestamp. It found **24**
`watchlist_removed_before_position_episode_ended` events representing **16**
distinct symbol/opportunity episodes. No cap was reached: 183 orders, 59 fills,
506 intents, 29 managed rows and 421 ownership/identity/retry snapshots were
returned for the 16 symbols represented by removal markers plus controls.
This is the complete retained unknown-owner-removal marker population, not
a claim to enumerate silent evictions that produced no marker.

Counts below are order/fill rows matched by exact opportunity metadata over
the captured history, not inferred from present account flatness. `No match`
is **UNMEASURED for safe clearing**, because absence of a matched order does
not itself prove there was never an emitted/in-flight or malformed entry.
Repeated events retain their original episode identity.

| Removal ET | Symbol | Opportunity | Matched orders | Matched fills | Classification |
|---|---|---|---:|---:|---|
| 09-28 13:25:24 | CLRO | 1790615942287 | 4 | 0 | Submitted |
| 09-28 13:36:16 | MEDS | 1790615640378 | 6 | 0 | Submitted |
| 09-28 15:03:20 | NAMI | 1790616903122 | 9 | 0 | Submitted |
| 09-29 18:40:50 | DXST | 1790703543160 | 10 | 0 | Submitted |
| 09-30 08:24:37 | NCI | 1790768102878 | 0 | 0 | No match; software rest disarmed |
| 09-30 08:37:43 | NCI | 1790768102878 | 0 | 0 | No match; repeated episode |
| 09-30 09:35:37 | LGHL | 1790774762362 | 0 | 0 | No match; both rest cancels queued |
| 10-01 08:29:57 | MEDS | 1790857262747 | 0 | 0 | No match; software rest disarmed |
| 10-01 08:46:46 | VEEA | 1790858582561 | 0 | 0 | No match |
| 10-01 09:02:25 | VEEA | 1790858582561 | 0 | 0 | No match; repeated episode |
| 10-01 09:32:47 | MEDS | 1790857262747 | 0 | 0 | No match; repeated episode |
| 10-01 09:44:45 | MEDS | 1790857262747 | 0 | 0 | No match; repeated episode |
| 10-01 10:08:41 | MEDS | 1790857262747 | 0 | 0 | No match; repeated episode |
| 10-01 13:16:53 | MEDS | 1790857262747 | 0 | 0 | No match; repeated episode |
| 10-01 16:41:10 | MEDS | 1790857262747 | 0 | 0 | No match; repeated episode |
| 10-02 04:00:00 | NXL | 1790875862772 | 8 | 2 | Filled; both owners bound |
| 10-02 04:00:00 | VEEA | 1790879042757 | 2 | 2 | Filled; both owners bound |
| 10-02 14:25:09 | CYCU | 1790963880380 | 8 | 0 | Submitted |
| 10-02 19:58:34 | AIXI | 1790968922460 | 2 | 0 | Submitted |
| 10-05 08:15:39 | SAIQ | 1791200102388 | 1 | 0 | Submitted/rejected |
| 10-05 11:28:38 | RETO | 1791213663149 | 1 | 0 | Submitted/rejected |
| 10-06 10:18:59 | AIXI | 1791294242804 | 1 | 0 | Submitted/cancelled |
| 10-06 10:32:57 | AIXI | 1791294242804 | 1 | 0 | Same submitted episode |
| 10-06 10:41:17 | XHG | 1791297123018 | 1 | 0 | Submitted/broker-rejected |

Thus 13 events have matched submitted-order history (including 2 events with
fills); 11 events across 4 episodes have no matched order and are candidates
for further proof, not approved clean episodes. Both premarket software rest
and RTH broker rest appear in the population. Additional non-unknown removal
markers are retained in the raw capture, including MIMI 09-28, MSGY 09-29 and
JAGX 10-06. They are not silently counted as ownership failures.

## Code Assessment And Stop Condition

`release_and_drop_symbol` ends quote wait, then #1097's LINE=CHART path retains
the state/owner, disarms permission and queues active rest cancellations.
The older strict-owner path has a narrow in-memory no-order retirement test;
otherwise it persists unknown ownership. The position book proves managed
position state, not absence of every submission, foreign fill or in-flight
intent. Empty owner fields are demonstrably insufficient in JAGX.

The operator's narrow card can be implemented only with durable never-sent
proof and preserved retry/slot/seed/restoration semantics. It cannot satisfy
the mandatory AIXI 10:22 clean-readd replay while also obeying "episodes WITH
an order ... keep today's fail-closed path". AIXI's confirmed zero-fill
terminal cancellation is a **different scope** from never submitted. No
scope was broadened to include it. Rollback default-ON switch, recorded
acceptance mutations, full-unit baseline pair and both Validate runs have
not been built/run, because the requested Step 0 stop condition fired.

## Capture Method And Receipts

`read_step0.py` was executed by SSH stdin on `mai-tai-vps`, `nice -n 19`, under
a 90-second process bound; each DB query uses `SET TRANSACTION READ ONLY`,
8-second statement timeout and 500ms lock timeout. SELECTs only; no remote
file was uploaded. Initial unprivileged log read was refused; a root read-only
invocation succeeded without permissions changes. One supplemental query
used a nonexistent event `created_at` column; it was corrected to `event_at`
and the final complete capture rerun. Neither failed attempt is treated as
evidence. No secret-bearing environment or token values were printed.

Production capture head: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`.
This is distinct from the intentionally pinned implementation base.
Raw captures and SHA256 receipts are preserved locally under
`/Users/velkris/.codex/clearwait1-evidence-20261006`.
`evidence.json` contains generated exact-case source rows and sweep identities;
its generating script refuses query errors, exhausted row caps, missing case
broker evidence or the absence of JAGX's foreign-fill control.

Assessment review hour: **2026-10-06 11:15 ET**. Runtime ready-for-review hour
is **unavailable pending the one scope/acceptance blocker**. No implementation
PR or ready head is claimed.
