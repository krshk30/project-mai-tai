"""ID-only metadata repair from existing broker reports; never reads the broker."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session, object_session

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, SystemIncident

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentityEvidence:
    order_id: UUID
    account_name: str
    client_order_id: str
    symbol: str
    side: str
    broker_order_id: str
    source: str

    @classmethod
    def from_report(cls, order_id, account_name, client_order_id, symbol, side, report, source):
        if (not isinstance(report, ExecutionReport) or report.origin != "broker"
                or report.client_order_id != client_order_id
                or report.symbol != symbol or report.side != side
                or not isinstance(report.broker_order_id, str)
                or not report.broker_order_id.strip()
                or report.broker_order_id == client_order_id):
            return None
        return cls(order_id, account_name, client_order_id, symbol, side,
                   report.broker_order_id, source)


def record_conflict(session: Session, order: BrokerOrder, incoming: str, source: str) -> None:
    LOG.error("[WBNULLID1] result=identity_conflict order=%s coid=%s existing=%s incoming=%s source=%s",
              order.id, order.client_order_id, order.broker_order_id, incoming, source)
    title = f"WBNULLID1 broker identity conflict: {order.id}"
    if session.scalar(select(SystemIncident.id).where(
        SystemIncident.service_name == "oms-risk", SystemIncident.title == title,
        SystemIncident.status != "closed",
    )) is None:
        session.add(SystemIncident(
            service_name="oms-risk", severity="critical", title=title, status="open",
            payload={"source": "WBNULLID1", "order_id": str(order.id),
                     "client_order_id": order.client_order_id, "symbol": order.symbol,
                     "existing_id": order.broker_order_id, "incoming_id": incoming,
                     "report_source": source},
        ))
        session.flush()


def immutable_report_id(order: BrokerOrder, incoming: str | None, *, dispatch=None) -> str | None:
    """Normal submit/ack has no extra DB work. Only a conflicting existing ID needs review."""
    current = order.broker_order_id
    if not incoming or not current or incoming == current:
        return incoming or current
    session = object_session(order)
    if session is None:
        return current
    with session.no_autoflush:
        account = session.get(BrokerAccount, order.broker_account_id)
    if account is None or account.provider != "webull":
        return incoming
    if dispatch is not None:
        LOG.error("[WBNULLID1] result=identity_conflict order=%s coid=%s existing=%s incoming=%s",
                  order.id, order.client_order_id, current, incoming)
        pending = session.info.setdefault("wbnullid1_after_commit", [])
        pending.append((IdentityEvidence(order.id, account.name, order.client_order_id,
                                         order.symbol, order.side, incoming, "report_conflict"),
                        session.get_nested_transaction() or session.get_transaction()))
        if not session.info.get("wbnullid1_hooks"):
            session.info["wbnullid1_hooks"] = True

            def committed(current):
                if not current.in_nested_transaction():
                    items = current.info.pop("wbnullid1_after_commit", [])
                    try:
                        dispatch([item for item, _transaction in items])
                    except Exception:
                        LOG.exception("[WBNULLID1] result=after_commit_dispatch_failed count=%s",
                                      len(items))

            def rolled_back(current, previous_transaction):
                def survived(transaction):
                    # A rolled-back parent also discards evidence from committed children.
                    while transaction is not None:
                        if transaction is previous_transaction:
                            return False
                        transaction = transaction.parent
                    return True

                items = current.info.get("wbnullid1_after_commit", [])
                current.info["wbnullid1_after_commit"] = [
                    (item, transaction) for item, transaction in items if survived(transaction)
                ]

            event.listen(session, "after_commit", committed)
            event.listen(session, "after_soft_rollback", rolled_back)
        return current
    try:
        with session.begin_nested():
            record_conflict(session, order, incoming, "existing_transaction_report")
    except Exception:
        # A diagnostic must not roll back a legitimate fill or change the immutable ID.
        LOG.exception("[WBNULLID1] result=conflict_incident_write_failed order=%s", order.id)
    return current


def persist_identities(session: Session, evidence, *, owner_guard) -> list[str]:
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        session.execute(text("SET LOCAL statement_timeout = '2s'"))
    results = []
    for item in evidence:
        try:
            with session.begin_nested():
                order = session.scalar(select(BrokerOrder).where(
                    BrokerOrder.id == item.order_id,
                ).with_for_update())
                account = session.get(BrokerAccount, order.broker_account_id) if order else None
                if (order is None or account is None or account.provider != "webull"
                        or account.name != item.account_name
                        or order.client_order_id != item.client_order_id
                        or order.symbol != item.symbol or order.side != item.side):
                    results.append("binding_refused")
                    continue
                if order.broker_order_id:
                    if order.broker_order_id != item.broker_order_id:
                        record_conflict(session, order, item.broker_order_id, item.source)
                        results.append("conflict")
                    else:
                        results.append("already_saved")
                    continue
                if owner_guard(session, order, account.name):
                    results.append("owner_refused")
                    continue
                if session.scalar(select(BrokerOrder.id).where(
                    BrokerOrder.broker_order_id == item.broker_order_id,
                    BrokerOrder.id != order.id,
                )) is not None:
                    record_conflict(session, order, item.broker_order_id, "id_already_bound")
                    results.append("conflict")
                    continue
                # Core update bypasses ORM onupdate: unchanged status is not a fresh
                # broker-read receipt, and must not reset stuck-alert age/provenance.
                changed = session.execute(update(BrokerOrder).where(
                    BrokerOrder.id == order.id,
                    BrokerOrder.broker_order_id == order.broker_order_id,
                ).values(broker_order_id=item.broker_order_id, updated_at=order.updated_at))
                results.append("saved" if changed.rowcount == 1 else "superseded")
        except Exception:
            LOG.exception("[WBNULLID1] result=id_write_failed order=%s source=%s",
                          item.order_id, item.source)
            results.append("write_failed")
    return results


class IdentityWriter:
    """One bounded metadata FIFO and dedicated worker, never awaited by order submission."""

    def __init__(self, session_factory, owner_guard, *, capacity=128):
        self.session_factory = session_factory
        self.owner_guard = owner_guard
        self.queue = asyncio.Queue(maxsize=capacity)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wbnullid1")
        self.task = None
        self.closed = False

    def offer(self, evidence):
        for item in evidence:
            if self.closed:
                LOG.error("[WBNULLID1] result=writer_closed order=%s", item.order_id)
                continue
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                LOG.error("[WBNULLID1] result=queue_full order=%s id_not_saved=1", item.order_id)
        if not self.closed and (self.task is None or self.task.done()) and not self.queue.empty():
            self.task = asyncio.create_task(self._run())

    def _write(self, item):
        with self.session_factory() as session:
            result = persist_identities(session, [item], owner_guard=self.owner_guard)
            session.commit()
            return result

    async def _run(self):
        loop = asyncio.get_running_loop()
        while not self.queue.empty():
            item = self.queue.get_nowait()
            try:
                await loop.run_in_executor(self.pool, self._write, item)
            except Exception:
                LOG.exception("[WBNULLID1] result=writer_failed order=%s", item.order_id)
            finally:
                self.queue.task_done()

    async def drain(self):
        await self.queue.join()
        if self.task is not None:
            await asyncio.shield(self.task)

    async def close(self):
        self.closed = True
        try:
            await asyncio.wait_for(self.drain(), timeout=30)
        except TimeoutError:
            LOG.error("[WBNULLID1] result=shutdown_undrained count=%s", self.queue.qsize() + 1)
        finally:
            self.pool.shutdown(wait=False)
