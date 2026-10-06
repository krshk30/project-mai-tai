# T43 OLOX: independent assessment before the build

As of 2026-10-06 10:49:53 ET. Own bounded read-only PostgreSQL and log pull;
source read at a7fed34d97732e1c1379ec77d89fd83f886ce2d8. Box source is
7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf. No production write or source fix.

**AGREE** that one leg was absent while the other rested, and that the shared
rest latch prevents the ordinary next-bar placement from restoring the absent
leg. **DISAGREE** with the supplied cause "Schwab rejected the replacement":
the recorded 10:15 attempt was a local authorization-age refusal before wire.
Per the standing independent-assessment rule, implementation is stopped for
this cause correction. The requested next-bar, one-leg-only card is not being
rejected; its proof must use the actual client-abort sequence, not an invented
Schwab stop-price rejection.

## Own evidence

| Claim | Verdict | Evidence |
|---|---|---|
| Schwab replacement is absent after 10:15 | AGREE | Order d56dac04-058e-47d1-b759-a9c9ffbbd9eb, client schwab_1m_v2-OLOX-open-b95fc559b0a2, submitted 14:15:08.077040Z, status rejected, broker_order_id null. Own query of orders 14:12-14:30Z finds no later Schwab buy. |
| Schwab rejected it because stop 1.46 crossed its market | DISAGREE | Linked intent 99ee8c8c-0e8a-452f-b828-d09cd55ec9e7 has refusal_origin=client_abort and refusal_code=rpg_stale_strategy_authorization. Ticket c9b13cc4-d0c7-5e87-84c1-5308f001158d has replacement_reasons=[rpg_stale_strategy_authorization]. No venue rejection text exists in this attempt's recorded evidence. |
| Refusal happened at the second, pre-wire authorization check | AGREE | service.py:2333-2349 commits a pending exact-client row, checks _rpg_open_refusal again, sets rejected and returns without the submit_order call at :2355. That branch writes the intent's origin/code but neither the order's reject_reason nor its audit event. The exact observed row/intent shape matches it. |
| Authorization expired during processing | AGREE | Ticket authorization at 1791296107.078; submit_started_at 1791296107.900015 (age 0.822015 s); completed_at 1791296108.163075 (age 1.085075 s). atr_reprice_runtime.py requires age <=1.0 s. The durable code states the refusal; the exact instant of the second check is not separately logged. No proposal to relax that safety bound. |
| Reject text not recorded on the order or order-event table | AGREE, corrected provenance | Own query: reject_reason null and event_count 0; the reason IS present on trade_intents and the hand-off ticket. This is a local-refusal audit gap, not proof that a broker error response was discarded. Ordinary broker ExecutionReports already write reason through _record_order_reports and update_order_from_report. |
| Webull survived while the primary stayed absent | AGREE | 10:15 Webull replacement client ...-13cd89daf224, 204 shares, stop 1.4639; later canceled at 10:18. The 10:18 replacement ...-9b56df8192c8, 205 shares, stop 1.4517, status filled, payload reports venue fill 14:27:14.293Z. Its child is recorded filled. |
| Next bar was admitted but did not re-place primary | AGREE | Own v2 log: 14:16:00.966Z admission allowed=1; 14:16:02.833Z bar timestamp1791296100000, short age45, trail1.456585, volume127705, held quantity not shown in this probe. No RESTING-PLACE in this selected interval. Code keeps resting_active when mirror quantity remains, then enters stable-rest management rather than initial placement. |
| The missed Schwab leg would certainly have made +5% | UNMEASURED counterfactual | Absence is proven, as is the recorded Webull round trip. A new Schwab attempt's acceptance, fill price, queue priority and exit cannot be asserted from Webull's fill. Replay acceptance should prove a correctly admitted next-bar attempt, not guaranteed profit. |

## Code cause and proposed-fix assessment

`rpg_handoff_authorization` receives per-leg feedback. For a terminal refused
ticket whose OLD buy is proven clear, it sets the primary quantity to zero but
clears the shared `resting_active` latch only if BOTH leg quantities are zero.
The placed mirror therefore keeps that shared latch true. `_cw_v2_resting_track`
places normally only when it is false; at an unchanged trail the active branch
does nothing. `_queue_resting_place` already recognizes generation-matched
placed tickets per account, but the tracker never reaches that producer.

Restoring one leg at the next eligible bar is sound only with per-account,
slot/segment/generation-matched positive terminal/no-wire evidence, current
window/price/liquidity/ownership gates, and no fill or uncertain dispatch. A
terminal old order alone must not excuse an uncertain replacement. The other
leg's generation, quantity and working order must remain unchanged. This is
not permission to restore operator hand-cancels or bypass policy/distance
refusals, nor to retry an unknown dispatch or already-filled leg.

Session scope must be stated faithfully: RPG hand-offs themselves are RTH-only
(`within_rth_entry_window`, plus pre-market exclusion in strategy authorization).
Pre-market software rests and their existing cross path must remain protected;
there is no recorded pre-market OLOX hand-off in this evidence. Swapped-account
and pre-market tests will be labelled controlled compositions, not history.

Issue 2 should record the actual local refusal on the pending order and a
client-origin event, alongside the existing durable intent/ticket reason. It
must not fabricate Schwab rejection text or mark a client abort as broker truth.
Any additional broker-path defect needs its own reproduced evidence.

## Raw receipts and bounds

PostgreSQL only: BEGIN READ ONLY, statement_timeout=5s; order interval
14:12-14:30Z LIMIT30, OLOX tickets LIMIT40, intent interval14:14-14:19Z LIMIT20,
exact-order event count. No broker submit/read side effects, token refresh,
Redis snapshot-batches read, service action, database or ledger write.
Log captures are root-readable filtered OLOX lines in existing v2/OMS logs.

| Local original raw path | SHA256 |
|---|---|
| /tmp/codex-olox-orders-tickets-20261006.jsonl | 405dc016519b5e8ae9744fa451e66a6133cf26ea3f11636a011e94afec636ff2 |
| /tmp/codex-olox-tickets-intents-20261006.jsonl | 475c7c03ea819c034419c8952881bacbc25d1ce6a506b2a49773e5108fd2c608 |
| /tmp/codex-olox-logs-20261006.txt | 62b1498421f0e670ccd283f288a9eb659570cb96f394c19122e5c3deebe84ffd |
| /tmp/codex-olox-rejection-proof-20261006.txt | 7732775339f99ab293cf1a6974382ef38ac58bd2a78a5fbe97cca3c451fc8d16 |

The first raw capture's attempted snapshot LIKE '%rpg%' returned none because
the actual type is atr_reprice_handoff; the second uses that exact type. Initial
log attempts as trader lacked file permission; the successful capture uses
read-only sudo. Neither absence nor access error was treated as proof.

No PR or "built" head is claimed. Branch: codex/rpg-one-leg-rest-recovery.
Await the cause correction before implementation; no change to any live flag,
the 0.5% band/reprice threshold, sizing, exits, or tonight's install candidate.

## Correction accepted and race assessment, 2026-10-06 11:05 ET

The corrected operator relay accepts the local-abort cause and releases both
halves. Own exact-intent pull reproduces TWO stale-authorization client aborts:
AIXI/Webull09:45 and OLOX/Schwab10:15. Own as-of11:05 count is 2/78 token-bearing
OPEN intents, not the reviewer's earlier approximate72 denominator.
Raw: /tmp/codex-t43-age-refusals-20261006.jsonl. A separate bounded120-row
handoff pull returned40 durable current tickets;35 have authorization and submit
timestamps. Authorization is mutable, so negative differences occur after later
feedback; completed_at is NOT the second-check timestamp. Exact p99
authorization-to-check is UNMEASURED; the1.380382s largest completion proxy is
not an acceptable safety-window calibration.

AGREE with inline reauthorization, not a blind age-window increase. On a stale
check, OMS requests an exact nonce from the existing v2 callback and waits only
the existing two-second read timeout. v2 evaluates all current gates, then
acknowledges that nonce. OMS retains the one-second age, canonical price,
quantity, account, slot, segment, dispatch-token and fill checks. A changed
price or missing acknowledgement aborts before wire; it never refreshes a
timestamp without asking the strategy. The serial lane remains the only
dispatcher. This also covers an initial-risk check that ages before pre-wire.

Local RPG aborts need a distinct stored status and client-origin audit with the
code. Historical rows are not edited. Controlled future broker acknowledgements
will remain explicitly simulated in the recorded OLOX/AIXI replay.
