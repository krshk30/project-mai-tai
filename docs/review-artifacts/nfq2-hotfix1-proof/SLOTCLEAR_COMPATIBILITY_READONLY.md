# SLOTCLEAR1 #1113 compatibility read only

Observed GitHub head: d68d25e0bf8cca33d6c22da39fa550b5977b5da3,
codex/slotclear1-fresh-flip-side, OPEN/DRAFT, base
1a70da19176d2cad486e8518e1c9030f1cabb968. Read local objects only after
verifying the PR head. No SLOTCLEAR source/worktree edit or composition performed.

Read sources at that exact head:
- docs/review-artifacts/slotclear1/BUILD_2026-10-07.md
- tests/unit/test_slotclear1_fresh_flip.py
- diff from f9c9bd33 for strategy_core/schwab_1m_v2.py, oms/service.py, settings.py

The stated policies are compatible: fresh SLOTCLEAR first-slot entries retain a
0.5% ask cap; only an authoritative serial NFQ2 held dispatch receives 1%.
SLOTCLEAR's OMS cap condition explicitly excludes _nfq2_held_dispatch(event).
NFQ2 validates a hold's token/row/account/slot/segment rather than trusting
incoming metadata. This is source-level policy agreement, not integration proof.

SLOTCLEAR's fresh-first branch produces shared fanout identity using eh_resting
while preserving execution as a reactive draft. Both patches edit the same
_cw_v2_quote shared-identity condition: SLOTCLEAR admits dual-fanout OR fresh-first;
NFQ2 admits dual-fanout OR its enabled flag. The eventual composition needs both
conditions. Both touch OMS reactive cap logic, settings, catalog counts and
associated all-on/counter tests. Independent count deltas cannot be added as
proof of the merged catalog.

Required joint tests: actual SLOTCLEAR primary/mirror generated metadata enters
NFQ2 on missing OMS ask, fresh 0.5% refusal versus held 1% retry on each applicable
pricer, slot/segment/account identity, scanner removal and BUY/SELL replacement
after NFQ2 feedback marks resting_active, and generation-aware cancellation.
SLOTCLEAR's current OMS replay calls a pricer directly and does not exercise
NFQ2 process_trade_intent, real quote-handler retry, serial delivery or its
durable feedback consumer. The BIYA combined benchmark seeds a hold directly
and cannot establish those producer lifecycle properties.

NFQ2's software-rest cancel producer requires resting_flip_ms; SLOTCLEAR's fresh
reactive first producer has separate owner/flip fields. That cross-lifecycle
case is unproven here; no production defect or compatibility PASS is asserted
without a real composed-tree replay. Exact NFQ2+SLOTCLEAR integration remains
a parent-review blocker. No SLOTCLEAR tests were imported into this proof tree.
