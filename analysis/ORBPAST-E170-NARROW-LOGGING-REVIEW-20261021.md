# ORBPASTLEVEL1-20261021: Narrow Logging Correction And Fixture Proposal

HISTORICAL a11 CHECKPOINT. Ten quote-input amendment proposals below are
SUPERSEDED/NOT AUTHORIZED by17:47:30: revise applicability in source instead;
original ten tests stay byte-identical. New feature test capture-after-init IS
authorized. See ORBPAST-FRESH-ASK-APPLICABILITY-IMPACT-20261021.md. Historical
cause evidence retained, no retroactive14 GREEN or replay acceptance claim.

Intake source: direct main changes-requested message after full e170 review.
Priority: urgent regression source correction; owner: Gibbs sole ORB author.
Parent e170dc96cb21220323a62a29cd44f466a4b3c41f is FROZEN, not amended.
Worktree: /private/tmp/mai-tai-orbpast-e170-narrow-logging-20261021.
Branch: codex/orbpast-e170-narrow-logging-20261021.
Status: SOURCE-AUTHORED / REVIEW AND ALL RUNTIME GATES UNRUN.
Next action: main reviews this descendant and requests reviewer fixture
permission; no worker execution and no change to existing tests is authorized.
Completion criterion for this bounded correction: remove rejected logging
hunks, retain three valid corrections and publish exact evidence/impact and
fixture proposal. This is NOT overall original14 or original11 acceptance.
Main owns queue/status/LATEST and serial qualification; worker does not edit them.

## Source Disposition

log.py and oms/service.py are restored byte-for-byte to7ef. Production logging
still calls basicConfig(force=True), establishes the configured root level and
stdout handler, and installs OMS Webull request redaction. No preserve_handlers
parameter, named-logger level override or existing-handler detection remains.
This avoids an ERROR-filtered existing handler suppressing the intended INFO
sink. No change to logger initialization guarantees is justified by caplog.

The original fourteen-node classification is retained verbatim as historical
pre-edit evidence. Its proposed production correction for E is SUPERSEDED by
this review: E is a separate fixture-harness amendment, not production policy.
The new author-only control wraps route._service, attaches caplog.handler only
AFTER initialization, invokes the original function with ALL assertions intact,
and removes only the handler it added. No old test file is edited.

Retained exactly from e170: Schwab legacy HTTP method interface with task-local
prewire fence; legacy unmarked STOP_LIMIT scope; Webull one-argument geometry.
Call graph and safety scope are in the revised SOURCE-CORRECTION-IMPACT file.
No missing/freshness/cap fallback, actor retirement or broker hook was added.

## Exact Ten Quote-Input Nodes (Reviewer Permission Pending)

Paths and line anchors below refer to this source worktree; old files match7ef.
Each ID includes its actual pytest parameter suffix from the original RED XML.

1. tests/unit/test_l8_orb_control_envelope.py::test_real_oms_with_native_run_control_submits_one_selected_parent[True]
   Input site: function184, chain.setup85 constructs service without quotes;
   event188 uses ABC. Supply ABC quote after setup/selection before process191.
2. tests/unit/test_orbpurple1_atr_entry.py::test_oms_rechecks_atr_after_preview_and_records_local_refusal
   Input site: _service220 imports route helper161; helper FreshQuotes178 only
   recognizes CLRO, but event225 requests JAGX. Supply JAGX before process226.
3. tests/unit/test_orbpurple1_atr_entry.py::test_oms_real_schwab_history_passes_both_fills_and_blocks_purple[MI-2026-10-05-True]
   Input site: _service247 / route helper161, history clock255-256; event267
   requests MI. Supply MI quote AFTER controlled clock replacement, before268.
4. tests/unit/test_orbpurple1_atr_entry.py::test_oms_real_schwab_history_passes_both_fills_and_blocks_purple[JAGX-2026-10-06-True]
   Same helper/clock/event sites as3, selected symbol JAGX. ATR history is not
   NBBO; use an explicit synthetic unit-fixture quote, not an inferred bar ask.
5. tests/unit/test_orbroute1_manual_net_092.py::test_manual_thousand_shares_allows_routed_ten_and_logs_without_receipt[live:schwab_1m_v2-True]
   Input site: function27/setup28 -> chain.setup85, event30-31 uses ABC;
   supply ABC quote before process33, leaving manual1000/routed10 assertions.
6. tests/unit/test_orbroute1_oms_chain.py::test_real_oms_one_selected_parent_ten_shares_no_mirror[True-live:schwab_1m_v2]
   Input site: function132/setup133, quote-free setup85-116; event135 uses ABC.
   Supply ABC before process137; country/no-mirror/native geometry unchanged.
7. tests/unit/test_orbroute1_session_admission.py::test_actual_oms_entry_uses_session_aware_wrapper[order-previous-13-28-live:schwab_1m_v2-True]
   Input site: function166/setup169, controlled_time170, event173 uses ABC.
   Supply ABC at returned now AFTER170 before process175, not fixed chain.OPEN.
8. tests/unit/test_orbroute1_session_admission.py::test_actual_oms_entry_uses_session_aware_wrapper[order-previous-13-28-14-live:schwab_1m_v2-True]
   Same site as7; use its actual13:28:14 controlled now, not13:28 (14s stale).
9. tests/unit/test_orbroute1_session_admission.py::test_actual_oms_entry_uses_session_aware_wrapper[unknown-previous-13-28-live:schwab_1m_v2-True]
   Same site as7, separate unknown owner kind; do not alter foreign row/state.
10. tests/unit/test_orbroute1_session_admission.py::test_actual_oms_entry_uses_session_aware_wrapper[unknown-previous-13-28-14-live:schwab_1m_v2-True]
    Same site as8, separate unknown owner kind; preserve row-status assertion.

## Minimal Input-Only Amendment Proposal (NOT APPLIED)

Prefer explicit per-node/selected-branch snapshots, not a broad shared fixture
that gives every symbol fresh quotes on every lookup. In nodes1,5,6, selected
Schwab True branch only, before processing, propose a test-input assignment:

```python
service._latest_quotes_by_symbol["ABC"] = {
    "ask": "4.99", "bid": "4.99", "received_at": oms_module.utcnow(),
}
```

In node2 use JAGX ask/bid6.50 at the controlled clock after helper construction.
In nodes3,4 use selected symbol ask/bid6.50 at explicit historical `now` AFTER
clock replacement, only `allowed is True`. In nodes7-10 use ABC4.99 at returned
`now`, only `age == "previous" and is_us is True`. A reviewer may prefer
explicit fixture opt-in injection with the same limited scope. Synthetic unit
inputs are NOT actual historical quotes, freshness, fills or native outcomes.
No new production code or historical-data claim is proposed. No refresh-on-get,
universal fallback, P/clock invention, assertion relaxation or skipped node.
Leave every deliberately missing/stale/changed-during-preview/above-cap quote
test untouched. Preserve owner/permit/quantity/ATR/post-preview/native and
negative-path assertions; actual qualification still requires reviewer tests.

## Separate Log-Capture Node Proposal (NOT APPLIED)

tests/unit/test_orb_schwab_order_route.py::test_pastlevel_actual_oms_placement_shapes[1.22-None-0]

Input/harness site280 sets caplog level BEFORE _service281, whose OMS
constructor force-configures logging and removes the root capture handler.
Quote282 already supplies the intended OLOX1.22 at OPEN; quote input does NOT
need amendment. After _service returns, attach `caplog.handler` to root if
absent and set capture INFO, with fixture cleanup of only that added handler.
Keep ALL four parameters and every original assertion, especially NO_SEND,
reason, trigger/ask/cap, zero submits and persisted local_zero. No weaker log
assertion, no production sink/level change. Reviewer must authorize this
separately from the ten quote-input amendments; old source remains unchanged.

## Blockers, Evidence And Saved Scope

Immutable original JUnit: ORBPAST-7ef8-REVIEW-JUNIT-20261021.xml,
SHA256 cea25f6d7952d21b03fd2ed48d3ea8ad56abc33d0680e1c3a6271312bbfa922c.
Original14 RED remains RED; ten quote nodes and one logging harness node await
reviewer permission. Three source corrections and21 intended new control
cases are UNRUN; no PASS from static diff/hash inspection.

Adapter checkpoint551dcecfea65710389deac0eec7314be7cd3fffd is preserved at
/private/tmp/mai-tai-orbpast-input-adapter-20261021. Resume point: after source
review, VERSION source pins from frozen7ef to approved candidate; obtain
original plan/P/clock/settings/native/state/exit-continuation horizons and
reconcile all22 raw records including APUS/GENK without invented parentage.
Original11 plus MI/JAGX/FLYE/GIXI unchanged gates remain owed. No worker
replay/test/import/compile/AST/runtime/DB/PG/provider/SQL/network/SSH/orders/
push/install/restart. Source+impact/native/full/ALLON/restart04/GO remain open.
