from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    RiskCheck,
    SchwabIneligibleToday,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import (
    QuoteTickEvent,
    QuoteTickPayload,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    TradeTickPayload,
)
from project_mai_tai.oms.service import _EXIT_FETCH_FAILED, OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import configured_broker_account_registrations, strategy_registration_map
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.time_utils import session_day_eastern_str


@pytest.fixture(autouse=True)
def _market_always_fillable(monkeypatch):
    """These tests exercise the order-refresh / abandon and exit-ladder logic, not
    the 7 AM–8 PM ET fillable-session gate. Hold the market open so they are
    deterministic regardless of the wall-clock run time (else a CI run outside
    7 AM–8 PM ET, or on a weekend, would trip the MARKET_CLOSED abandon). The gate
    itself is covered directly in test_oms_fillable_window.py."""
    monkeypatch.setattr(
        "project_mai_tai.oms.service.OmsRiskService._market_is_fillable",
        lambda self, now=None: True,
    )


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []
        self.values: dict[str, str] = {}

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        del ex
        self.values[key] = value
        return True

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


class FakeCancelBrokerAdapter:
    def __init__(self, *, cancel_event_type: str = "cancelled") -> None:
        self.cancel_event_type = cancel_event_type

    async def submit_order(self, request):
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-123",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        return [
            ExecutionReport(
                event_type=self.cancel_event_type,  # type: ignore[arg-type]
                client_order_id=request.client_order_id,
                broker_order_id="ord-123",
                symbol=request.symbol,
                side=request.side,
                intent_type="cancel",
                quantity=request.quantity,
                reason=request.reason or "USER_CANCEL",
                metadata=dict(request.metadata),
            )
        ]

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []

    async def fetch_order_update(self, request):
        del request
        return None


class FakeOrderSyncBrokerAdapter:
    def __init__(self, report: ExecutionReport | None = None) -> None:
        self.report = report

    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-123",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        assert request.metadata["broker_order_id"] == "ord-123"
        return self.report

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeSequentialOrderSyncBrokerAdapter:
    def __init__(self, reports: list[ExecutionReport | None]) -> None:
        self.reports = reports
        self.fetch_calls = 0

    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-123",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        report = self.reports[min(self.fetch_calls, len(self.reports) - 1)]
        self.fetch_calls += 1
        return report

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakePendingExitBrokerAdapter:
    async def submit_order(self, request):
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=Decimal("2.55"),
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-exit",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=Decimal("10"),
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeScaleThenHardStopBrokerAdapter:
    def __init__(self) -> None:
        self.submit_requests = []

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=Decimal("2.55"),
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-scale")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        if request.intent_type == "scale":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-scale",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-hard-stop",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=Decimal("10"),
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeStopRejectedFallbackBrokerAdapter:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, str, Decimal]] = []
        self.quantity = Decimal("10")

    async def submit_order(self, request):
        self.submitted.append((request.intent_type, request.side, request.symbol, request.quantity))
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="child stop rejected at/below stop",
                    metadata=dict(request.metadata),
                )
            ]
        self.quantity = Decimal("0")
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-fallback",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id=request.client_order_id,
                broker_order_id="ord-fallback",
                broker_fill_id="fill-fallback",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=Decimal("2.40"),
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.quantity,
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeAcceptedOnlyBrokerAdapter:
    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-{request.client_order_id}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeStopGuardCloseRejectFallbackBrokerAdapter:
    """Open fills; first stop_guard close gets stop-rejected; market fallback fills."""

    def __init__(self) -> None:
        self.submit_requests: list = []
        self.position_qty = Decimal("0")

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            self.position_qty = request.quantity
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=Decimal("2.55"),
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        is_stop_guard = str(request.metadata.get("stop_guard", "")).strip().lower() == "true"
        is_fallback = str(request.metadata.get("stop_reject_fallback", "")).strip().lower() == "true"
        if is_stop_guard and not is_fallback:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=None,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="child stop rejected at/below stop",
                    metadata=dict(request.metadata),
                )
            ]
        self.position_qty = Decimal("0")
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-fallback",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id=request.client_order_id,
                broker_order_id="ord-fallback",
                broker_fill_id="fill-fallback",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=Decimal("2.40"),
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.position_qty <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.position_qty,
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeHardStopThenHardStopBrokerAdapter:
    """Open fills; HARD_STOP closes stay open (accepted) so a second HARD_STOP must preempt the first."""

    def __init__(self) -> None:
        self.submit_requests: list = []
        self._hard_stop_count = 0

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=Decimal("2.55"),
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-cancelled")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        self._hard_stop_count += 1
        broker_id = f"ord-hard-stop-{self._hard_stop_count}"
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=broker_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=Decimal("10"),
                average_price=Decimal("2.55"),
                market_value=None,
                as_of=None,
            )
        ]


class FakeWorkingOrderRefreshBrokerAdapter:
    def __init__(
        self,
        *,
        fetch_event_type: str = "accepted",
        filled_quantity: Decimal = Decimal("0"),
        fill_price: Decimal | None = None,
        ask_price: float = 1.23,
        bid_price: float = 1.21,
    ) -> None:
        self.fetch_event_type = fetch_event_type
        self.filled_quantity = filled_quantity
        self.fill_price = fill_price
        self.ask_price = ask_price
        self.bid_price = bid_price
        self.submit_requests = []

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-123")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]

        broker_order_id = f"ord-{len([item for item in self.submit_requests if item.intent_type != 'cancel'])}"
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type=self.fetch_event_type,  # type: ignore[arg-type]
            client_order_id=request.client_order_id,
            broker_order_id=str(request.metadata.get("broker_order_id", "ord-123")),
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            filled_quantity=self.filled_quantity,
            fill_price=self.fill_price,
            reason=request.reason,
            metadata=dict(request.metadata),
        )

    async def fetch_quotes(self, symbols):
        return {
            str(symbol).upper(): {
                "ask_price": self.ask_price,
                "bid_price": self.bid_price,
                "last_price": (self.ask_price + self.bid_price) / 2,
            }
            for symbol in symbols
        }

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeTickDrivenHardStopBrokerAdapter:
    def __init__(self, *, open_fill_price: Decimal = Decimal("4.00")) -> None:
        self.open_fill_price = open_fill_price
        self.submit_requests = []
        self.position_quantity = Decimal("0")

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            self.position_quantity += request.quantity
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=self.open_fill_price,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="ord-close",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.position_quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.position_quantity,
                average_price=self.open_fill_price,
                market_value=None,
                as_of=None,
            )
        ]


class FakeNativeStopGuardBrokerAdapter:
    def __init__(self, *, open_fill_price: Decimal = Decimal("4.00")) -> None:
        self.open_fill_price = open_fill_price
        self.submit_requests = []
        self.position_quantity = Decimal("0")

    async def submit_order(self, request):
        self.submit_requests.append(request)
        if request.intent_type == "open":
            self.position_quantity += request.quantity
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
                ExecutionReport(
                    event_type="filled",
                    client_order_id=request.client_order_id,
                    broker_order_id="ord-open",
                    broker_fill_id="fill-open",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    filled_quantity=request.quantity,
                    fill_price=self.open_fill_price,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                ),
            ]
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
                    broker_order_id=str(request.metadata.get("broker_order_id", "ord-stop")),
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        if str(request.metadata.get("native_stop_guard", "")).lower() == "true":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id=f"ord-stop-{len(self.submit_requests)}",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]
        self.position_quantity = max(Decimal("0"), self.position_quantity - request.quantity)
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-sell-{len(self.submit_requests)}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-sell-{len(self.submit_requests)}",
                broker_fill_id=f"fill-sell-{len(self.submit_requests)}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=request.quantity,
                fill_price=Decimal("4.20"),
                reason=request.reason,
                metadata=dict(request.metadata),
            ),
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        if self.position_quantity <= 0:
            return []
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol="UGRO",
                quantity=self.position_quantity,
                average_price=self.open_fill_price,
                market_value=None,
                as_of=None,
            )
        ]


async def _noop_sync_broker_state(*, account_names=None):
    del account_names
    return {"accounts": 0, "positions": 0, "orders": 0, "terminal_orders": 0}


class FakeRejectNotTradableBrokerAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                broker_order_id=None,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason='asset "JCSE" is not tradable',
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class FakeRejectSchwabIneligibleBrokerAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                broker_order_id=None,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason="Opening transactions for this security must be placed with a broker. Contact us",
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_runtime_registry_can_route_macd_30s_to_schwab_only() -> None:
    settings = Settings(
        oms_adapter="simulated",
        strategy_macd_30s_broker_provider="schwab",
        strategy_macd_1m_enabled=True,
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["macd_30s"].execution_mode == "live"
    assert registrations["macd_30s"].metadata["provider"] == "schwab"
    assert registrations["macd_1m"].execution_mode == "shadow"
    assert broker_accounts[settings.strategy_macd_30s_account_name].provider == "schwab"
    assert broker_accounts[settings.strategy_macd_1m_account_name].provider == "alpaca"


def test_oms_service_builds_routing_adapter_for_mixed_brokers() -> None:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
            strategy_macd_1m_enabled=True,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
    )

    assert isinstance(service.broker_adapter, RoutingBrokerAdapter)


def test_runtime_registry_can_route_tos_to_schwab_only() -> None:
    settings = Settings(
        oms_adapter="simulated",
        strategy_macd_1m_enabled=True,
        strategy_tos_enabled=True,
        strategy_tos_broker_provider="schwab",
    )

    registrations = strategy_registration_map(settings)
    broker_accounts = {item.name: item for item in configured_broker_account_registrations(settings)}

    assert registrations["tos"].execution_mode == "live"
    assert registrations["tos"].metadata["provider"] == "schwab"
    assert registrations["macd_1m"].execution_mode == "shadow"
    assert broker_accounts[settings.strategy_tos_account_name].provider == "schwab"
    assert broker_accounts[settings.strategy_macd_1m_account_name].provider == "alpaca"


@pytest.mark.asyncio
async def test_oms_service_persists_filled_intent_and_positions() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "path": "P1_MACD_CROSS",
                    "reference_price": "2.55",
                },
            ),
        )
    )

    assert [event.payload.status for event in events] == ["accepted", "filled"]
    assert [stream for stream, _payload in redis.entries] == ["test:order-events", "test:order-events"]

    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent))
        stored_order = session.scalar(select(BrokerOrder))
        stored_fill = session.scalar(select(Fill))
        virtual_position = session.scalar(select(VirtualPosition))
        account_position = session.scalar(select(AccountPosition))

        assert stored_intent is not None
        assert stored_intent.status == "filled"
        assert stored_order is not None
        assert stored_order.status == "filled"
        assert stored_fill is not None
        assert stored_fill.price == Decimal("2.55")
        assert virtual_position is not None
        assert virtual_position.quantity == Decimal("10")
        assert virtual_position.average_price == Decimal("2.55")
        assert account_position is not None
        assert account_position.quantity == Decimal("10")


@pytest.mark.asyncio
async def test_oms_service_rejects_non_positive_quantity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "rejected"
    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent))
        assert stored_intent is not None
        assert stored_intent.status == "rejected"


@pytest.mark.asyncio
async def test_oms_service_rejects_intents_for_protected_symbols() -> None:
    """Protected-symbol gate: every intent type (open/close/scale/cancel) for
    a symbol listed in MAI_TAI_PROTECTED_SYMBOLS must be rejected at the OMS
    risk evaluator, regardless of strategy code or broker account."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            protected_symbols="CYN, XYZ",
        ),
        redis_client=redis,
        session_factory=session_factory,
    )

    cases = [
        ("open", "buy", Decimal("10")),
        ("close", "sell", Decimal("10")),
        ("scale", "buy", Decimal("5")),
        ("cancel", "sell", Decimal("0")),
    ]
    for intent_type, side, quantity in cases:
        events = await service.process_trade_intent(
            TradeIntentEvent(
                source_service="strategy-engine",
                payload=TradeIntentPayload(
                    strategy_code="macd_30s",
                    broker_account_name="paper:macd_30s",
                    symbol="cyn",
                    side=side,
                    quantity=quantity,
                    intent_type=intent_type,
                    reason="ENTRY_P1_MACD_CROSS",
                    metadata={},
                ),
            )
        )
        assert len(events) == 1, intent_type
        assert events[0].payload.status == "rejected", intent_type

    with session_factory() as session:
        risk_checks = session.scalars(
            select(RiskCheck).order_by(RiskCheck.created_at.asc())
        ).all()
        assert len(risk_checks) >= len(cases)
        protected_checks = [r for r in risk_checks if r.reason == "protected_symbol:CYN"]
        assert len(protected_checks) == len(cases), (
            f"every CYN intent must record a protected_symbol risk_check: "
            f"got {[r.reason for r in risk_checks]}"
        )
        for check in protected_checks:
            assert check.outcome == "reject"


@pytest.mark.asyncio
async def test_oms_service_blocks_not_tradable_symbol_for_rest_of_session() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeRejectNotTradableBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    first = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="JCSE",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )
    second = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="JCSE",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )

    assert len(adapter.requests) == 1
    assert first[0].payload.status == "rejected"
    assert 'tradable' in (first[0].payload.reason or '')
    assert second[0].payload.status == "rejected"
    assert second[0].payload.reason == "broker_symbol_not_tradable_for_session"

    with session_factory() as session:
        intents = session.scalars(select(TradeIntent).order_by(TradeIntent.created_at.asc())).all()
        assert len(intents) == 2
        assert all(intent.status == "rejected" for intent in intents)


@pytest.mark.asyncio
async def test_oms_service_caches_schwab_ineligible_symbol_for_session_day() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeRejectSchwabIneligibleBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    first = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="AEHL",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )
    second = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="AEHL",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={},
            ),
        )
    )

    assert len(adapter.requests) == 1
    assert first[0].payload.status == "rejected"
    assert "placed with a broker" in (first[0].payload.reason or "")
    assert second[0].payload.status == "rejected"
    assert second[0].payload.reason == "schwab_ineligible_cached"

    with session_factory() as session:
        entries = session.scalars(select(SchwabIneligibleToday)).all()
        assert len(entries) == 1
        assert entries[0].symbol == "AEHL"
        assert entries[0].session_date == session_day_eastern_str()
        assert entries[0].hit_count == 1


@pytest.mark.asyncio
async def test_oms_service_syncs_account_positions_from_broker_truth() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        account_position.quantity = Decimal("3")
        account_position.average_price = Decimal("1.11")
        session.commit()

    sync_summary = await service.sync_broker_positions(account_names=["paper:macd_30s"])
    assert sync_summary == {"accounts": 1, "positions": 1}

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        assert account_position.quantity == Decimal("10")
        assert account_position.average_price == Decimal("2.55")


@pytest.mark.asyncio
async def test_oms_service_sync_clears_virtual_positions_without_broker_backing() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    with session_factory() as session:
        strategy = service.store.ensure_strategy(session, "macd_30s")
        account = service.store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="alpaca",
            environment="paper",
        )
        session.add(
            VirtualPosition(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                symbol="ASTC",
                quantity=Decimal("10"),
                average_price=Decimal("5.36"),
                realized_pnl=Decimal("0"),
            )
        )
        session.commit()

    sync_summary = await service.sync_broker_positions(account_names=["paper:macd_30s"])
    assert sync_summary == {"accounts": 1, "positions": 0}

    with session_factory() as session:
        virtual_position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "ASTC"))
        assert virtual_position is not None
        assert virtual_position.quantity == Decimal("0")
        assert virtual_position.average_price == Decimal("0")


@pytest.mark.asyncio
async def test_oms_service_cancels_open_order_using_existing_order_identity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeCancelBrokerAdapter(),
    )

    open_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )
    cancel_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="cancel",
                reason="USER_CANCEL",
                metadata={},
            ),
        )
    )

    assert [event.payload.status for event in open_events] == ["accepted"]
    assert [event.payload.status for event in cancel_events] == ["cancelled"]
    assert cancel_events[0].payload.client_order_id == open_events[0].payload.client_order_id
    assert cancel_events[0].payload.quantity == Decimal("10")

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        intents = session.scalars(select(TradeIntent).order_by(TradeIntent.created_at)).all()

        assert stored_order is not None
        assert stored_order.status == "cancelled"
        assert [intent.intent_type for intent in intents] == ["open", "cancel"]
        assert intents[0].status == "submitted"
        assert intents[1].status == "cancelled"


@pytest.mark.asyncio
async def test_oms_service_keeps_open_order_status_when_cancel_is_rejected() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeCancelBrokerAdapter(cancel_event_type="rejected"),
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )
    cancel_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("0"),
                intent_type="cancel",
                reason="USER_CANCEL",
                metadata={},
            ),
        )
    )

    assert [event.payload.status for event in cancel_events] == ["rejected"]

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        cancel_intent = session.scalars(
            select(TradeIntent).where(TradeIntent.intent_type == "cancel")
        ).one()

        assert stored_order is not None
        assert stored_order.status == "accepted"
        assert cancel_intent.status == "rejected"


@pytest.mark.asyncio
async def test_oms_service_syncs_open_order_status_from_broker() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeOrderSyncBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="BFRG",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P3_MACD_SURGE",
                metadata={"reference_price": "1.15"},
            ),
        )
    )

    adapter.report = ExecutionReport(
        event_type="cancelled",
        client_order_id="macd_1m-BFRG-open-abc123",
        broker_order_id="ord-123",
        symbol="BFRG",
        side="buy",
        intent_type="open",
        quantity=Decimal("100"),
        reason="ENTRY_P3_MACD_SURGE",
        metadata={},
    )

    summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder))
        stored_intent = session.scalar(select(TradeIntent))

        assert stored_order is not None
        assert stored_order.status == "cancelled"
        assert stored_intent is not None
        assert stored_intent.status == "cancelled"


@pytest.mark.asyncio
async def test_oms_service_sync_publishes_terminal_order_event_for_strategy_runtime() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeSequentialOrderSyncBrokerAdapter(
        reports=[
            ExecutionReport(
                event_type="partially_filled",
                client_order_id="macd_1m-MASK-open-abc123",
                broker_order_id="ord-123",
                broker_fill_id="fill-92",
                symbol="MASK",
                side="buy",
                intent_type="open",
                quantity=Decimal("100"),
                filled_quantity=Decimal("92"),
                fill_price=Decimal("2.43"),
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
            ExecutionReport(
                event_type="filled",
                client_order_id="macd_1m-MASK-open-abc123",
                broker_order_id="ord-123",
                broker_fill_id="fill-100",
                symbol="MASK",
                side="buy",
                intent_type="open",
                quantity=Decimal("100"),
                filled_quantity=Decimal("100"),
                fill_price=Decimal("2.43"),
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        ]
    )
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="MASK",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        )
    )

    first_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    second_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])

    assert first_summary == {"orders": 1, "terminal_orders": 0}
    assert second_summary == {"orders": 1, "terminal_orders": 1}
    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "partially_filled", "filled"]


@pytest.mark.asyncio
async def test_oms_service_sync_skips_duplicate_partial_without_new_fill_progress() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeOrderSyncBrokerAdapter(
        ExecutionReport(
            event_type="partially_filled",
            client_order_id="macd_1m-MASK-open-abc123",
            broker_order_id="ord-123",
            broker_fill_id="fill-92",
            symbol="MASK",
            side="buy",
            intent_type="open",
            quantity=Decimal("100"),
            filled_quantity=Decimal("92"),
            fill_price=Decimal("2.43"),
            reason="ENTRY_P2_VWAP_BREAKOUT",
            metadata={},
        )
    )
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="MASK",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P2_VWAP_BREAKOUT",
                metadata={},
            ),
        )
    )

    first_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    second_summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])

    assert first_summary == {"orders": 1, "terminal_orders": 0}
    assert second_summary == {"orders": 0, "terminal_orders": 0}
    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "partially_filled"]


@pytest.mark.asyncio
async def test_oms_service_sync_terminalizes_intent_when_all_linked_orders_are_cancelled() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeOrderSyncBrokerAdapter(None),
    )

    with session_factory() as session:
        strategy = service.store.ensure_strategy(session, "macd_30s", name="MACD 30s")
        account = service.store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="simulated",
            environment="paper",
        )
        intent = service.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=account,
            event=TradeIntentEvent(
                source_service="oms-risk",
                payload=TradeIntentPayload(
                    strategy_code="macd_30s",
                    broker_account_name="paper:macd_30s",
                    symbol="LABT",
                    side="sell",
                    quantity=Decimal("10"),
                    intent_type="close",
                    reason=service.NATIVE_STOP_GUARD_REASON,
                    metadata={"native_stop_guard": "true"},
                ),
            ),
        )
        intent.status = "submitted"
        for idx in range(2):
            session.add(
                BrokerOrder(
                    intent_id=intent.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    client_order_id=f"macd_30s-LABT-close-stop-{idx}",
                    broker_order_id=f"stop-{idx}",
                    symbol="LABT",
                    side="sell",
                    order_type="STOP",
                    time_in_force="day",
                    quantity=Decimal("10"),
                    status="cancelled",
                    payload={"native_stop_guard": "true"},
                    submitted_at=datetime.now(UTC),
                )
            )
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_30s"])
    assert summary == {"orders": 0, "terminal_orders": 0}

    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "LABT"))
        assert stored_intent is not None
        assert stored_intent.status == "cancelled"


@pytest.mark.asyncio
async def test_oms_service_sync_terminalizes_native_stop_cancel_intent_when_target_is_terminal() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeOrderSyncBrokerAdapter(None),
    )

    with session_factory() as session:
        strategy = service.store.ensure_strategy(session, "macd_30s", name="MACD 30s")
        account = service.store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="simulated",
            environment="paper",
        )
        target_client_order_id = "macd_30s-GCTK-close-stop"
        session.add(
            BrokerOrder(
                intent_id=None,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=target_client_order_id,
                broker_order_id="stop-target",
                symbol="GCTK",
                side="sell",
                order_type="STOP",
                time_in_force="day",
                quantity=Decimal("10"),
                status="cancelled",
                payload={"native_stop_guard": "true"},
                submitted_at=datetime.now(UTC),
            )
        )
        cancel_intent = service.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=account,
            event=TradeIntentEvent(
                source_service="oms-risk",
                payload=TradeIntentPayload(
                    strategy_code="macd_30s",
                    broker_account_name="paper:macd_30s",
                    symbol="GCTK",
                    side="sell",
                    quantity=Decimal("10"),
                    intent_type="cancel",
                    reason="NATIVE_STOP_GUARD_CANCEL",
                    metadata={
                        "native_stop_guard_manage": "true",
                        "target_client_order_id": target_client_order_id,
                        "broker_order_id": "stop-target",
                    },
                ),
            ),
        )
        cancel_intent.status = "accepted"
        session.commit()

    await service.sync_broker_orders(account_names=["paper:macd_30s"])

    with session_factory() as session:
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
        assert stored_intent is not None
        assert stored_intent.status == "cancelled"


@pytest.mark.asyncio
async def test_oms_service_refreshes_stale_working_limit_buy_order() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="BFRG",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P3_MACD_SURGE",
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "1.15",
                    "reference_price": "1.15",
                    "price_source": "ask",
                },
            ),
        )
    )

    with session_factory() as session:
        stored_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id.like("macd_1m-BFRG-open-%")))
        assert stored_order is not None
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        stored_order.updated_at = stale_time
        stored_order.submitted_at = stale_time
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_1m"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "BFRG").order_by(BrokerOrder.client_order_id)
        ).all()
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "BFRG"))

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].payload["limit_price"] == "1.23"
        assert orders[1].payload["watchdog_replaces_client_order_id"] == orders[0].client_order_id
        assert stored_intent is not None
        assert stored_intent.status == "submitted"

    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["accepted", "accepted"]
    assert all(item["payload"]["status"] != "cancelled" for item in order_events)
    assert [request.intent_type for request in adapter.submit_requests] == ["open", "cancel", "open"]


@pytest.mark.asyncio
async def test_oms_service_refreshes_remaining_quantity_for_stale_sell_order() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(
        fetch_event_type="partially_filled",
        filled_quantity=Decimal("4"),
        fill_price=Decimal("2.50"),
        bid_price=2.41,
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("10"),
            reason="HARD_STOP",
            status="submitted",
            payload={"metadata": {"order_type": "limit"}},
        )
        session.add(intent)
        session.flush()
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-abc123",
                broker_order_id="ord-123",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.40",
                    "reference_price": "2.40",
                    "price_source": "bid",
                },
                submitted_at=stale_time,
                updated_at=stale_time,
            )
        )
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_30s"])
    assert summary == {"orders": 2, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "UGRO").order_by(BrokerOrder.client_order_id)
        ).all()
        fills = session.scalars(select(Fill).where(Fill.symbol == "UGRO")).all()
        stored_intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "UGRO"))

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].quantity == Decimal("6")
        assert orders[1].payload["limit_price"] == "2.41"
        assert len(fills) == 1
        assert fills[0].quantity == Decimal("4")
        assert stored_intent is not None
        assert stored_intent.status == "submitted"

    order_events = [payload for stream, payload in redis.entries if stream == "test:order-events"]
    assert [item["payload"]["status"] for item in order_events] == ["partially_filled", "accepted"]
    assert all(item["payload"]["status"] != "cancelled" for item in order_events)
    assert [request.intent_type for request in adapter.submit_requests] == ["cancel", "close"]


@pytest.mark.asyncio
async def test_oms_service_refreshes_stop_guard_sell_order_with_wider_panic_limit() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(
        fetch_event_type="accepted",
        bid_price=2.41,
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
            oms_stop_guard_refresh_stage_1_seconds=0.5,
            oms_stop_guard_refresh_stage_2_seconds=1.0,
            oms_stop_guard_refresh_stage_3_seconds=2.0,
            oms_stop_guard_refresh_stage_1_buffer_pct=3.0,
            oms_stop_guard_refresh_stage_2_buffer_pct=5.0,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("10"),
            reason="HARD_STOP",
            status="submitted",
            payload={"metadata": {"order_type": "limit"}},
        )
        session.add(intent)
        session.flush()
        stale_time = datetime.now(UTC) - timedelta(seconds=10)
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-stop0",
                broker_order_id="ord-stop0",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.35",
                    "reference_price": "2.35",
                    "price_source": "bid",
                    "stop_guard": "true",
                    "panic_buffer_pct": "1.5",
                },
                submitted_at=stale_time,
                updated_at=stale_time,
            )
        )
        session.commit()

    summary = await service.sync_broker_orders(account_names=["paper:macd_30s"])
    assert summary == {"orders": 1, "terminal_orders": 1}

    with session_factory() as session:
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "UGRO").order_by(BrokerOrder.client_order_id)
        ).all()

        assert len(orders) == 2
        assert orders[0].status == "cancelled"
        assert orders[1].status == "accepted"
        assert orders[1].payload["limit_price"] == "2.34"
        assert orders[1].payload["panic_buffer_pct"] == "3.0"
        assert orders[1].payload["stop_guard_refresh_stage"] == "1"


@pytest.mark.asyncio
async def test_oms_service_builds_second_stage_stop_guard_refresh_with_five_percent_buffer() -> None:
    adapter = FakeWorkingOrderRefreshBrokerAdapter(bid_price=2.41)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_stop_guard_refresh_stage_1_buffer_pct=3.0,
            oms_stop_guard_refresh_stage_2_buffer_pct=5.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=adapter,
    )

    order = BrokerOrder(
        strategy_id=None,  # type: ignore[arg-type]
        broker_account_id=None,  # type: ignore[arg-type]
        client_order_id="macd_30s-UGRO-close-stop1",
        broker_order_id="ord-stop1",
        symbol="UGRO",
        side="sell",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal("10"),
        status="accepted",
        payload={
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": "2.34",
            "reference_price": "2.34",
            "price_source": "bid",
            "stop_guard": "true",
            "panic_buffer_pct": "3.0",
            "stop_guard_refresh_stage": "1",
        },
    )

    refreshed = await service._build_refreshed_order_metadata(
        broker_account_name="paper:macd_30s",
        order=order,
    )

    assert refreshed is not None
    assert refreshed["limit_price"] == "2.29"
    assert refreshed["panic_buffer_pct"] == "5.0"
    assert refreshed["stop_guard_refresh_stage"] == "2"


@pytest.mark.asyncio
async def test_oms_service_uses_catastrophic_after_hours_stop_guard_refresh_when_quote_is_far_below_stop() -> None:
    adapter = FakeWorkingOrderRefreshBrokerAdapter(bid_price=2.10, ask_price=2.12)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_after_hours_stop_guard_catastrophic_gap_pct=1.5,
            oms_after_hours_stop_guard_catastrophic_panic_buffer_pct=8.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=adapter,
    )

    order = BrokerOrder(
        strategy_id=None,  # type: ignore[arg-type]
        broker_account_id=None,  # type: ignore[arg-type]
        client_order_id="macd_30s-UGRO-close-stop-cat",
        broker_order_id="ord-stop-cat",
        symbol="UGRO",
        side="sell",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal("10"),
        status="accepted",
        payload={
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": "2.29",
            "reference_price": "2.29",
            "price_source": "bid",
            "stop_guard": "true",
            "panic_buffer_pct": "1.0",
            "stop_price": "2.35",
            "session": "AM",
            "extended_hours": "true",
        },
    )

    refreshed = await service._build_refreshed_order_metadata(
        broker_account_name="paper:macd_30s",
        order=order,
    )

    assert refreshed is not None
    assert refreshed["limit_price"] == "1.93"
    assert refreshed["reference_price"] == "1.93"
    assert refreshed["panic_buffer_pct"] == "8.0"
    assert refreshed["catastrophic_stop_guard"] == "true"
    assert refreshed["stop_guard_refresh_stage"] == "2"
    assert refreshed["watchdog_refresh_reason"] == "catastrophic_gap"


@pytest.mark.asyncio
async def test_oms_service_uses_fast_broker_sync_interval_when_stop_guard_order_is_active() -> None:
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_broker_sync_interval_seconds=5,
            oms_stop_guard_refresh_stage_1_seconds=0.5,
        ),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(session, "macd_30s", name="MACD 30s", execution_mode="paper", metadata_json={})
        account = store.ensure_broker_account(
            session,
            "paper:macd_30s",
            provider="schwab",
            environment="development",
        )
        session.add(
            BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="macd_30s-UGRO-close-live",
                broker_order_id="ord-live",
                symbol="UGRO",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("10"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "stop_guard": "true",
                    "panic_buffer_pct": "1.5",
                },
                submitted_at=datetime.now(UTC),
            )
        )
        session.commit()

    assert await service._broker_sync_interval_seconds() == 0.5


@pytest.mark.asyncio
async def test_oms_service_applies_after_hours_stop_guard_overrides_when_arming_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
    )
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_after_hours_stop_guard_quote_max_age_ms=1000,
            oms_after_hours_stop_guard_initial_panic_buffer_pct=1.0,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    service._update_hard_stop_registry_from_fill(
        strategy_code="schwab_1m",
        broker_account_name="live:schwab_1m",
        symbol="UGRO",
        side="buy",
        intent_type="open",
        quantity=Decimal("10"),
        price=Decimal("4.00"),
        metadata={
            "stop_guard_enabled": "true",
            "stop_loss_pct": "1.5",
            "stop_guard_quote_max_age_ms": "2000",
            "stop_guard_initial_panic_buffer_pct": "0.5",
            "session": "PM",
            "extended_hours": "true",
        },
    )

    stop = service._armed_hard_stops[("schwab_1m", "live:schwab_1m", "UGRO")]
    assert stop.quote_max_age_ms == 1000
    assert stop.initial_panic_buffer_pct == 1.0


@pytest.mark.asyncio
async def test_oms_service_arms_hard_stop_from_open_fill_and_triggers_close_on_quote_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeTickDrivenHardStopBrokerAdapter(open_fill_price=Decimal("4.00"))
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    key = ("macd_30s", "paper:macd_30s", "UGRO")
    assert key in service._armed_hard_stops
    assert service._armed_hard_stops[key].stop_price == Decimal("3.940")

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.93"),
                    ask_price=Decimal("3.95"),
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    close_request = adapter.submit_requests[-1]
    assert close_request.reason == "HARD_STOP"
    assert close_request.metadata["stop_guard"] == "true"
    assert close_request.metadata["stop_trigger_source"] == "bid"
    assert close_request.metadata["limit_price"] == "3.91"
    assert close_request.metadata["price_source"] == "bid"
    assert service._armed_hard_stops[key].close_in_flight is True


@pytest.mark.asyncio
async def test_oms_service_uses_trade_trigger_when_fresh_bid_has_not_breached_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 3, 31, 11, 0, tzinfo=UTC)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: fixed_now)
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeTickDrivenHardStopBrokerAdapter(open_fill_price=Decimal("4.00"))
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    key = ("macd_30s", "paper:macd_30s", "UGRO")
    assert key in service._armed_hard_stops

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.95"),
                    ask_price=Decimal("3.97"),
                ),
            ).model_dump_json()
        }
    )

    await service._handle_stream_message(
        {
            "data": TradeTickEvent(
                source_service="market-data",
                payload=TradeTickPayload(
                    symbol="UGRO",
                    price=Decimal("3.93"),
                    size=100,
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    close_request = adapter.submit_requests[-1]
    assert close_request.reason == "HARD_STOP"
    assert close_request.metadata["stop_trigger_source"] == "last"
    assert close_request.metadata["stop_trigger_price"] == "3.93"
    assert close_request.metadata["price_source"] == "last"
    assert service._armed_hard_stops[key].close_in_flight is True


@pytest.mark.asyncio
async def test_oms_service_arms_native_stop_guard_in_regular_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeNativeStopGuardBrokerAdapter(open_fill_price=Decimal("4.00"))
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]
    native_stop_request = adapter.submit_requests[-1]
    assert native_stop_request.reason == service.NATIVE_STOP_GUARD_REASON
    assert native_stop_request.metadata["native_stop_guard"] == "true"
    assert native_stop_request.metadata["order_type"] == "STOP"
    assert native_stop_request.metadata["stop_price"] == "3.94"

    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="UGRO",
                    bid_price=Decimal("3.93"),
                    ask_price=Decimal("3.95"),
                ),
            ).model_dump_json()
        }
    )

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "close"]


@pytest.mark.asyncio
async def test_oms_service_cancels_and_rearms_native_stop_guard_around_regular_hours_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "project_mai_tai.oms.service.utcnow",
        lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
    )
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeNativeStopGuardBrokerAdapter(open_fill_price=Decimal("4.00"))
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={
                    "reference_price": "4.00",
                    "stop_guard_enabled": "true",
                    "stop_loss_pct": "1.5",
                    "stop_guard_quote_max_age_ms": "2000",
                    "stop_guard_initial_panic_buffer_pct": "0.5",
                },
            ),
        )
    )

    sell_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("4"),
                intent_type="scale",
                reason="SCALE_1",
                metadata={"reference_price": "4.20"},
            ),
        )
    )

    request_flow = [(request.intent_type, request.reason) for request in adapter.submit_requests]
    assert request_flow == [
        ("open", "ENTRY_P1_MACD_CROSS"),
        ("close", service.NATIVE_STOP_GUARD_REASON),
        ("cancel", "NATIVE_STOP_GUARD_CANCEL"),
        ("scale", "SCALE_1"),
        ("close", service.NATIVE_STOP_GUARD_REASON),
    ]
    rearmed_stop_request = adapter.submit_requests[-1]
    assert rearmed_stop_request.metadata["native_stop_guard"] == "true"
    assert rearmed_stop_request.quantity == Decimal("6")
    assert any(event.payload.reason == "NATIVE_STOP_GUARD_CANCEL" for event in sell_events)

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("6")


@pytest.mark.asyncio
async def test_oms_service_refreshes_broker_positions_before_rejecting_exit() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        account_position.quantity = Decimal("0")
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.40"},
            ),
        )
    )

    assert [event.payload.status for event in events] == ["accepted", "filled"]

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("0")


@pytest.mark.asyncio
async def test_oms_service_still_rejects_exit_when_broker_refresh_confirms_no_position() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition))
        assert account_position is not None
        account_position.quantity = Decimal("0")
        session.commit()

    service.broker_adapter.seed_account_positions("paper:macd_30s", {})  # type: ignore[attr-defined]

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.40"},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "rejected"
    assert events[0].payload.reason == "no broker position available to sell"


@pytest.mark.asyncio
async def test_oms_service_rejects_duplicate_exit_in_flight() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakePendingExitBrokerAdapter(),
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    first = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.40"},
            ),
        )
    )
    assert first[0].payload.status == "accepted"

    duplicate = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={"reference_price": "2.39"},
            ),
        )
    )

    assert len(duplicate) == 1
    assert duplicate[0].payload.status == "rejected"
    assert duplicate[0].payload.reason == "duplicate_exit_in_flight"


@pytest.mark.asyncio
async def test_oms_service_hard_stop_preempts_existing_scale_exit() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    broker_adapter = FakeScaleThenHardStopBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=broker_adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    scale_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("5"),
                intent_type="scale",
                reason="SCALE_PCT2",
                metadata={"level": "PCT2", "reference_price": "2.90", "order_type": "limit", "time_in_force": "day"},
            ),
        )
    )
    assert [event.payload.status for event in scale_events] == ["accepted"]

    hard_stop_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={
                    "stop_guard": "true",
                    "reference_price": "2.40",
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.38",
                },
            ),
        )
    )

    assert [event.payload.status for event in hard_stop_events] == ["cancelled", "accepted"]
    assert hard_stop_events[0].payload.intent_type == "cancel"
    assert hard_stop_events[0].payload.reason == "HARD_STOP_PREEMPT_PENDING_EXIT"
    assert hard_stop_events[1].payload.intent_type == "close"
    assert hard_stop_events[1].payload.reason == "HARD_STOP"
    assert [request.intent_type for request in broker_adapter.submit_requests] == ["open", "scale", "cancel", "close"]

    with session_factory() as session:
        open_orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.status.in_(OmsStore.OPEN_ORDER_STATUSES))
        ).all()
        assert len(open_orders) == 1
        assert open_orders[0].payload.get("stop_guard") == "true"


@pytest.mark.asyncio
async def test_oms_service_hard_stop_preempts_existing_stop_guard_close() -> None:
    """A second HARD_STOP close must cancel a stuck prior HARD_STOP close, not bail out."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    broker_adapter = FakeHardStopThenHardStopBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=broker_adapter,
    )

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    first_hard_stop = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={
                    "stop_guard": "true",
                    "order_type": "limit",
                    "limit_price": "2.40",
                    "reference_price": "2.40",
                    "time_in_force": "day",
                },
            ),
        )
    )
    assert [event.payload.status for event in first_hard_stop] == ["accepted"]

    second_hard_stop = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={
                    "stop_guard": "true",
                    "order_type": "limit",
                    "limit_price": "2.35",
                    "reference_price": "2.35",
                    "time_in_force": "day",
                },
            ),
        )
    )

    statuses = [event.payload.status for event in second_hard_stop]
    intent_types = [event.payload.intent_type for event in second_hard_stop]
    reasons = [event.payload.reason for event in second_hard_stop]

    assert statuses[0] == "cancelled"
    assert intent_types[0] == "cancel"
    assert reasons[0] == "HARD_STOP_PREEMPT_PENDING_EXIT"
    assert "accepted" in statuses[1:]
    assert [request.intent_type for request in broker_adapter.submit_requests] == [
        "open",
        "close",
        "cancel",
        "close",
    ]

    with session_factory() as session:
        open_orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.status.in_(OmsStore.OPEN_ORDER_STATUSES))
        ).all()
        assert len(open_orders) == 1
        assert open_orders[0].payload.get("stop_guard") == "true"
        assert open_orders[0].broker_order_id == "ord-hard-stop-2"


@pytest.mark.asyncio
async def test_oms_service_stop_guard_close_gets_market_fallback_on_stop_rejection() -> None:
    """A HARD_STOP close rejected by the broker for stop reasons must escalate to a market fallback."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    broker_adapter = FakeStopGuardCloseRejectFallbackBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=broker_adapter,
    )

    open_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )
    assert [event.payload.status for event in open_events] == ["accepted", "filled"]

    hard_stop_events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("10"),
                intent_type="close",
                reason="HARD_STOP",
                metadata={
                    "stop_guard": "true",
                    "order_type": "limit",
                    "limit_price": "2.40",
                    "reference_price": "2.40",
                    "time_in_force": "day",
                },
            ),
        )
    )

    statuses = [event.payload.status for event in hard_stop_events]
    assert "rejected" in statuses
    assert "filled" in statuses
    fallback_fill = next(
        event for event in hard_stop_events if event.payload.status == "filled"
    )
    assert fallback_fill.payload.reason == "STOP_REJECTED_FALLBACK"
    assert (
        str(fallback_fill.payload.metadata.get("stop_reject_fallback", "")).lower()
        == "true"
    )

    submitted_intents = [request.intent_type for request in broker_adapter.submit_requests]
    assert submitted_intents == ["open", "close", "close"]

    with session_factory() as session:
        account_position = session.scalar(
            select(AccountPosition).where(AccountPosition.symbol == "UGRO")
        )
        assert account_position is not None
        assert account_position.quantity == Decimal("0")


@pytest.mark.asyncio
async def test_oms_service_submits_market_fallback_after_stop_rejection() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    broker_adapter = FakeStopRejectedFallbackBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=broker_adapter,
    )

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="UGRO",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_MACD_CROSS",
                metadata={"reference_price": "2.55"},
            ),
        )
    )

    assert [event.payload.status for event in events] == ["rejected", "accepted", "filled"]
    assert events[1].payload.intent_type == "close"
    assert events[2].payload.reason == "STOP_REJECTED_FALLBACK"
    assert broker_adapter.submitted == [
        ("open", "buy", "UGRO", Decimal("10")),
        ("close", "sell", "UGRO", Decimal("10")),
    ]

    with session_factory() as session:
        account_position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "UGRO"))
        assert account_position is not None
        assert account_position.quantity == Decimal("0")


@pytest.mark.asyncio
async def test_oms_service_shared_account_exit_uses_strategy_virtual_quantity() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    with session_factory() as session:
        tos = service.store.ensure_strategy(session, "tos")
        runner = service.store.ensure_strategy(session, "runner")
        account = service.store.ensure_broker_account(
            session,
            "paper:tos_runner_shared",
            provider="alpaca",
            environment="paper",
        )
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=tos.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("60"),
                    average_price=Decimal("2.00"),
                    realized_pnl=Decimal("0"),
                ),
                VirtualPosition(
                    strategy_id=runner.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("40"),
                    average_price=Decimal("2.10"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("100"),
                    average_price=Decimal("2.04"),
                    market_value=Decimal("204.00"),
                ),
            ]
        )
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="runner",
                broker_account_name="paper:tos_runner_shared",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("100"),
                intent_type="close",
                reason="TRAIL_STOP_10%",
                metadata={},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "accepted"
    assert events[0].payload.quantity == Decimal("40")


@pytest.mark.asyncio
async def test_oms_service_shared_account_exit_respects_pending_exit_reservations() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeAcceptedOnlyBrokerAdapter(),
    )

    with session_factory() as session:
        tos = service.store.ensure_strategy(session, "tos")
        runner = service.store.ensure_strategy(session, "runner")
        account = service.store.ensure_broker_account(
            session,
            "paper:tos_runner_shared",
            provider="alpaca",
            environment="paper",
        )
        existing_intent = TradeIntent(
            strategy_id=tos.id,
            broker_account_id=account.id,
            symbol="UGRO",
            side="sell",
            intent_type="close",
            quantity=Decimal("60"),
            reason="TRAIL_STOP_10%",
            status="submitted",
            payload={},
        )
        session.add(existing_intent)
        session.flush()
        session.add(
            BrokerOrder(
                intent_id=existing_intent.id,
                strategy_id=tos.id,
                broker_account_id=account.id,
                client_order_id="tos-exit-1",
                broker_order_id="ord-existing",
                symbol="UGRO",
                side="sell",
                order_type="market",
                time_in_force="day",
                quantity=Decimal("60"),
                status="accepted",
                payload={},
            )
        )
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=runner.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("50"),
                    average_price=Decimal("2.10"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("100"),
                    average_price=Decimal("2.04"),
                    market_value=Decimal("204.00"),
                ),
            ]
        )
        session.commit()

    events = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="runner",
                broker_account_name="paper:tos_runner_shared",
                symbol="UGRO",
                side="sell",
                quantity=Decimal("50"),
                intent_type="close",
                reason="TRAIL_STOP_10%",
                metadata={},
            ),
        )
    )

    assert len(events) == 1
    assert events[0].payload.status == "accepted"
    assert events[0].payload.quantity == Decimal("40")


def test_store_clears_virtual_positions_without_broker_backing() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy = Strategy(code="macd_30s", name="MACD 30S", execution_mode="paper", metadata_json={})
        account = BrokerAccount(name="paper:macd_30s", provider="alpaca", environment="development")
        session.add_all([strategy, account])
        session.flush()
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="UGRO",
                    quantity=Decimal("10"),
                    average_price=Decimal("2.50"),
                    realized_pnl=Decimal("0"),
                ),
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol="MESA",
                    quantity=Decimal("5"),
                    average_price=Decimal("1.25"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol="MESA",
                    quantity=Decimal("5"),
                    average_price=Decimal("1.25"),
                    market_value=Decimal("6.25"),
                ),
            ]
        )
        session.commit()

    store = OmsStore()
    with session_factory() as session:
        cleared = store.clear_virtual_positions_without_account_backing(session)
        session.commit()

    # Returns WHAT it erased, not just how many: the caller has to be able to log the symbols.
    assert len(cleared) == 1
    assert [symbol for _account_id, symbol, _quantity in cleared] == ["UGRO"]
    assert cleared[0][2] == Decimal("10"), "the quantity BEFORE the clear must survive into the log"
    with session_factory() as session:
        positions = {position.symbol: position for position in session.scalars(select(VirtualPosition)).all()}
        assert positions["UGRO"].quantity == Decimal("0")
        assert positions["UGRO"].average_price == Decimal("0")
        assert positions["MESA"].quantity == Decimal("5")


# ---- Stuck-intent cancellation (2026-05-18 incident regression tests) ---------
#
# Three guards prevent the 4.5-hour / 414-attempt loop that hit AUUD/QNCX/SBFM:
#   Tier 1: quote-tick instant cancel when limit drifts past ask/bid
#   Tier 2: intent max-age cap (default 30s)
#   Tier 3: setup re-validation against strategy_bar_history before retry


@pytest.mark.asyncio
async def test_oms_service_cancels_buy_limit_when_ask_drifts_past_limit() -> None:
    """Tier 1: a quote tick whose ask > limit + tolerance cancels the order and
    marks the intent abandoned with reason QUOTE_DRIFT_CANCEL."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(ask_price=2.13, bid_price=2.11)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_quote_drift_cancel_tolerance_cents=1.0,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="AUUD",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P2_VWAP",
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.11",
                    "reference_price": "2.11",
                    "price_source": "ask",
                    "path": "P2_VWAP",
                },
            ),
        )
    )

    # Quote tick: ask is now 2.13 — 2c past the 2.11 limit.
    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="AUUD",
                    bid_price=Decimal("2.11"),
                    ask_price=Decimal("2.13"),
                ),
            ).model_dump_json()
        }
    )

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "AUUD"))
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "AUUD")
        ).all()
        assert intent is not None
        assert intent.status == "cancelled"
        assert len(orders) == 1
        assert orders[0].status == "cancelled"
        assert orders[0].payload.get("abandon_reason_code") == "QUOTE_DRIFT_CANCEL"

    # Critical: exactly one cancel went to the broker; NO replacement order.
    assert [request.intent_type for request in adapter.submit_requests] == ["open", "cancel"]


@pytest.mark.asyncio
async def test_oms_service_does_not_cancel_when_ask_within_tolerance() -> None:
    """Tier 1 sanity: quote within tolerance does not trigger cancellation."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(ask_price=2.115, bid_price=2.10)
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_quote_drift_cancel_tolerance_cents=1.0,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="AUUD",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P2_VWAP",
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.11",
                    "reference_price": "2.11",
                    "price_source": "ask",
                    "path": "P2_VWAP",
                },
            ),
        )
    )

    # Ask drifts only 0.5c past the limit — within 1c tolerance.
    await service._handle_stream_message(
        {
            "data": QuoteTickEvent(
                source_service="market-data",
                payload=QuoteTickPayload(
                    symbol="AUUD",
                    bid_price=Decimal("2.10"),
                    ask_price=Decimal("2.115"),
                ),
            ).model_dump_json()
        }
    )

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "AUUD"))
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "AUUD")
        ).all()
        assert intent is not None
        assert intent.status != "cancelled"
        assert all(order.status != "cancelled" for order in orders)


@pytest.mark.asyncio
async def test_oms_service_abandons_intent_after_max_age_instead_of_refresh() -> None:
    """Tier 2: intent older than oms_intent_max_age_seconds is abandoned, not refreshed."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
            oms_intent_max_age_seconds=30,
            oms_intent_setup_revalidation_enabled=False,
            oms_quote_drift_cancel_tolerance_cents=0.0,  # don't fire Tier 1 here
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_1m",
                broker_account_name="paper:macd_1m",
                symbol="BFRG",
                side="buy",
                quantity=Decimal("100"),
                intent_type="open",
                reason="ENTRY_P3_MACD_SURGE",
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "1.15",
                    "reference_price": "1.15",
                    "price_source": "ask",
                    "path": "P3_MACD_SURGE",
                },
            ),
        )
    )

    # Age the intent + order well past the 30s cap so refresh would trigger.
    now = datetime.now(UTC)
    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "BFRG"))
        assert intent is not None
        intent.created_at = now - timedelta(seconds=120)
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "BFRG"))
        assert order is not None
        order.updated_at = now - timedelta(seconds=10)
        order.submitted_at = now - timedelta(seconds=120)
        session.commit()

    await service.sync_broker_orders(account_names=["paper:macd_1m"])

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "BFRG"))
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "BFRG")
        ).all()
        assert intent is not None
        assert intent.status == "cancelled"
        assert len(orders) == 1  # NO replacement order spawned
        assert orders[0].status == "cancelled"
        assert orders[0].payload.get("abandon_reason_code") == "INTENT_MAX_AGE"

    # Open + cancel went out; no second open (no retry).
    assert [request.intent_type for request in adapter.submit_requests] == ["open", "cancel"]


@pytest.mark.asyncio
async def test_oms_service_abandons_intent_when_setup_no_longer_matches() -> None:
    """Tier 3: latest strategy_bar_history bar status != signal -> abandon, not refresh."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
            oms_intent_max_age_seconds=0,  # disable Tier 2 here
            oms_intent_setup_revalidation_enabled=True,
            oms_quote_drift_cancel_tolerance_cents=0.0,  # disable Tier 1 here
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(
        TradeIntentEvent(
            source_service="strategy-engine",
            payload=TradeIntentPayload(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="GOVX",
                side="buy",
                quantity=Decimal("10"),
                intent_type="open",
                reason="ENTRY_P1_CROSS",
                metadata={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "2.40",
                    "reference_price": "2.40",
                    "price_source": "ask",
                    "path": "P1_CROSS",
                },
            ),
        )
    )

    # Strategy publishes a fresh bar showing the setup is no longer 'signal'.
    now = datetime.now(UTC)
    with session_factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "macd_30s"))
        assert strategy is not None
        session.add(
            StrategyBarHistory(
                strategy_code="macd_30s",
                symbol="GOVX",
                interval_secs=30,
                bar_time=now - timedelta(seconds=2),
                open_price=Decimal("2.39"),
                high_price=Decimal("2.41"),
                low_price=Decimal("2.38"),
                close_price=Decimal("2.39"),
                volume=10000,
                trade_count=20,
                position_state="flat",
                position_quantity=0,
                decision_status="idle",
                decision_reason="momentum faded",
                decision_path="",
                decision_score="",
                decision_score_details="",
                indicators_json={},
            )
        )
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "GOVX"))
        assert order is not None
        order.updated_at = now - timedelta(seconds=10)
        order.submitted_at = now - timedelta(seconds=10)
        session.commit()

    await service.sync_broker_orders(account_names=["paper:macd_30s"])

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "GOVX"))
        orders = session.scalars(
            select(BrokerOrder).where(BrokerOrder.symbol == "GOVX")
        ).all()
        assert intent is not None
        assert intent.status == "cancelled"
        assert len(orders) == 1
        assert orders[0].status == "cancelled"
        assert orders[0].payload.get("abandon_reason_code") == "SETUP_INVALID"

    assert [request.intent_type for request in adapter.submit_requests] == ["open", "cancel"]


class _FakeIntent:
    def __init__(self, path: str, symbol: str = "SKYQ", intent_type: str = "open") -> None:
        self.intent_type = intent_type
        self.symbol = symbol
        self.payload = {"metadata": {"path": path}}


class _FakeStrategy:
    def __init__(self, code: str) -> None:
        self.code = code


def _bar(code: str, sym: str, status: str, path: str, bar_time: datetime) -> StrategyBarHistory:
    return StrategyBarHistory(
        strategy_code=code, symbol=sym, interval_secs=60, bar_time=bar_time,
        open_price=Decimal("1"), high_price=Decimal("1"), low_price=Decimal("1"),
        close_price=Decimal("1"), volume=100, decision_status=status, decision_path=path,
    )


def _svc(session_factory):
    return OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )


def test_setup_revalidation_failopen_for_tapeless_v2_atr() -> None:
    # Regression: the isolated schwab_1m_v2 bot persists OHLCV bars but writes NO
    # decision tape (decision_status=''). The Tier-3 setup-revalidation guard must
    # FAIL OPEN for such strategies — otherwise it abandoned every v2 ATR-Flip
    # intent that did not fill instantly (i.e. all after-hours fills).
    sf = build_test_session_factory()
    svc = _svc(sf)
    intent = _FakeIntent("ATR Flip", symbol="SKYQ")
    strat = _FakeStrategy("schwab_1m_v2")
    with sf() as s:
        s.add(_bar("schwab_1m_v2", "SKYQ", "", "", datetime(2026, 6, 22, 20, 11, tzinfo=UTC)))
        s.commit()
        assert svc._intent_setup_invalid_reason(s, intent=intent, strategy=strat) is None


def test_setup_revalidation_still_guards_tape_writing_momentum_bots() -> None:
    # The guard must STILL protect the momentum bots (which DO write the tape):
    # a non-signal latest bar -> abandon; a matching signal bar -> keep.
    sf = build_test_session_factory()
    svc = _svc(sf)
    intent = _FakeIntent("P1_CROSS", symbol="GOVX")
    strat = _FakeStrategy("macd_30s")
    with sf() as s:
        s.add(_bar("macd_30s", "GOVX", "idle", "", datetime(2026, 6, 22, 14, 0, tzinfo=UTC)))
        s.commit()
        assert svc._intent_setup_invalid_reason(s, intent=intent, strategy=strat) is not None
    sf2 = build_test_session_factory()
    svc2 = _svc(sf2)
    with sf2() as s:
        s.add(_bar("macd_30s", "GOVX", "signal", "P1_CROSS", datetime(2026, 6, 22, 14, 0, tzinfo=UTC)))
        s.commit()
        assert svc2._intent_setup_invalid_reason(s, intent=intent, strategy=strat) is None


def _seed_stale_close_order(session_factory, *, symbol: str = "SOBR"):
    """Insert a submitted CLOSE (sell limit) intent + a stale accepted working order
    — the shape of the 2026-07-13 overnight AGEN/SOBR exit that churned forever."""
    store = OmsStore()
    with session_factory() as session:
        strategy = store.ensure_strategy(
            session, "schwab_1m_v2", name="Schwab 1m v2", execution_mode="live", metadata_json={}
        )
        account = store.ensure_broker_account(
            session, "live:schwab_1m_v2", provider="schwab", environment="development"
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol=symbol,
            side="sell",
            intent_type="close",
            quantity=Decimal("2"),
            reason="CW_HARD_STOP",
            status="submitted",
            payload={"metadata": {"order_type": "limit"}},
        )
        session.add(intent)
        session.flush()
        stale = datetime.now(UTC) - timedelta(seconds=30)  # past the 5s refresh window
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"schwab_1m_v2-{symbol}-close-abc123",
                broker_order_id="ord-123",
                symbol=symbol,
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("2"),
                status="accepted",
                payload={
                    "order_type": "limit",
                    "time_in_force": "day",
                    "limit_price": "0.95",
                    "reference_price": "0.95",
                    "price_source": "bid",
                    "session": "AM",
                },
                submitted_at=stale,
                updated_at=stale,
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_oms_abandons_close_intent_when_market_not_fillable() -> None:
    """Churn fix: when the market is NOT in a fillable session, a working CLOSE
    intent is abandoned (MARKET_CLOSED) instead of endlessly cancel/re-placed — the
    2026-07-13 AGEN/SOBR overnight loop (181 refreshes on SOBR). No replacement order
    is spawned. The managed row stays open so a fresh exit re-emits when it reopens."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]
    # Market CLOSED regardless of wall-clock run time.
    service._market_is_fillable = lambda now=None: False  # type: ignore[assignment,method-assign]

    _seed_stale_close_order(session_factory)
    await service.sync_broker_orders(account_names=["live:schwab_1m_v2"])

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "SOBR"))
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "SOBR")).all()
        assert intent is not None
        assert intent.status == "cancelled"
        assert len(orders) == 1  # NO replacement spawned
        assert orders[0].status == "cancelled"
        assert orders[0].payload.get("abandon_reason_code") == "MARKET_CLOSED"
    # Only a cancel went out — no re-placed exit (the churn is stopped).
    assert [r.intent_type for r in adapter.submit_requests] == ["cancel"]


@pytest.mark.asyncio
async def test_oms_refreshes_close_intent_when_market_fillable() -> None:
    """Regression: during a fillable session the SAME stale close order refreshes
    (cancel + replace) as before — the gate only bites when the market is closed."""
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]
    service._market_is_fillable = lambda now=None: True  # type: ignore[assignment,method-assign]

    _seed_stale_close_order(session_factory)
    await service.sync_broker_orders(account_names=["live:schwab_1m_v2"])

    # A replacement close was placed (cancel + re-submit), i.e. normal exit management.
    assert [r.intent_type for r in adapter.submit_requests] == ["cancel", "close"]
    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "SOBR"))
        assert intent is not None
        assert intent.status == "submitted"  # still working, not abandoned


def _resting_order(order_type: str) -> BrokerOrder:
    return BrokerOrder(
        strategy_id=None,  # type: ignore[arg-type]
        broker_account_id=None,  # type: ignore[arg-type]
        client_order_id="schwab_1m_v2-NVVE-open-x",
        broker_order_id="o1",
        symbol="NVVE",
        side="buy",
        order_type=order_type.lower(),
        time_in_force="day",
        quantity=Decimal("2"),
        status="working",
        payload={"order_type": order_type},
    )


def test_resting_trigger_order_is_exempt_from_intent_max_age() -> None:
    """A buy STOP / STOP_LIMIT (the resting flip-entry) is a TRIGGER order -- DESIGNED to rest until
    price crosses it, so it must be exempt from the 30s INTENT_MAX_AGE abandon. A marketable LIMIT /
    MARKET chase is NOT exempt (that is exactly what the abandon exists to kill). Segregation is by
    order TYPE (2026-07-23 live finding: the OMS was re-cancelling the resting order every ~30-58s)."""
    assert OmsRiskService._is_resting_trigger_order(_resting_order("STOP_LIMIT")) is True
    assert OmsRiskService._is_resting_trigger_order(_resting_order("STOP")) is True
    assert OmsRiskService._is_resting_trigger_order(_resting_order("LIMIT")) is False
    assert OmsRiskService._is_resting_trigger_order(_resting_order("MARKET")) is False


# ---------------------------------------------------------- operator manual stop (2026-07-27)
# The operator cancelled a v2 resting order on DFNS twice and the bot RE-PLACED it within ~2 minutes
# each time. `_cw_v2_resting_track` places whenever `state.resting_active` is False, and a
# broker-side cancel clears exactly that flag — so the bot cannot tell "my order expired" from
# "a human killed this". Manual-stop was wired to the scanner and the in-process bots ONLY, never to
# the OMS or v2, so vetoing one symbol needed a blacklist AND an env edit AND a service restart.
#
# These drive `_evaluate_risk` directly — it is the chokepoint every intent passes and the unit under
# test; the full process_trade_intent happy path needs seeded strategy/account rows that the existing
# rejection-only tests in this file do not provide.


def _manual_stop_row(session_factory, symbols):
    from project_mai_tai.db.models import DashboardSnapshot
    with session_factory() as session:
        session.add(DashboardSnapshot(
            snapshot_type="global_manual_stop_symbols",
            payload={"symbols": list(symbols)},
        ))
        session.commit()


def _mstop_service(session_factory):
    return OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )


def _risk(service, symbol, intent_type="open", side="buy", quantity=Decimal("2")):
    service._load_global_manual_stop_symbols()   # process_trade_intent does this before the txn
    return service._evaluate_risk(TradeIntentEvent(
        source_service="strategy-engine",
        payload=TradeIntentPayload(
            strategy_code="macd_30s", broker_account_name="paper:macd_30s",
            symbol=symbol, side=side, quantity=quantity,
            intent_type=intent_type, reason="ENTRY_P1_MACD_CROSS", metadata={},
        ),
    ))


def test_manual_stop_blocks_entries_but_never_blocks_getting_out() -> None:
    """THE REGRESSION (and its same-day correction).

    Blocking entries is the point: the bot re-placed a hand-cancelled DFNS order twice.
    But blocking `close` would STRAND an open position -- the OMS could not exit it and the
    operator would have to sell by hand. A stop means "stop buying this", not "abandon what
    I hold". So the guard is EXPOSURE-DIRECTIONAL.
    """
    sf = build_test_session_factory()
    _manual_stop_row(sf, ["DFNS"])
    svc = _mstop_service(sf)

    # BLOCKED -- opens or increases exposure
    for it, side, qty in (("open", "buy", Decimal("2")),
                          ("open", "sell", Decimal("2")),      # a short entry is still new exposure
                          ("scale", "buy", Decimal("1"))):     # scale-IN
        ok, reason = _risk(svc, "dfns", it, side, qty)
        assert ok is False, (it, side)
        assert reason == "manual_stop:DFNS", (it, side, reason)

    # ALLOWED -- reduces exposure or cancels. Blocking ANY of these strands the operator.
    for it, side, qty in (("close", "sell", Decimal("2")),
                          ("cancel", "sell", Decimal("0")),
                          ("scale", "sell", Decimal("1"))):    # scale-OUT = the +2/4% ladder
        ok, reason = _risk(svc, "dfns", it, side, qty)
        assert ok is True, (it, side, reason)


def test_manual_stop_is_case_insensitive_and_per_symbol() -> None:
    """Lowercase input still blocks; a DIFFERENT symbol is untouched (a veto, not a kill switch)."""
    sf = build_test_session_factory()
    _manual_stop_row(sf, ["dfns"])
    svc = _mstop_service(sf)
    assert _risk(svc, "DFNS")[0] is False
    assert _risk(svc, "LGHL") == (True, "ok")


def test_no_manual_stop_row_blocks_nothing() -> None:
    """No snapshot row -> byte-identical to before this change."""
    svc = _mstop_service(build_test_session_factory())
    assert _risk(svc, "DFNS") == (True, "ok")


def test_manual_stop_load_failure_keeps_the_last_good_set() -> None:
    """FAIL-CLOSED: a DB blip must NEVER silently un-stop a symbol the operator halted."""
    sf = build_test_session_factory()
    _manual_stop_row(sf, ["DFNS"])
    svc = _mstop_service(sf)
    assert svc._load_global_manual_stop_symbols() == {"DFNS"}

    def boom():
        raise RuntimeError("db down")

    svc.session_factory = boom
    svc._manual_stop_loaded_at = -1e9              # force a refresh attempt
    assert svc._load_global_manual_stop_symbols() == {"DFNS"}   # last good set retained
    assert _risk(svc, "DFNS")[0] is False                        # and still blocked


def test_manual_stop_is_cached_not_queried_per_intent() -> None:
    """One query per cache window, not one per intent."""
    sf = build_test_session_factory()
    _manual_stop_row(sf, ["DFNS"])
    svc = _mstop_service(sf)
    calls = {"n": 0}
    real = svc.session_factory

    def counting():
        calls["n"] += 1
        return real()

    svc.session_factory = counting
    svc._manual_stop_loaded_at = -1e9
    for _ in range(5):
        _risk(svc, "DFNS")
    assert calls["n"] == 1, calls


# ------------------------------------- native-OCO exit fill capture (2026-07-27)
# Since the native OCO went live (2026-07-22) NO exit fill has been recorded: the exit executes on
# a broker-created child leg the OMS never placed. `collect_completed_trade_cycles` then has
# entries with no exits to pair, so the operator's completed-trades table and P&L render BLANK.
# Measured: Schwab sell fills 07-20: 3 · 07-21: 5 · 07-22: 1 (OCO goes live) · 07-23: 0 · 07-27: 0.

def _oco_service(session_factory, *, enabled=True):
    import types as _t
    svc = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )
    object.__setattr__(svc.settings, "oms_record_native_oco_exit_fills_enabled", enabled) \
        if not isinstance(svc.settings, _t.SimpleNamespace) else None
    return svc


def _seed_managed_row(session, acct="live:orb", symbol="BIYA"):
    """An OPEN oms_managed_positions row — the poll's ground truth since 2026-08-03.

    The poll used to iterate the in-memory guard, so these tests only had to add a key. It now
    iterates the OPEN ROWS, which is the whole point: a row the set has forgotten must still be
    polled and closed."""
    from project_mai_tai.db.models import OmsManagedPosition
    row = OmsManagedPosition(
        strategy_code="schwab_1m_v2", broker_account_name=acct, symbol=symbol,
        entry_price=Decimal("3.90"), original_quantity=1, current_quantity=1,
        entry_path="ATR Flip", entry_time=datetime(2026, 7, 28, 15, 30, tzinfo=UTC),
        status="open", config_name="make_v2_variant",
    )
    session.add(row)
    session.commit()
    return row


def _seed_entry(session):
    """A filled bracket ENTRY: strategy + account + intent + buy order."""
    from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, TradeIntent
    strategy = Strategy(code="schwab_1m_v2", name="V2", execution_mode="live")
    account = BrokerAccount(name="live:orb", provider="webull", environment="live")
    session.add_all([strategy, account])
    session.flush()
    intent = TradeIntent(
        strategy_id=strategy.id, broker_account_id=account.id, symbol="BIYA",
        side="buy", intent_type="open", quantity=Decimal("1"), reason="ATR Flip",
        status="filled", payload={},
    )
    session.add(intent)
    session.flush()
    order = BrokerOrder(
        intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
        client_order_id="schwab_1m_v2-BIYA-open-abc", symbol="BIYA", side="buy",
        order_type="market", time_in_force="day", quantity=Decimal("1"),
        status="filled", payload={"bracket": "true"},
    )
    session.add(order)
    session.commit()
    return order


_EXIT = {"symbol": "BIYA", "quantity": Decimal("1"), "price": Decimal("3.93"),
         "filled_at": datetime(2026, 7, 27, 15, 36, 30, tzinfo=UTC),
         "broker_order_id": "WB-EXIT-1"}


def test_the_broker_exit_is_recorded_as_a_real_fill() -> None:
    """THE FIX: without this the entry has no exit to pair with and the trade never completes."""
    from project_mai_tai.db.models import Fill
    sf = build_test_session_factory()
    with sf() as session:
        entry = _seed_entry(session)
        svc = _oco_service(sf)
        ok = svc._persist_oco_exit_fill(session, "live:orb", "BIYA", entry, _EXIT)
        session.commit()
        assert ok is True
        fills = session.scalars(select(Fill)).all()
        assert len(fills) == 1
        assert fills[0].side == "sell"
        assert fills[0].price == Decimal("3.93")
        assert fills[0].quantity == Decimal("1")


def test_recording_the_same_exit_twice_books_one_fill() -> None:
    """Both close paths can fire for the same symbol; double-booking would double the P&L."""
    from project_mai_tai.db.models import Fill
    sf = build_test_session_factory()
    with sf() as session:
        entry = _seed_entry(session)
        svc = _oco_service(sf)
        svc._persist_oco_exit_fill(session, "live:orb", "BIYA", entry, _EXIT)
        session.commit()
        svc._persist_oco_exit_fill(session, "live:orb", "BIYA", entry, _EXIT)
        session.commit()
        assert len(session.scalars(select(Fill)).all()) == 1


def test_a_zero_priced_exit_is_never_booked() -> None:
    """⛔ THE TRAP: a CANCELED sibling leg carries an execution priced 0.0. Booking it would
    write a $0 exit and report the trade as -100%."""
    from project_mai_tai.db.models import Fill
    sf = build_test_session_factory()
    with sf() as session:
        entry = _seed_entry(session)
        svc = _oco_service(sf)
        bad = dict(_EXIT, price=Decimal("0"))
        assert svc._persist_oco_exit_fill(session, "live:orb", "BIYA", entry, bad) is False
        session.commit()
        assert session.scalars(select(Fill)).all() == []


@pytest.mark.asyncio
async def test_flag_off_makes_no_broker_call() -> None:
    """Deploys inert."""
    sf = build_test_session_factory()
    svc = _oco_service(sf, enabled=False)
    called = []

    class _A:
        async def fetch_oco_exit_fill(self, *a, **k):
            called.append(a)
            return _EXIT

    svc.broker_adapter = _A()
    assert await svc._fetch_oco_exit_detail("live:orb", "BIYA", "base") is None
    assert called == []


@pytest.mark.asyncio
async def test_a_broker_failure_never_breaks_the_close_path() -> None:
    """PROTECTION > BOOKKEEPING. If the exit read fails the row must still close; losing the
    phantom-row cleanup would restart the rejected-close storm this whole path exists to end.

    ⭐ AMENDED 2026-07-28, deliberately. The failure is now RETRIED a bounded number of times before
    the row closes, because collapsing "the broker says no exit" into "we could not ask" lost a
    trade's P&L permanently on a transient Webull 429 (live: CNET 16:11 ET) -- the exact blackout
    this capture exists to close. So the helper now returns `_EXIT_FETCH_FAILED`, not `None`.

    ⛔ The invariant itself is UNCHANGED and still pinned below: the row always closes in the end.
    It is bounded by `_MAX_EXIT_FETCH_DEFERRALS` (~45s at the 15s sync), never open-ended, because
    an open managed row blocks fan-out re-entry. Protection still outranks bookkeeping -- it just
    gives bookkeeping a few seconds to succeed first.
    """
    sf = build_test_session_factory()
    svc = _oco_service(sf)

    class _Boom:
        async def fetch_oco_exit_fill(self, *a, **k):
            raise RuntimeError("broker down")

    svc.broker_adapter = _Boom()
    got = await svc._fetch_oco_exit_detail("live:orb", "BIYA", "base")
    assert got is _EXIT_FETCH_FAILED, "a transient failure must stay distinguishable from 'no exit'"

    # THE INVARIANT: retries are bounded, so the row is guaranteed to close.
    svc._oco_exit_fetch_deferrals = {}
    deferrals = 0
    while svc._defer_for_exit_fetch("live:orb", "BIYA"):
        deferrals += 1
        assert deferrals <= 10, "unbounded retry — the managed row would never close"
    assert deferrals == svc._MAX_EXIT_FETCH_DEFERRALS


@pytest.mark.asyncio
async def test_an_adapter_without_the_method_is_skipped() -> None:
    """Alpaca/simulated have no OCO children; absence is not an error."""
    sf = build_test_session_factory()
    svc = _oco_service(sf)
    svc.broker_adapter = object()
    assert await svc._fetch_oco_exit_detail("live:orb", "BIYA", "base") is None


# ------------------------------------ event-driven OCO exit poll (2026-07-28)
# The close path needs 3 REJECTED CLOSES + the 120s grace: measured lag exit 09:36:40 ->
# recorded 09:53:33 = ~17 MINUTES. That stale managed row also BLOCKS re-entry, because the
# fan-out guard `fanout_webull_collision_managed` refuses a leg while a managed row is open.
# Live 07-28 INLF: signal 5.4850 skipped (prior row still open, its exit filled 5s later),
# next signal filled 5.6200 = +2.46% worse against a +2% target. 7 of 9 lost signals that day.

def _poll_service(sf, *, enabled=True, min_secs=0.0):
    svc = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=sf,
    )
    object.__setattr__(svc.settings, "oms_native_oco_exit_poll_enabled", enabled)
    object.__setattr__(svc.settings, "oms_native_oco_exit_poll_min_secs", min_secs)
    object.__setattr__(svc.settings, "oms_record_native_oco_exit_fills_enabled", True)
    object.__setattr__(svc.settings, "strategy_schwab_1m_v2_account_name", "live:orb")
    assert svc.settings.oms_native_oco_exit_poll_enabled is enabled
    return svc


class _ExitAdapter:
    def __init__(self, detail=None):
        self.detail = detail
        self.calls = 0

    async def fetch_oco_exit_fill(self, acct, symbol, base="", *, resolved_within_seconds=3600.0,
                                  entry_broker_order_id="", entry_filled_at=None, entry_quantity=None):
        self.calls += 1
        return self.detail


_POLL_EXIT = {"symbol": "BIYA", "quantity": Decimal("1"), "price": Decimal("3.93"),
              "filled_at": datetime(2026, 7, 28, 15, 36, 30, tzinfo=UTC),
              "broker_order_id": "WB-POLL-1"}


@pytest.mark.asyncio
async def test_the_poll_records_the_exit_and_clears_the_row_without_waiting() -> None:
    """THE FIX: no rejected closes, no 120s grace — the row clears on the periodic sync."""
    from project_mai_tai.db.models import Fill
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)
        _seed_managed_row(session)
    svc = _poll_service(sf)
    svc.broker_adapter = _ExitAdapter(_POLL_EXIT)
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))

    await svc._poll_native_oco_exits()

    with sf() as session:
        fills = session.scalars(select(Fill)).all()
    assert len(fills) == 1 and fills[0].side == "sell"
    assert fills[0].price == Decimal("3.93")


@pytest.mark.asyncio
async def test_no_exit_at_the_broker_leaves_the_position_alone() -> None:
    """⛔ THE DANGEROUS DIRECTION. A still-open position must NOT be closed just because the poll
    ran — that would abandon a live position's ladder."""
    from project_mai_tai.db.models import Fill
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)
        _seed_managed_row(session)
    svc = _poll_service(sf)
    svc.broker_adapter = _ExitAdapter(None)          # bracket still working
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))

    await svc._poll_native_oco_exits()

    with sf() as session:
        assert session.scalars(select(Fill)).all() == []
    assert ("live:orb", "BIYA") in svc._managed_v2_symbols


@pytest.mark.asyncio
async def test_the_per_symbol_throttle_limits_broker_calls() -> None:
    """⛔ RATE LIMIT. Webull 429s readily (the exit-fill probe and the 07-24 mirror flood). Polling
    every managed symbol on every ~15s sync must not become a call storm."""
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)
        _seed_managed_row(session)
    svc = _poll_service(sf, min_secs=999.0)
    svc.broker_adapter = _ExitAdapter(None)
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))

    for _ in range(5):
        await svc._poll_native_oco_exits()
    assert svc.broker_adapter.calls == 1              # throttled to one inside the window


@pytest.mark.asyncio
async def test_exit_poll_flag_off_makes_no_broker_call() -> None:
    sf = build_test_session_factory()
    svc = _poll_service(sf, enabled=False)
    svc.broker_adapter = _ExitAdapter(_POLL_EXIT)
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))
    await svc._poll_native_oco_exits()
    assert svc.broker_adapter.calls == 0


@pytest.mark.asyncio
async def test_a_broker_failure_does_not_spin_the_poll() -> None:
    """The clock is stamped BEFORE the call, so a persistently failing symbol is retried once per
    window rather than on every sync."""
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)
        _seed_managed_row(session)          # the poll's work-list is the OPEN ROWS

    class _Boom:
        calls = 0

        async def fetch_oco_exit_fill(self, *a, **k):
            _Boom.calls += 1
            raise RuntimeError("broker down")

    svc = _poll_service(sf, min_secs=999.0)
    svc.broker_adapter = _Boom()
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))
    for _ in range(4):
        await svc._poll_native_oco_exits()            # must not raise
    assert _Boom.calls == 1


@pytest.mark.asyncio
async def test_EVICT_A_KEY_the_poll_still_finds_polls_closes_and_REENROLLS_it() -> None:
    """⛔⭐ THE ACCEPTANCE CRITERION for the phantom-row class (2026-08-03).

    A fix that only works when the in-memory guard is already correct assumes the failure mode away.
    So: leave an OPEN managed row, evict its key from `_managed_v2_symbols` entirely, and prove the
    poll still reaches it — records the exit, closes the row, and REPAIRS the guard.

    This is the shape of all three live phantoms that day (live:orb FUSE 2h17m, live:orb HYFM
    1h41m, live:schwab_1m_v2 HYFM): a filled entry, an OCO bracket emitted, the broker flat, the row
    open, and ZERO miss lines — because the loop body never ran for them at all."""
    from project_mai_tai.db.models import Fill, OmsManagedPosition
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)
        _seed_managed_row(session)
    svc = _poll_service(sf)
    svc.broker_adapter = _ExitAdapter(_POLL_EXIT)

    # THE PHANTOM: the row is open, but the guard has forgotten it.
    svc._managed_v2_symbols.clear()
    assert ("live:orb", "BIYA") not in svc._managed_v2_symbols

    await svc._poll_native_oco_exits()

    with sf() as session:
        fills = session.scalars(select(Fill)).all()
        rows = session.scalars(select(OmsManagedPosition)).all()
    assert len(fills) == 1 and fills[0].side == "sell", "the evicted row was never polled"
    assert all(r.status != "open" for r in rows), "the phantom row was left open"


@pytest.mark.asyncio
async def test_the_poll_work_list_is_the_open_rows_not_the_in_memory_guard() -> None:
    """The guard is the QUOTE hot path and may legitimately hold stale keys; it must not be able to
    manufacture work. A key with no open row must NOT be polled."""
    sf = build_test_session_factory()
    with sf() as session:
        _seed_entry(session)          # entry order only - NO open managed row
    svc = _poll_service(sf)
    svc.broker_adapter = _ExitAdapter(_POLL_EXIT)
    svc._managed_v2_symbols.add(("live:orb", "BIYA"))   # a stale key

    await svc._poll_native_oco_exits()

    assert svc.broker_adapter.calls == 0, "a key with no open row must not reach the broker"


# ---- [VIRTUAL-CLEAR]: a one-way erasure of our own holdings ledger must not be silent ----
#
# ⭐⭐ DSY, live:orb, 2026-08-07 (open item 12). A position we HELD read virtual_quantity = 0 while
# account_positions said 1 and oms_managed_positions had an open row. Two causes were possible:
# the buy fill never reached apply_fill_to_positions, or this clear erased it. ⛔ They are
# INDISTINGUISHABLE after the fact — _apply_position_fill's sell branch writes the same 0/0/NULL.
# The clear ran ~1×/30s and DISCARDED ITS COUNT, so there was no line to read; diagnosis needed an
# elimination argument that only worked because the broker still held the shares.



class _RecordingHandler(logging.Handler):
    """caplog cannot see these lines: `configure_logging` calls `basicConfig(force=True)` when the
    service is constructed, which removes pytest's root handler. Own the sink instead."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _capture_service_log(service, name: str) -> _RecordingHandler:
    handler = _RecordingHandler()
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    service.logger = logger
    return handler


def _seed_virtual(session, *, symbol: str, virtual_qty: str, account_qty: str | None):
    strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
    account = BrokerAccount(name="live:orb", provider="schwab", environment="production")
    session.add_all([strategy, account])
    session.flush()
    session.add(
        VirtualPosition(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol=symbol,
            quantity=Decimal(virtual_qty),
            average_price=Decimal("1.00"),
            realized_pnl=Decimal("0"),
        )
    )
    if account_qty is not None:
        session.add(
            AccountPosition(
                broker_account_id=account.id,
                symbol=symbol,
                quantity=Decimal(account_qty),
                average_price=Decimal("1.00"),
                market_value=Decimal("1.00"),
            )
        )
    session.flush()
    return account.id


def test_virtual_clear_REPORTS_what_it_erased() -> None:
    """⛔ The defect was a discarded count. The return has to carry account, symbol, and the
    quantity BEFORE the write, or the log line cannot name the next occurrence."""
    session_factory = build_test_session_factory()
    with session_factory() as session:
        account_id = _seed_virtual(session, symbol="DSY", virtual_qty="1", account_qty="0")
        session.commit()

    with session_factory() as session:
        cleared = OmsStore().clear_virtual_positions_without_account_backing(session)
        session.commit()

    assert cleared == [(account_id, "DSY", Decimal("1"))]


def test_virtual_clear_never_touches_a_BROKER_BACKED_position() -> None:
    """⭐ THE DSY SHAPE as the broker actually reports it: backed by 1 share. If this ever fails,
    the clear is erasing positions we genuinely hold and the false zero becomes reproducible."""
    session_factory = build_test_session_factory()
    with session_factory() as session:
        _seed_virtual(session, symbol="DSY", virtual_qty="1", account_qty="1")
        session.commit()

    with session_factory() as session:
        cleared = OmsStore().clear_virtual_positions_without_account_backing(session)
        session.commit()
    assert cleared == [], "a broker-backed position was erased"

    with session_factory() as session:
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "DSY"))
        assert position is not None and position.quantity == Decimal("1")


@pytest.mark.asyncio
async def test_sync_broker_state_LOGS_the_symbols_it_zeroed() -> None:
    """⭐ The production call site, not a re-implementation of its format string.

    A virtual row with no broker backing, driven through the real `sync_broker_state`. The line has
    to name the ACCOUNT as well as the symbol — `virtual_positions` is keyed per account and DSY sat
    on the fan-out leg, not on v2."""
    from types import SimpleNamespace

    session_factory = build_test_session_factory()
    with session_factory() as session:
        _seed_virtual(session, symbol="DSY", virtual_qty="1", account_qty="0")
        session.commit()

    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    async def _no_positions(_name):
        return []

    service.broker_adapter = SimpleNamespace(list_account_positions=_no_positions)
    handler = _capture_service_log(service, "test-virtual-clear")

    await service.sync_broker_state()

    lines = [m for m in handler.messages if "[VIRTUAL-CLEAR]" in m]
    assert lines, "the ledger was erased with no log line — the DSY diagnosis stays impossible"
    assert "DSY" in lines[0]
    assert "live:orb" in lines[0], "the account is load-bearing: v2 and the fan-out leg differ"
    assert "=1" in lines[0], "the quantity erased must be in the line"


@pytest.mark.asyncio
async def test_sync_broker_state_stays_QUIET_when_nothing_was_cleared() -> None:
    """~2,800 syncs a day erase nothing. If the line fires on those it is noise, and a noisy line is
    an unread line — the same failure mode as the reconciler's ~3,000 daily criticals."""
    from types import SimpleNamespace

    session_factory = build_test_session_factory()
    with session_factory() as session:
        _seed_virtual(session, symbol="DSY", virtual_qty="0", account_qty="0")
        session.commit()

    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=FakeRedis(),
        session_factory=session_factory,
    )

    async def _no_positions(_name):
        return []

    service.broker_adapter = SimpleNamespace(list_account_positions=_no_positions)
    handler = _capture_service_log(service, "test-virtual-clear-quiet")

    await service.sync_broker_state()

    assert not [m for m in handler.messages if "[VIRTUAL-CLEAR]" in m]
