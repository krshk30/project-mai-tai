# T43: one-leg recovery and fresh pre-wire authorization

Independent cause verdict: AGREE with the corrected local-abort cause. Own
11:05 ET count is 2/78 token-bearing OPENs. Exact authorization-to-final-check
p99 is UNMEASURED: the authorization field is mutable and completed_at is not
the check timestamp. The fix requests a new current v2 decision instead of
widening the one-second authorization guard. No production action.

## Behaviour and safety

Only a refused replacement with positively cleared old order, no replacement
fill, matching account/slot/segment/generation and a later completed bar reaches
ordinary placement. The producer is scoped to the absent account; the surviving
leg's generation, quantity and wire prices remain unchanged. Existing ownership,
window, price, liquidity, sizing, fill and uncertain-dispatch gates still run.

On a stale check, OMS releases its transaction, requests a nonce, and waits for
v2's real current-gate evaluation using the existing two-second read budget.
After acknowledgement it reacquires the shared-account advisory lock and repeats
ORB collision and risk admission as well as the existing canonical-price,
quantity, slot, segment, dispatch-token, freshness and fill checks. No clock
stamp is forged. A changed price or an unreadable acknowledgement aborts.

Local RPG refusals now store order and intent status=aborted, the client-origin
code, an aborted audit event and [OMS-RPG1-ABORT]. Real venue refusals remain
rejected with broker-origin reason. No historical rows or ledger are edited.
Crash recovery accepts an aborted replacement only with its exact bound identity,
zero fill, no broker id, matching client-origin intent and client audit, and
positively cleared old order. Unproven rows remain submit_unknown.
The same read-only proof is used by v2's in-flight-position and unknown-owner
readers and by NFQ retirement. An unproven aborted row remains blocking; it is
not treated as a flat or cleared broker order. Companion pending-latch readers
understand the new local terminal status. Broker cancel-proof rules are unchanged.

## Recorded acceptance and disclosed controls

| Case | Recorded inputs | Assertion / limitation |
|---|---|---|
| OLOX Schwab10:15 ->10:16 | Ticket c9b13cc4; auth age0.822015 at submit,1.085075 at completion; trail1.456585, bar14:14Z, capture bid1.36/ask1.37 | Named timing replay obtains a new nonce and retains identity; ordinary later-bar replay emits primary only. Later acceptance is simulated, not proof of a fill or profit. |
| AIXI Webull09:45 -> next bar | Ticket a5b034cb; auth age0.913296 at submit,1.340543 at completion; trail2.944435, bar13:44Z, capture bid2.50/ask2.53 | Named timing replay obtains a new nonce with unchanged identity; later-bar replay emits mirror only. Capture ask2.53 is outside the unchanged Webull distance rule versus stop2.9592, so live placement/acceptance is NOT claimed. |
| Interrupted committed client abort | Existing real runtime submit path, controlled interruption after its durable abort audit | Exact audited no-wire replacement releases; missing audit, wrong audit source/token, broker id or unknown origin stays blocked. |
| Concurrent ORB admission during nonce wait | Explicit controlled competing accepted ORB order, same account/symbol | v2 aborts, no second broker open; the ORB order remains accepted and unchanged. |
| All-on / flip owner | Eight candidate switches true plus controlled restored owner/position evidence | Either absent account may be repaired once; sibling remains unchanged, no bypass of first-slot ownership. |

The named race test uses the six-decimal retained ATR probe, not the four-decimal
metadata line. Initial harness failures exposed zero cached notional, millisecond
decision-clock precision and the rounded AIXI trail; each fixture was corrected
to the stated recorded/live values without relaxing production comparisons.

RPG broker hand-offs are RTH-only. A pre-market software reprice does not submit
a broker replacement and therefore cannot create this replacement-refused ticket.
Installed pre-market PMREST/PMPRINT/PMFLIP tests and the all-on composition remain
required regressions; no extra pre-market entry path or reclaim switch is added.
The next-bar sibling/quote inputs and future venue responses are controlled;
capture quotes do not attest what was in the OMS decision cache.

## Verification

As of 2026-10-06 11:35 ET, target review-ready:2026-10-06 15:00 ET. Not ready until
final suite and CI receipts. Early focused runtime/composition:272 PASS;
expanded PM/NFQ/fanout set545 PASS before the last abort-reader proof refinement.
Final new tests26 PASS, including named races, both legs, all-on/flip owner,
positive exact abort recovery and negative reader cases. The final frozen full
unit suite and15-mutation run are running; no final suite PASS claimed yet.
The preceding pre-reader14-mutation run was assertion-RED; refreshed targets cover:
missing-leg branch, sibling scope, generation, refresh request, age guard,
canonical price, order/intent abort classification, post-wait ORB collision,
abort crash recovery, client audit source, generation, broker id, origin and
in-flight reader. Initial
mutation target/None-audit errors were NOT counted RED; corrected targets require
AssertionError, not an import/compile exception.

Full comparison baseline: exact main3ebde364, independent fresh unit run
/tmp/orbpurple1-unit-base.log:47 FAIL /6416 PASS; raw failed names will be compared
with the final T43 run, not with an assumed 56-failure historical baseline.
Linux CI remains authoritative for its own environment; both Validate runs must
be green before ready. PostgreSQL advisory-lock operation is UNEXERCISED locally
(SQLite race simulation only); no live broker dispatch or live latency claim.

## Own raw paths

| Receipt | SHA256 |
|---|---|
| /tmp/codex-t43-latest-named-quotes-20261006.jsonl | fbccea847e35d93cc99e4615dd195e29e233b89eb538317b679767544ea7ce5e |
| /tmp/codex-t43-named-bars-20261006.jsonl | 6c83499fb2138e8768797a0b401e90935cae7ffa4bec476142aa217eb2ece0b9 |
| /tmp/codex-t43-aixi-probe-20261006.txt | b794e859f7612895dc1e9527d355ede92115c24ecf874a93bf050c56308b9747 |
| /tmp/codex-t43-olox-probe-20261006.txt | 801ab6422c21210e70170c395d928a9521cdaeab2af1992212551beec97c415a |

Queries were BEGIN READ ONLY, timeout5s, two names, one last available row each;
no snapshot-batches read. Initial global LIMIT50 quote query omitted OLOX; the
per-name bounded lateral query corrected that, not treated as an empty market.

Restart list if pinned: OMS + v2 (coordinated with strategy companion in the
separately reviewed install plan). No migration, gateway/ORB/paper change or new
setting. The existing hand-off switch remains the rollback for new hand-offs;
reverting this source requires its own exact-SHA approval, not an automatic
rollback. Tonight's three-item set is unchanged until a separate pin and plan
binding explicitly include this PR.
