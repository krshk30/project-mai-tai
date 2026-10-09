"""Exact cancel evidence shared by request consumers; never a flatness proof."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
from uuid import UUID

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
    side: str = "unknown"
    broker_order_id: str = ""


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
    if not isinstance(evidence.target_status, str):
        return result("broker_target_status_unknown")
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
            or len({name for name, _account_id in bindings}) != len(bindings)
            or len({r.scope.event_id for r in expected}) != len(expected)
            or len({r.scope.symbol for r in expected}) != 1
            or dict(bindings) != dict(account_ids)):
        raise ValueError("cancel_request_accounts_unknown")
    return tuple(evaluate_cancel_terminal(
        receipt, evidence.get(receipt.scope.event_id), now_ms=now_ms, freshness_ms=freshness_ms,
    ) for receipt in expected)


@dataclass(frozen=True)
class UnboundCancelRequest:
    symbol: str
    request_id: str
    token: str
    generation: str
    purpose: str
    requested_at_ms: int
    account_ids: Mapping[str, str]
    account_providers: Mapping[str, str] = field(default_factory=dict)
    session_key: str = ""
    opportunity_started_at_ms: int = 0
    coverage_process_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NeverSentScope:
    account_name: str
    account_id: str
    symbol: str
    generation: str
    opportunity_started_at_ms: int
    request_id: str
    request_token: str
    requested_at_ms: int
    coverage_process_id: UUID
    session_key: str = ""


@dataclass(frozen=True)
class NeverSentWitness:
    scope: NeverSentScope
    observed_at_ms: int
    coverage_started_at_ms: int
    never_sent: bool
    reason: str


@dataclass(frozen=True)
class TerminalSubmissionWitness:
    """Exact broker-terminal attempts with the same durable admission fence."""
    scope: NeverSentScope
    observed_at_ms: int
    coverage_started_at_ms: int
    terminal: bool
    client_order_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SchwabLocalCancelWitness:
    """Consumer-owned causal closure, NOT broker inventory or SQL-absence alone.

    Ordinary and replacement BUY submissions can precede durable journal writes.
    The consumer must cover those paths or leave no_unjournaled_buy false.
    """
    request: UnboundCancelRequest
    account_name: str
    account_id: str
    session_key: str
    observed_at_ms: int
    closure_kind: str
    publication_closed: bool
    no_unjournaled_buy: bool
    no_live_buy: bool
    no_inflight_buy: bool
    no_unanswered_cancel: bool


@dataclass(frozen=True)
class UnboundCancelFences:
    request: UnboundCancelRequest
    no_inflight_buy: bool
    no_unanswered_cancel: bool
    owned_rows_closed: bool
    request_cas_current: bool


@dataclass(frozen=True)
class UnboundCancelTerminalProof:
    request: UnboundCancelRequest
    observed_at_ms: int
    terminal: bool
    reason: str


def evaluate_unbound_cancel_terminal(
    expected: UnboundCancelRequest,
    books: Mapping[str, CompleteWorkingBook | None],
    *,
    fences: UnboundCancelFences | None,
    schwab_witness: SchwabLocalCancelWitness | None = None,
    never_sent_witnesses: Mapping[str, NeverSentWitness] | None = None,
    terminal_submission_witnesses: Mapping[str, TerminalSubmissionWitness] | None = None,
    now_ms: int,
    freshness_ms: int = FRESHNESS_MS,
) -> UnboundCancelTerminalProof:
    """Separate approved unbound rule, never an invented client-order binding.

    The consumer supplies its transaction's exact-request closure/CAS witness.
    This proof does not authorize position changes or bypass owned open rows.
    """
    def result(reason: str, terminal: bool = False) -> UnboundCancelTerminalProof:
        counts = ",".join(sorted(
            f"{expected.account_providers.get(name, name)}:{len(book.orders) if isinstance(book, CompleteWorkingBook) and isinstance(book.orders, tuple) else '?'}"
            for name, book in books.items()))
        logging.getLogger(__name__).info(
            "[V2-CANCEL-TERMINAL] sym=%s request=%s bound=0 decision=%s reason=%s books=%s",
            expected.symbol, expected.request_id, "TERMINAL" if terminal else "UNKNOWN", reason, counts,
        )
        return UnboundCancelTerminalProof(expected, now_ms, terminal, reason)

    if (not all(_identity(v) for v in (expected.symbol, expected.request_id, expected.token,
                                     expected.generation, expected.purpose))
            or len(expected.account_ids) != 2 or not set(books) <= set(expected.account_ids)
            or not all(_identity(n) and _identity(i) for n, i in expected.account_ids.items())):
        return result("unbound_request_identity_unknown")
    if (set(expected.account_providers) != set(expected.account_ids)
            or set(expected.account_providers.values()) != {"schwab", "webull"}):
        return result("unbound_request_broker_set_unknown")
    if (type(now_ms) is not int or type(expected.requested_at_ms) is not int
            or not 0 < expected.requested_at_ms <= now_ms or type(freshness_ms) is not int
            or not 0 < freshness_ms <= FRESHNESS_MS):
        return result("unbound_request_time_unknown")
    if (not isinstance(fences, UnboundCancelFences) or fences.request != expected
            or any(v is not True for v in (fences.no_inflight_buy, fences.no_unanswered_cancel,
                                          fences.owned_rows_closed, fences.request_cas_current))):
        return result("unbound_request_db_or_cas_unknown")
    supplied = set(never_sent_witnesses or {}) | set(terminal_submission_witnesses or {})
    if (never_sent_witnesses is not None or terminal_submission_witnesses is not None) and (
            supplied != set(expected.account_ids)
            or set(never_sent_witnesses or {}) & set(terminal_submission_witnesses or {})):
        return result("unbound_never_sent_account_coverage_unknown")
    for name, account_id in expected.account_ids.items():
        never_sent = (never_sent_witnesses or {}).get(name)
        terminal = (terminal_submission_witnesses or {}).get(name)
        if terminal is not None:
            if (not isinstance(terminal, TerminalSubmissionWitness) or terminal.terminal is not True
                    or terminal.reason != "broker_terminal_durable_admission_closed"
                    or not isinstance(terminal.client_order_ids, tuple) or not terminal.client_order_ids
                    or not all(_identity(coid) for coid in terminal.client_order_ids)
                    or len(set(terminal.client_order_ids)) != len(terminal.client_order_ids)):
                return result("unbound_submission_terminal_unknown")
        if never_sent is not None or terminal is not None:
            if never_sent is not None and (not isinstance(never_sent, NeverSentWitness) or never_sent.never_sent is not True
                    or never_sent.reason != "never_sent_durable_admission_closed"
                    or not isinstance(never_sent.scope, NeverSentScope)):
                return result("unbound_never_sent_unknown")
            witness = never_sent if never_sent is not None else terminal
            scope = witness.scope
            if not isinstance(scope, NeverSentScope):
                return result("unbound_never_sent_scope_changed")
            if (scope.account_name, scope.account_id, scope.symbol, scope.generation,
                    scope.opportunity_started_at_ms, scope.request_id, scope.request_token,
                    scope.requested_at_ms, str(scope.coverage_process_id), scope.session_key) != (
                    name, account_id, expected.symbol, expected.generation,
                    expected.opportunity_started_at_ms, expected.request_id, expected.token,
                    expected.requested_at_ms, expected.coverage_process_ids.get(name), expected.session_key):
                return result("unbound_never_sent_scope_changed")
            if (type(witness.coverage_started_at_ms) is not int
                    or type(expected.opportunity_started_at_ms) is not int
                    or not _identity(expected.session_key)
                    or not 0 < witness.coverage_started_at_ms <= expected.opportunity_started_at_ms
                    or type(witness.observed_at_ms) is not int
                    or not expected.requested_at_ms <= witness.observed_at_ms <= now_ms
                    or now_ms - witness.observed_at_ms > freshness_ms):
                return result("unbound_never_sent_legacy_or_stale")
            # Durable admission proves only OMS never sent this generation. It
            # cannot certify absence of an operator BUY in the Webull account.
            if expected.account_providers[name] == "schwab":
                continue
        if expected.account_providers[name] == "schwab":
            witness = schwab_witness
            if (not isinstance(witness, SchwabLocalCancelWitness)
                    or witness.request != expected or not _identity(expected.session_key)
                    or (witness.account_name, witness.account_id, witness.session_key) != (
                        name, account_id, expected.session_key)
                    or witness.closure_kind not in {"no_dispatch", "exact_cancel_chain"}
                    or any(v is not True for v in (witness.publication_closed,
                        witness.no_unjournaled_buy, witness.no_live_buy,
                        witness.no_inflight_buy, witness.no_unanswered_cancel))):
                return result("unbound_schwab_causal_closure_unknown")
            if (type(witness.observed_at_ms) is not int
                    or not expected.requested_at_ms <= witness.observed_at_ms <= now_ms
                    or now_ms - witness.observed_at_ms > freshness_ms):
                return result("unbound_schwab_local_witness_stale")
            continue
        book = books.get(name)
        if (not isinstance(book, CompleteWorkingBook) or book.complete is not True
                or book.source != "broker" or book.coverage != "all_working"
                or (book.account_name, book.account_id) != (name, account_id)
                or not isinstance(book.orders, tuple)):
            return result("unbound_complete_book_unknown")
        if (type(book.started_at_ms) is not int or type(book.finished_at_ms) is not int
                or not expected.requested_at_ms <= book.started_at_ms <= book.finished_at_ms <= now_ms
                or now_ms - book.started_at_ms > freshness_ms):
            return result("unbound_book_stale_or_pre_request")
        seen = set()
        for order in book.orders:
            if (not isinstance(order, BookOrder) or order.side not in {"buy", "sell"}
                    or not all(_identity(v) for v in (order.symbol, order.status))
                    or not (_identity(order.client_order_id) or _identity(order.broker_order_id))
                    or any((order.client_order_id and order.client_order_id == coid)
                           or (order.broker_order_id and order.broker_order_id == broker_id)
                           for coid, broker_id in seen)
                    or order.status not in WORKING | TERMINAL | {"filled"}):
                return result("unbound_book_row_or_side_unknown")
            seen.add((order.client_order_id, order.broker_order_id))
            if order.symbol == expected.symbol and order.side == "buy" and order.status in WORKING:
                return result("unbound_symbol_working_buy")
    return result("unbound_symbol_terminal", True)
