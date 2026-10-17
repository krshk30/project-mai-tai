"""D2: bounded immutable capture; one off-loop, coalesced diagnostic unit.

No broker call, trading mutation, savepoint, row lock or session crosses await.
Overflow revokes scalar evidence instead of silently retaining dropped negatives.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot
from project_mai_tai.reconciliation.order_read_evidence import (
    SNAPSHOT_TYPE, invalidate_order_read, record_order_read,
)

MAX_CAPTURED_ORDERS = 64
MAX_CACHE_EVIDENCE_BYTES = 2048


@dataclass(frozen=True)
class OrderRead:
    order_id: UUID
    account_id: UUID
    account_name: str
    provider: str
    coid: str
    native_id: str | None
    symbol: str
    side: str
    quantity: Decimal
    started: datetime
    completed: datetime
    native_account: str | None
    report: tuple | None


class OrderReadBatch:
    def __init__(self):
        self.reads = {}
        self.overflow = False
        self.failed = False
        # At most the actual account scope of this original pass, not one
        # identity per dropped order. Other accounts must keep their receipts.
        self.revoked_accounts = set()

    def revoke_account(self, account_id):
        if isinstance(account_id, UUID):
            self.revoked_accounts.add(account_id)

    def capture(self, *, order, account, report, started, completed, native_account=None):
        if not account.name.startswith("live:") or account.provider not in {"schwab", "webull"}:
            return
        if order.id not in self.reads and len(self.reads) >= MAX_CAPTURED_ORDERS:
            self.overflow = True
            self.revoke_account(account.id)
            return
        values = None
        if report is not None:
            try:
                cache = report.metadata.get("_webull_list_evidence")
                if cache is not None and (not isinstance(cache, str) or len(cache.encode()) > MAX_CACHE_EVIDENCE_BYTES):
                    raise ValueError("unbounded diagnostic metadata")
                values = (report.event_type, report.client_order_id, report.broker_order_id,
                          report.symbol, report.side, report.quantity, report.filled_quantity,
                          report.origin, cache)
                if (any(not isinstance(value, str) for value in (
                        report.event_type, report.client_order_id, report.symbol, report.side, report.origin))
                        or report.broker_order_id is not None and not isinstance(report.broker_order_id, str)
                        or not isinstance(report.quantity, Decimal) or not isinstance(report.filled_quantity, Decimal)):
                    raise ValueError("diagnostic report is not an immutable scalar shape")
            except Exception:
                values = None  # Diagnostic uncertainty cannot affect the original broker result.
        self.reads[order.id] = OrderRead(order.id, account.id, account.name, account.provider,
            order.client_order_id, order.broker_order_id, order.symbol, order.side,
            order.quantity, started, completed,
            native_account if isinstance(native_account, str) else None, values)


def persist_order_reads(session, batch, *, cadence_seconds):
    """Call ONLY in service._run_db after original trading session has closed."""
    revoked = {str(identity) for identity in batch.revoked_accounts}
    affected = {str(read.account_id) for read in batch.reads.values()} | revoked
    if not affected:
        return
    rows = session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        DashboardSnapshot.payload["account_id"].as_string().in_(affected))).all()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("account_id") in revoked:
            row.payload = {**payload, "orders": {}}
    if not batch.reads:
        return
    orders = {row.id: row for row in session.scalars(select(BrokerOrder).where(
        BrokerOrder.id.in_(tuple(batch.reads)))).all()}
    accounts = {row.id: row for row in session.scalars(select(BrokerAccount).where(
        BrokerAccount.id.in_({read.account_id for read in batch.reads.values()}))).all()}
    # Preload once. JSON assignments coalesce into one final flush per account,
    # not SELECT/savepoint/double-read/UPDATE per observed order.
    with session.no_autoflush:
        for read in batch.reads.values():
            order, account = orders.get(read.order_id), accounts.get(read.account_id)
            if (batch.failed or read.account_id in batch.revoked_accounts
                    or order is None or account is None or read.report is None
                    or account.name != read.account_name or account.provider != read.provider
                    or order.broker_account_id != account.id or order.client_order_id != read.coid
                    or order.broker_order_id != read.native_id or order.symbol != read.symbol
                    or order.side != read.side
                    or not isinstance(order.quantity, Decimal) or not order.quantity.is_finite()
                    or not isinstance(read.quantity, Decimal) or not read.quantity.is_finite()
                    or order.quantity <= 0 or order.quantity != read.quantity
                    or order.status not in {"pending", "submitted", "accepted", "partially_filled"}):
                invalidate_order_read(session, order_id=read.order_id, account_id=read.account_id,
                                      receipt_rows=rows)
                continue
            kind, coid, native, symbol, side, quantity, filled, origin, cache = read.report
            report = SimpleNamespace(event_type=kind, client_order_id=coid, broker_order_id=native,
                symbol=symbol, side=side, quantity=quantity, filled_quantity=filled, origin=origin,
                metadata={"_webull_list_evidence": cache} if cache is not None else {})
            record_order_read(session, order=order, account=account, report=report,
                wire_started_at=read.started, wire_completed_at=read.completed,
                cadence_seconds=cadence_seconds, broker_account_native_id=read.native_account,
                receipt_rows=rows)
