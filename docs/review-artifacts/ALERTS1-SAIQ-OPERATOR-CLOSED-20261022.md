# ALERTS1-SAIQ-OPERATOR-CLOSED-20261022: source checkpoint / ALL UNRUN

Base690, sole Maxwell lane. Explicit Claude14:59:39/operator ruling only. No L9
attach/protection/ownership investigation repair; no OMS, fill, order, position,
schema or incident-status changes. No deployment/DB/provider actions by author.

Exact historical allowance binds live:orb/Webull/SAIQ, buy39@7.62 in second
2026-10-22T15:22:04Z, native entry7HG0RUDTB5R0DFMUFRELAST5G8. Native entry and
managed row6e641bb1-b7c2-4a3c-b41f-2cfcb0ba940b come from retained sanitized
SAIQ-OMS-1121-1123-20261022.log. Flat/manual intent/VIRTUAL-CLEAR15:28:36Z are
positive reviewed operator facts from14:59:39, NOT invented wire-read timestamps.
No absence-of-position/manual-intent inference. Future entries require a separate
explicit reviewed positive ruling and exact native binding through the same API.

Original OCO aggregate predicate unchanged; its fourth output now returns IDs
instead of a count. Only intersection with those exact IDs can reduce the phone
count. Historical audit/tag lookup also runs after CURRENT_DATE rolls, so an
after-midnight install can annotate this decided historical item. Any later bot
sell order/intent/fill or contradictory nonzero native/virtual/managed books veto
the exclusion. Rejected/refused sell attempts also conservatively veto. A real
bot sale with missing exit Fill therefore still pages. $0-exit/AMBER/selftest/
phone priority and thresholds unchanged. Unknown evidence/write failure rolls
back the savepoint and keeps original RED; not a phone-path exception waiver.

One INFO operator-closed audit per entry, serialized by matching Fill NOWAIT
row lock on PostgreSQL. SQLite sequential-repeat controls are NOT native PG
concurrency proof. Audit failure => no phone-count subtraction. It writes no
synthetic exit fill. Existing payloads preserved. Only exact entry-bound SAIQ
incidents in the three reviewed UTC minutes15:22/15:28/15:35 become INFO, same
operator-closed tag, no closed/status mutation. Missing/foreign/multi-handle
payload binding stays critical. No broad SAIQ symbol/day whitelist.

Successor source binding: Claude15:31:49 delivered the original three incident
rows plus the full22-line OMS window and fills receipt. All3 SHA256SUMS matched
by file-only hashing. Exact incident packet is retained byte-for-byte at
tests/fixtures/alerts1_saiq_incidents_20261022.jsonl, SHA256
a36f30d7095954c579b241a025343fe2f662f37324323c84d518689db1406a59.
OMS receipt SHA86ad1d09d30d6cbae3e330434fc6aa7ac6a0de3b6ff94e66f361ef13b80eb63b;
fills SHA390ecd330a5eebdf171ab89352b95946ed6f09f50c089f9c190626c7fddf5545.
VIRTUAL-CLEAR is actually logged15:28:36.242; WBATTACH at15:35:04.877 says
broker flat before software exit, sell=0. The fills receipt has only the
15:22:04.337 buy39@7.62. Historical manual intent remains the positive operator
ruling, not inferred from absence of a fill.

Frozen86e7's generic alias matcher misses the actual reconciler row (account_name,
no entry alias). The actual WBATTACH entry_id is not assumed to be BrokerOrder.id.
Successor binds precisely the reviewed UUIDs463793e7/67fcff97/d2711858, service,
full opened_at and full original payload equality. WBATTACH additionally checks
the recorded client_order_id against the matching native entry's BrokerOrder.
Only this audit's own two annotations may be present on repeat. Any evidence
drift or extra identity keeps that incident critical. No symbol/time fallback
for these three exact bindings; generic aliases remain only for separately
reviewed future rulings without exact-row bindings. Title/severity are the only
incident fields changed beyond the two preserved-evidence annotations.
No actual DB mutation or phone silencing is claimed from this source work.

Impact:15-minute OCO health process only; bounded native-entry fill lookup,
per-reviewed-entry local evidence reads and one audited INFO transaction. No
tick/quote/broker connection or new source in OMS. Unreviewed rows retain RED.
No performance PASS from source; main owns functional/existing low-priority/
ALERTS1 preservation qualification and controlled PG race proof if required.

Intended controls:38 (existing18 plus20 additive), all UNRUN (not collected).
The existing three-incident control now uses the actual retained row fixture,
still verifies unrelated incident critical, payload/status preservation and
once audit. New18 drift controls cover each actual row's UUID/service/clock/
account/extra identity/foreign allowance;2 controls cover actual WB client
identity and bot-sell veto with the actual packet. These are controlled SQLite
tests using real sanitized payloads, NOT proof the production DB was changed.
Module:
tests/unit/test_alerts1_saiq_operator_closed.py. Proposed MAIN command on reviewed
composition: existing .venv/bin/python -m pytest -q that WHOLE module plus
test_alerts1_flat_incidents_and_allowances.py and test_low_priority_alerts.py,
fresh raw/XML and serial claim. No worker test/import/AST/compile/DB/runtime.

Main-only proposed serial command on a reviewed composition:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m pytest -q
tests/unit/test_alerts1_saiq_operator_closed.py
tests/unit/test_alerts1_flat_incidents_and_allowances.py
tests/unit/test_low_priority_alerts.py --junitxml=<fresh-main-owned.xml>
Capture raw stdout/stderr to a fresh main-owned file. Native PostgreSQL once-only
concurrency and actual three incident effects remain UNRUN; no author execution.

Finite L13 source assessment completed: persistent last-spike/ticker/cooldown
memory explains why five-minute price history is not native state equivalence.
Claude15:31:49 explicitly replaces native-output comparison with full120-batch
cold OLD/NEW parity plus per-batch payload byte equality. Earlier actual203-vs0
failure is preserved. No native historical state invented, no worker replay.
