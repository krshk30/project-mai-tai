# ORBCOUNTRY1 connected replacement source follow-up

Task OCT28-1056-ORBCOUNTRY1-NEXT-BUILD, original Cicero.
Parent first-source075c958c09935459071f1fdc0dcdb0160cd21231 stays immutable.
Production orb_country.py bytes unchanged from075. No Settings/service writes.
All authored controls UNRUN; Main-only integration/execution/PR ownership.

## Required Main hook revision

`/private/tmp/OCT28-CICERO-ORBCOUNTRY1-MAIN-HOOKS-V2.proposed.diff`
retains the first proposal and adds ONLY a successful Schwab replacement
provenance assignment. Source32 `_process_orb_schwab_reprice` merges old and
new metadata into the same target row. A refreshed new event would overwrite
the old parent stamp on success. New flagged routed-parent assignment keeps
that original as_of while leaving replacement handle/order type/price and
new event/order-event receipt provenance intact. OFF is the old whole merge.
No ledger, quantity, stop/target, account, intent, bracket or ACK relaxation.
Connected tests explicitly assert the same parent UUID/COID, original stamp,
original quantity, original broker ID passed to replace, and no second submit.

V1 external proposal remains preserved9b94518a; V2 must be independently
reviewed by Main before composing any shared file. No branch auto-adoption.

## Frozen authored roster: 56, all UNRUN

Existing first file remains33 parameter-expanded cases. Follow-up changes its
Webull case helper to accept an explicit reprice_at (original four inputs and
default09:29 remain); defaultFalse test now checks model field default directly,
so deploy env TRUE cannot be mistaken for a changed code default.

New `tests/unit/test_orbcountry1_connected_reprice.py`: 23 expanded cases:

| Function prefix | Cases | Actual controlled consumer |
| --- | ---: | --- |
| enabled_semantics_keeps_original_owned_rth_attach_assertions | 2 | original owned3/10 RTH pair attach geometry and persisted phase |
| enabled_semantics_keeps_unknown_sibling_booking_without_owner_release | 1 | original child fill booking once / no release / no resend |
| enabled_semantics_preserves_actual_manual_position_rule | 2 | real OMS +1000manual excess, routed10 still permitted |
| enabled_semantics_never_borrows_foreign_bot_quantity | 2 | real OMS foreign VP veto on each broker |
| off_actual_oms_keeps_original_stamp_refusal_without_request | 2 | actual OMS, refreshed real store, OFF no request |
| actual_oms_changed_broker_or_ready_incomplete_never_dispatches | 4 | actual store/router/OMS, US/nonUS both ways and READY->INCOMPLETE both brokers |
| actual_webull_rth_replace_retains_zero_proof_and_parent_provenance | 2 | real OMS09:30:10; unknown cancel no replacement / terminal zero replacement |
| actual_webull_rth_cutoff_never_cancels_or_replaces_parent | 2 | actual OMS09:30:15 and09:30:16 no cancel/no new parent |
| actual_schwab_parent_replace_preview_prewire_and_unknown_fences | 6 | real OMS09:29 and09:30:10 x confirmed/unknown/rejected replacement SDK outcomes |

Every function name has `test_` prefix in source. Intended Main first command,
after literal composition and source review:

`python -m pytest -q tests/unit/test_orbcountry1_semantic_admission.py tests/unit/test_orbcountry1_connected_reprice.py`

No claim of a collection result. No first33 execution known to author here;
Main must avoid repeating anything already claimed elsewhere. Existing related
controls remain separate evidence; no blanket full suite requested.

## Fixture disclosure

Existing test_orbroute1_oms_chain setup supplies private SQLite and adapter
ports; completed-bar MACD and session-symbol block ports are controlled by
that original fixture. Original attach helper additionally controls its
placeability port. These are preserved controlled unit seams, not original
native facts or proof of a complete historical admission. New code does not
mock the real matcher/router/store/OMS replacement body/prewire/receipt handler.
Schwab added replacement port returns typed ExecutionReport at the SDK boundary;
unknown is None; rejection never gets classified accepted by a permission mock.
Unknown redelivery must not call replacement SDK again. No quote/clock policy
changed: explicit legal replace clock and no-fresh-ask legacy quote policy used
by the real selector, not a synthetic fresh quote or forced selected order.

## Historical C11131 disposition

Same six-case manifest amended in place:
`/private/tmp/OCT28-CICERO-ORBCOUNTRY1-SIX-CASE-INPUT-MANIFEST.json`.
C1 says complete callback packets do not exist; missing inputs UNKNOWN allowed.
Preactivation no-country case is unchanged control only; ELPW is accepted as
bound for timestamp comparison, never independent owner/dispatch permission.
Exact first runtime activation receipt remains unbound in inspected documents:
10-16 WB-inclusive installation instructions are conditional, while9df installed
report lacks exact ORB flag/process readback. GIXI original event no country
metadata is positively retained; it is not retrofitted. Other case applicability
and partial retained IDs/quotes/refusals are explicit in the manifest.
No newly authorized remote read was attempted. No new rig, capture or provider.
