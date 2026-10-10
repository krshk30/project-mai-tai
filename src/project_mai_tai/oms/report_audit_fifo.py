"""Bounded, in-memory post-commit audit queue; deliberately not a durable outbox."""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

PRIOR = "ordinary_report_audit_prior_payload"
LIMIT = 256


def ordinary_metadata(*sources):
    for source in sources:
        if not isinstance(source, dict):
            return False
        # Every durable root cancel carries this writer stamp; it alone is not proof scope.
        if any(not isinstance(key, str) or (key != "buy_submission_process_id" and key.startswith((
                "fanout_", "nfq", "clearwait_", "buy_submission_", "rpg_", "mirrorhold_"))
               ) for key in source):
            return False
    return True


@dataclass(frozen=True)
class AuditItem:
    id: UUID
    order_id: UUID
    broker_account_id: UUID
    symbol: str
    client_order_id: str
    event_type: str
    reported_at: datetime
    origin: str
    reason: str
    payload_json: str
    order_metadata_json: str

    @classmethod
    def capture(cls, order, report, payload):
        return cls(uuid4(), order.id, order.broker_account_id, order.symbol,
            report.client_order_id, report.event_type, report.reported_at,
            getattr(report, "origin", "unknown") or "unknown", report.reason or "",
            json.dumps(payload, allow_nan=False), json.dumps(order.payload, allow_nan=False))


async def finish_task(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ReportAuditFIFO:
    def __init__(self, factory, append, complete, logger, *, limit=LIMIT):
        self.factory, self.append, self.complete, self.logger = factory, append, complete, logger
        self.limit = limit
        self.pending = deque()
        self.active = None
        self.task = None
        self.enqueued = self.written = self.failed = 0
        self.logger.warning("[OMS-AUDIT-FIFO-START] previous_process_pending=UNKNOWN "
            "inventory=in_memory crash_commit_to_audit_loss_possible=1")

    async def transfer(self, items):
        await finish_task(asyncio.create_task(self._transfer(items)))

    async def _transfer(self, items):
        for item in items:
            while len(self.pending) + int(self.active is not None) >= self.limit:
                self.logger.warning("[OMS-AUDIT-FIFO-FULL] pending=%d backpressure=drain no_drop=1",
                    len(self.pending) + int(self.active is not None))
                await self.drain()
            self.pending.append(item)
            self.enqueued += 1
            self.logger.info("[OMS-AUDIT-FIFO-ENQUEUE] enqueue_id=%s order_id=%s coid=%s "
                "status=%s parent_committed=1 audit_committed=0", item.id, item.order_id,
                item.client_order_id, item.event_type)
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._run())

    def _write(self, item):
        try:
            with self.factory() as session:
                order = SimpleNamespace(id=item.order_id, symbol=item.symbol,
                    broker_account_id=item.broker_account_id,
                    payload=json.loads(item.order_metadata_json))
                report = SimpleNamespace(event_type=item.event_type, reported_at=item.reported_at,
                    origin=item.origin, reason=item.reason)
                with session.begin_nested():
                    result = self.append(session, order=order, report=report,
                        payload=json.loads(item.payload_json), use_core=True)
                # The generated audit UUID identifies the real inserted row in completion logs.
                audit_id = result.id
                session.commit()
            return None, audit_id
        except Exception as error:
            return error, None

    async def _run(self):
        cancelled = False
        while self.pending:
            item = self.active = self.pending.popleft()
            outcome = {}
            def write():
                outcome["result"] = self._write(item)
            try:
                await finish_task(asyncio.create_task(asyncio.to_thread(write)))
            except asyncio.CancelledError:
                cancelled = True
            error, audit_id = outcome["result"]
            self.complete(error, order=item, report=item)
            self.written += int(error is None)
            self.failed += int(error is not None)
            self.logger.info("[OMS-AUDIT-FIFO-COMPLETE] audit_id=%s enqueue_id=%s order_id=%s "
                "coid=%s committed=%d enqueued=%d written=%d failed=%d pending=%d",
                audit_id, item.id, item.order_id, item.client_order_id, int(error is None),
                self.enqueued, self.written, self.failed, len(self.pending))
            self.active = None
        if cancelled:
            raise asyncio.CancelledError

    async def drain(self):
        task = self.task
        if task is not None:
            await finish_task(task)
