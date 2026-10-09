"""Exact DAY cancel absence evidence; never a fill or submission-token receipt."""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.cancel_terminal_proof import BookOrder, CancelScope, CompleteWorkingBook, FRESHNESS_MS, TERMINAL, WORKING
from project_mai_tai.fanout_segment_store import current_session_anchor


@dataclass(frozen=True)
class BoundDayCancel:
    scope: CancelScope
    broker_order_id: str
    submitted_at_ms: int
    time_in_force: str
    side: str
    cancel_error_code: str
    cancel_http_status: str
    cancel_reason: str


@dataclass(frozen=True)
class FlatPositionRead:
    account_name: str
    account_id: str
    symbol: str
    started_at_ms: int
    finished_at_ms: int
    complete: bool
    flat: bool
    source: str = "broker"


@dataclass(frozen=True)
class DayCancelAbsence:
    bound: BoundDayCancel
    today: CompleteWorkingBook
    positions: FlatPositionRead


def eligible_bound_day_cancel(bound, receipt, now_ms):
    if (not isinstance(bound, BoundDayCancel) or bound.scope != receipt.scope
            or not isinstance(bound.broker_order_id, str) or not bound.broker_order_id.strip()
            or bound.side != "buy" or bound.time_in_force != "day"
            or bound.cancel_error_code != "ORDER_CAN_NOT_BE_CANCEL"
            or bound.cancel_http_status != "417" or receipt.status != "rejected"
            or receipt.refusal_origin != "broker_reject"
            or bound.cancel_reason != receipt.refusal_code
            or type(bound.submitted_at_ms) is not int
            or not 0 < bound.submitted_at_ms <= receipt.observed_at_ms <= now_ms):
        return False
    try:
        anchor = current_session_anchor(datetime.fromtimestamp(now_ms / 1000, UTC))
        if anchor.astimezone(ZoneInfo("America/New_York")).date() != datetime.fromtimestamp(
                now_ms / 1000, ZoneInfo("America/New_York")).date():
            return False  # list-today is not the previous 04:00 session before 04:00.
        return all(current_session_anchor(datetime.fromtimestamp(at / 1000, UTC)) == anchor
                   for at in (bound.submitted_at_ms, receipt.observed_at_ms))
    except (ValueError, OverflowError, OSError):
        return False


def day_cancel_absence_proven(witness, receipt, now_ms):
    if not isinstance(witness, DayCancelAbsence) or not eligible_bound_day_cancel(witness.bound, receipt, now_ms):
        return False
    book, positions, scope = witness.today, witness.positions, receipt.scope
    if (not isinstance(book, CompleteWorkingBook) or not isinstance(positions, FlatPositionRead)
            or book.complete is not True or book.coverage != "today" or book.source != "broker"
            or not isinstance(book.orders, tuple) or positions.complete is not True
            or positions.flat is not True or positions.source != "broker"
            or positions.symbol != scope.symbol):
        return False
    if any(not isinstance(row, BookOrder) or not isinstance(row.symbol, str) or not row.symbol
           or not isinstance(row.client_order_id, str) or not row.client_order_id
           or not isinstance(row.status, str) or row.status not in TERMINAL | WORKING | {"filled"}
           for row in book.orders):
        return False
    for read in (book, positions):
        if ((read.account_name, read.account_id) != (scope.account_name, scope.account_id)
                or type(read.started_at_ms) is not int or type(read.finished_at_ms) is not int
                or not receipt.observed_at_ms <= read.started_at_ms <= read.finished_at_ms <= now_ms
                or now_ms - read.started_at_ms > FRESHNESS_MS):
            return False
    # All rows were validated by the collector. Presence, including FILLED, is
    # contradictory, not permission to infer a cancelled/zero-fill target.
    return not any(row.client_order_id == scope.client_order_id
                   or row.broker_order_id == witness.bound.broker_order_id for row in book.orders)
