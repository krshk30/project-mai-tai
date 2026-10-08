from dataclasses import replace

import pytest

from project_mai_tai.cancel_terminal_proof import (
    BookOrder, CancelReceipt, CancelScope, CancelTerminalEvidence, CompleteWorkingBook,
    evaluate_cancel_terminal, evaluate_request_cancel_terminal,
)

NOW = 1_791_466_919_000
SCOPE = CancelScope("webull", "actual-account", "DKI", "exact-coid", "cancel-event")
RECEIPT = CancelReceipt(SCOPE, NOW - 20_000, "rejected", "skipped_before_submit",
                        "cancel_target_not_found")
BOOK = CompleteWorkingBook("webull", "actual-account", NOW - 500, NOW - 100,
                           True, "all_working", (), "broker")
EVIDENCE = CancelTerminalEvidence(RECEIPT, BOOK, source="broker")


def evaluate(evidence=EVIDENCE, receipt=RECEIPT, **kwargs):
    return evaluate_cancel_terminal(receipt, evidence, now_ms=NOW, **kwargs)


def test_local_refusal_requires_current_complete_broker_book():
    assert evaluate().terminal
    assert not evaluate(None).terminal
    assert not evaluate(replace(EVIDENCE, book=None)).terminal


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"complete": 1}, {"coverage": "today"}, {"source": "cache"},
    {"source": "sql"}, {"account_id": "foreign"}, {"account_name": "foreign"},
    {"started_at_ms": NOW - 15_001}, {"started_at_ms": RECEIPT.observed_at_ms - 1},
    {"finished_at_ms": NOW + 1}, {"orders": None},
])
def test_unreadable_partial_stale_or_mismatched_books_unknown(changes):
    assert not evaluate(replace(EVIDENCE, book=replace(BOOK, **changes))).terminal


@pytest.mark.parametrize("status", ["working", "pending", "accepted", "submitting",
                                    "partially_filled", "pending_cancel", "pending_replace", "new"])
@pytest.mark.parametrize("coid", [SCOPE.client_order_id, "another-generation"])
def test_real_working_same_symbol_never_terminal(status, coid):
    book = replace(BOOK, orders=(BookOrder(coid, "DKI", status),))
    assert not evaluate(replace(EVIDENCE, book=book)).terminal


@pytest.mark.parametrize("status", ["cancelled", "rejected", "expired"])
def test_broker_terminal_target_receipt(status):
    evidence = replace(EVIDENCE, book=None, target_status=status,
                       target_observed_at_ms=NOW - 100,
                       target_client_order_id=SCOPE.client_order_id,
                       target_symbol=SCOPE.symbol, target_account_id=SCOPE.account_id,
                       target_filled_quantity="0")
    assert evaluate(evidence).terminal
    assert not evaluate(replace(evidence, source="client")).terminal
    assert not evaluate(replace(evidence, target_account_id="foreign")).terminal


@pytest.mark.parametrize("status", ["filled", "partially_filled", "pending_cancel", "unknown"])
def test_fill_or_pending_target_never_terminal(status):
    evidence = replace(EVIDENCE, target_status=status, target_observed_at_ms=NOW - 100,
                       target_client_order_id=SCOPE.client_order_id,
                       target_symbol=SCOPE.symbol, target_account_id=SCOPE.account_id)
    assert not evaluate(evidence).terminal


def test_broker_rejected_cancel_request_is_not_rejected_target():
    receipt = replace(RECEIPT, refusal_origin="broker_reject", refusal_code="cannot_cancel")
    assert not evaluate(replace(EVIDENCE, receipt=receipt), receipt).terminal


def test_receipt_revision_and_exact_filled_identity_are_fenced():
    assert not evaluate(replace(EVIDENCE, receipt=replace(RECEIPT, observed_at_ms=NOW))).terminal
    book = replace(BOOK, orders=(BookOrder(SCOPE.client_order_id, "DKI", "filled"),))
    assert not evaluate(replace(EVIDENCE, book=book)).terminal


def test_default_freshness_is_fifteen_seconds_and_cannot_be_loosened():
    assert evaluate(replace(EVIDENCE, book=replace(BOOK, started_at_ms=NOW - 15_000))).terminal
    assert not evaluate(freshness_ms=15_001).terminal


def test_every_request_account_must_prove():
    second = replace(RECEIPT, scope=replace(SCOPE, account_name="schwab", account_id="hash",
                                          event_id="schwab-cancel"))
    bindings = {"webull": "actual-account", "schwab": "hash"}
    proofs = evaluate_request_cancel_terminal([RECEIPT, second], {SCOPE.event_id: EVIDENCE},
                                              account_ids=bindings, now_ms=NOW)
    assert [p.terminal for p in proofs] == [True, False]
    with pytest.raises(ValueError, match="accounts_unknown"):
        evaluate_request_cancel_terminal([RECEIPT], {}, account_ids=bindings, now_ms=NOW)
    with pytest.raises(ValueError, match="accounts_unknown"):
        evaluate_request_cancel_terminal([], {}, account_ids={}, now_ms=NOW)
