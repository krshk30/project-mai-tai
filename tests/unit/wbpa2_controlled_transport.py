"""UNRUN private-test boundary, never native observations or entry permission.

Only typed explicit submission replies are configured. Any provider read or
unconfigured operation refuses. The real service/risk/slot gates are untouched.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from time import monotonic
import threading

from project_mai_tai.broker_adapters.protocols import ExecutionReport


@dataclass(frozen=True)
class ControlledSubmitReply:
    status: str
    reason: str
    error_code: str = ""
    native_id: str | None = None


class ControlledProvider:
    """Async, recording/refusing boundary; no SDK thread/off-loop claim.

    Market task identity is measured by the test around the ACTUAL callback.
    Child scheduler tasks are distinct, but reads on them also refuse. Only the
    explicit serial task may submit. No generic __getattr__ forwards to a real
    broker, and no empty list is returned as no-working proof.
    """
    def __init__(self, *, account, symbol, slot, segment, clock, replies=()):
        self.account, self.symbol, self.slot, self.segment = account, symbol, slot, str(segment)
        self.clock = clock
        self.replies = deque(replies)
        self.entries = []
        self.requests = []
        self.market_tasks = set()
        self.serial_task = None
        self.live_clients = set()

    def _record(self, operation):
        task = asyncio.current_task()
        row = dict(operation=operation, task=task, thread_id=threading.get_ident(),
                   entered_at=monotonic(), market_task=task in self.market_tasks)
        self.entries.append(row)
        assert not row["market_task"], "provider operation entered on market callback task"
        return row

    async def submit_order(self, request):
        row = self._record("submit")
        assert asyncio.current_task() is self.serial_task, "submit escaped reviewed serial actor"
        assert (request.broker_account_name, request.symbol, request.side, request.intent_type) == (
            self.account, self.symbol, "buy", "open")
        metadata = request.metadata
        assert metadata["fanout_slot_id"] == self.slot
        assert str(metadata["fanout_segment_id"]) == self.segment
        assert metadata["atr_country_broker"] == "webull"
        assert metadata["atr_country_account"] == self.account
        assert metadata["atr_country_schema"] == "1"
        assert not self.live_clients, "CONTROLLED book already has one live BUY for this slot"
        prewire = getattr(request, "prewire_check", None)
        if prewire is not None:
            assert callable(prewire)
            prewire()  # Preserve the real adapter contract, never replace permission.
            row["actual_request_prewire_check_returned"] = True
        assert self.replies, "unconfigured provider submission is refused"
        reply = self.replies.popleft()
        assert isinstance(reply, ControlledSubmitReply)
        assert reply.status in {"accepted", "rejected"}
        self.requests.append(request)
        await asyncio.sleep(0)  # Explicit controlled async I/O scheduling boundary.
        if reply.status == "accepted":
            assert reply.native_id and reply.native_id.startswith("CONTROLLED-")
            self.live_clients.add(request.client_order_id)
        assert isinstance(self.clock(), datetime)
        row["returned_at"] = monotonic()
        metadata = dict(metadata)
        if reply.error_code:
            metadata.update(webull_error_code=reply.error_code, webull_http_status="417")
        return [ExecutionReport(reply.status, request.client_order_id,
            broker_order_id=reply.native_id, symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            filled_quantity=Decimal(0), reported_at=self.clock(), origin="broker",
            reason=reply.reason, metadata=metadata)]

    async def list_account_positions(self, account):
        self._record("positions")
        raise AssertionError("UNCONFIGURED provider positions; no flat/empty permission stub")

    async def list_working_orders(self, account):
        self._record("working_orders")
        raise AssertionError("UNCONFIGURED provider working orders; existing real proof required")

    async def fetch_order_update(self, request):
        self._record("order_detail")
        raise AssertionError("UNCONFIGURED provider detail; no guessed terminal reply")

    async def fetch_oco_exit_fill(self, *args, **kwargs):
        self._record("oco_detail")
        raise AssertionError("UNCONFIGURED provider native OCO read")


class ControlledRedis:
    """Explicit private publish acknowledgement, not native order evidence.

    No stream worker runs. Tests drain exact captured messages through the real
    service serial handler. Unexpected reads refuse instead of supplying a
    blanket empty stream/history or fabricated startup permission.
    """
    def __init__(self, *, values=None):
        self.rows = []
        self.reads = []
        self.values = dict(values or {})

    async def xadd(self, stream, fields, **kwargs):
        await asyncio.sleep(0)
        self.rows.append((stream, dict(fields), dict(kwargs)))
        return f"CONTROLLED-{len(self.rows)}-0"

    async def get(self, key):
        self.reads.append(key)
        assert key in self.values, "UNCONFIGURED Redis read; bind exact fixture cache input first"
        return self.values[key]

    async def set(self, key, value, ex=None):
        # Record the real service's controlled session veto, including TTL.
        self.values[key] = value
        self.reads.append(("set", key, value, ex))
        return True
