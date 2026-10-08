"""Exact cancel evidence shared by request consumers; never a flatness proof."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FRESHNESS_MS = 15_000
TERMINAL = frozenset({"cancelled", "canceled", "expired", "rejected"})
WORKING = frozenset({"working", "pending", "submitting", "accepted", "new",
                     "partially_filled", "pending_cancel", "pending_replace"})


@dataclass(frozen=True)
class CancelScope:
    account_name: str
    account_id: str
    symbol: str
    client_order_id: str
    event_id: str


@dataclass(frozen=True)
class CancelReceipt:
    scope: CancelScope
    observed_at_ms: int
    status: str
    refusal_origin: str
    refusal_code: str


@dataclass(frozen=True)
class BookOrder:
    client_order_id: str
    symbol: str
    status: str


@dataclass(frozen=True)
class CompleteWorkingBook:
    account_name: str
    account_id: str
    started_at_ms: int
    finished_at_ms: int
    complete: bool
    coverage: str
    orders: tuple[BookOrder, ...]
    source: str = "unknown"


@dataclass(frozen=True)
class CancelTerminalEvidence:
    receipt: CancelReceipt
    book: CompleteWorkingBook | None
    source: str = "unknown"
    target_status: str = ""
    target_observed_at_ms: int = 0
    target_client_order_id: str = ""
    target_symbol: str = ""
    target_account_id: str = ""
    target_filled_quantity: str = ""


@dataclass(frozen=True)
class CancelTerminalProof:
    scope: CancelScope
    observed_at_ms: int
    terminal: bool
    reason: str


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value


def evaluate_cancel_terminal(
    expected: CancelReceipt,
    evidence: CancelTerminalEvidence | None,
    *,
    now_ms: int,
    freshness_ms: int = FRESHNESS_MS,
) -> CancelTerminalProof:
    """Accept a broker terminal target or a local refusal plus a fresh complete book.

    Freshness limits an acquisition, never the lifetime of a pending request.
    Consumers must separately fence dispatch, fill, position, and request ownership.
    """
    def result(reason: str, terminal: bool = False) -> CancelTerminalProof:
        return CancelTerminalProof(expected.scope, now_ms, terminal, reason)

    scope = expected.scope
    if not all(_identity(v) for v in (
        scope.account_name, scope.account_id, scope.symbol, scope.client_order_id, scope.event_id,
    )):
        return result("cancel_identity_unknown")
    if (type(expected.observed_at_ms) is not int or expected.observed_at_ms <= 0
            or type(now_ms) is not int or type(freshness_ms) is not int
            or not 0 < freshness_ms <= FRESHNESS_MS or expected.observed_at_ms > now_ms):
        return result("cancel_time_unknown")
    if not isinstance(evidence, CancelTerminalEvidence):
        return result("complete_book_unavailable")
    if evidence.receipt != expected:
        return result("cancel_receipt_changed")
    if evidence.source != "broker":
        return result("cancel_source_unknown")
    if evidence.target_status:
        if (evidence.target_client_order_id, evidence.target_symbol, evidence.target_account_id) != (
            scope.client_order_id, scope.symbol, scope.account_id,
        ):
            return result("broker_target_identity_mismatch")
        if (type(evidence.target_observed_at_ms) is not int
                or not expected.observed_at_ms <= evidence.target_observed_at_ms <= now_ms):
            return result("broker_target_time_unknown")
        if evidence.target_status not in TERMINAL:
            return result("broker_target_not_terminal")
        if evidence.target_filled_quantity != "0":
            return result("broker_target_fills_unknown_or_present")
    else:
        if expected.status != "rejected" or not (
            expected.refusal_code == "cancel_target_not_found"
            or expected.refusal_origin == "skipped_before_submit"
        ):
            return result("cancel_result_not_positive")
        if evidence.book is None:
            return result("complete_book_unavailable")

    book = evidence.book
    if book is not None:
        if not isinstance(book, CompleteWorkingBook):
            return result("book_incomplete")
        if (book.account_name, book.account_id) != (scope.account_name, scope.account_id):
            return result("book_account_mismatch")
        if book.complete is not True or book.coverage != "all_working" or book.source != "broker":
            return result("book_incomplete")
        if not isinstance(book.orders, tuple):
            return result("book_row_unknown")
        if (type(book.started_at_ms) is not int or type(book.finished_at_ms) is not int
                or not expected.observed_at_ms <= book.started_at_ms <= book.finished_at_ms <= now_ms
                or now_ms - book.started_at_ms > freshness_ms):
            return result("book_stale_or_pre_cancel")
        seen: set[str] = set()
        for order in book.orders:
            if (not isinstance(order, BookOrder)
                    or not all(_identity(v) for v in (
                        order.client_order_id, order.symbol, order.status,
                    )) or order.client_order_id in seen
                    or order.status not in TERMINAL | WORKING | {"filled"}):
                return result("book_row_unknown")
            seen.add(order.client_order_id)
            if order.client_order_id == scope.client_order_id:
                if order.symbol != scope.symbol:
                    return result("book_target_identity_mismatch")
                if order.status == "filled":
                    return result("book_target_filled")
                if order.status in WORKING:
                    return result("book_target_working")
            if order.symbol == scope.symbol and order.status in WORKING:
                return result("book_symbol_working")
    return result("cancel_terminal_broker_receipt" if evidence.target_status
                  else "cancel_terminal_complete_book", True)


def evaluate_request_cancel_terminal(
    expected: Sequence[CancelReceipt],
    evidence: Mapping[str, CancelTerminalEvidence],
    *,
    account_ids: Mapping[str, str],
    now_ms: int,
    freshness_ms: int = FRESHNESS_MS,
) -> tuple[CancelTerminalProof, ...]:
    """Require one exact receipt for every request account, with no foreign accounts.

    Empty or mismatched account sets raise rather than make ``all(())`` a proof.
    """
    bindings = [(r.scope.account_name, r.scope.account_id) for r in expected]
    if (not account_ids or len(bindings) != len(set(bindings))
            or len({r.scope.event_id for r in expected}) != len(expected)
            or len({r.scope.symbol for r in expected}) != 1
            or dict(bindings) != dict(account_ids)):
        raise ValueError("cancel_request_accounts_unknown")
    return tuple(evaluate_cancel_terminal(
        receipt, evidence.get(receipt.scope.event_id), now_ms=now_ms, freshness_ms=freshness_ms,
    ) for receipt in expected)
