from decimal import Decimal
import logging
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, VirtualPosition
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.strategy_core.v2_entry_sizing import sized_entry_quantity
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.mark.parametrize(
    ("price", "schwab", "webull"),
    [
        ("1.05", 571, 286),
        ("3.05", 197, 98),
        ("7.16", 84, 42),
        ("13.50", 44, 22),
        ("17.00", 35, 18),
    ],
)
def test_fixed_dollar_rounds_each_leg_at_order_price(price, schwab, webull):
    assert sized_entry_quantity(Decimal("600"), Decimal(price), 2, 1000) == schwab
    assert sized_entry_quantity(Decimal("300"), Decimal(price), 1, 1000) == webull


def test_fixed_dollar_clamps_to_one_and_maximum():
    assert sized_entry_quantity(Decimal("600"), Decimal("0.01"), 2, 1000) == 1000
    assert sized_entry_quantity(Decimal("300"), Decimal("1000"), 1, 1000) == 1


@pytest.mark.parametrize("price", [None, Decimal("0"), Decimal("-1")])
def test_fixed_dollar_refuses_missing_or_nonpositive_price(price):
    with pytest.raises(ValueError, match="entry price"):
        sized_entry_quantity(Decimal("600"), price, 2, 1000)


def test_zero_notional_preserves_legacy_quantity_without_price():
    assert sized_entry_quantity(Decimal("0"), None, 2, 1000) == 2
    assert sized_entry_quantity(Decimal("0"), None, 1, 1000) == 1


def _v2_event(account: str, quantity: int, price: str) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol="TEST",
            side="buy",
            quantity=Decimal(quantity),
            intent_type="open",
            reason="ATR Flip",
            metadata={"order_type": "STOP_LIMIT", "limit_price": price},
        ),
    )


def test_oms_refuses_oversized_v2_buy_before_broker():
    service = object.__new__(OmsRiskService)
    service.settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    service._manual_stop_symbols = set()
    service.logger = logging.getLogger(__name__)
    assert service._evaluate_risk(_v2_event("live:schwab_1m_v2", 200, "5.00")) == (
        False, "v2_entry_notional_cap_exceeded"
    )
    assert service._evaluate_risk(_v2_event("live:orb", 1001, "0.10")) == (
        False, "v2_entry_max_shares_exceeded"
    )


def test_zero_notional_bypasses_new_oms_guard_byte_for_byte():
    service = object.__new__(OmsRiskService)
    service.settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_entry_notional_usd=0,
        strategy_schwab_1m_v2_webull_entry_notional_usd=0,
        strategy_schwab_1m_v2_entry_max_shares=1,
    )
    service._manual_stop_symbols = set()
    service.logger = logging.getLogger(__name__)
    event = _v2_event("legacy:account", 2, "3.05")
    intent = SimpleNamespace(quantity=event.payload.quantity, payload={"metadata": dict(event.payload.metadata)})
    original_metadata = dict(event.payload.metadata)

    assert service._evaluate_risk(event) == (True, "ok")
    assert service._finalize_v2_entry_quantity(event, intent) is None
    assert event.payload.quantity == Decimal("2")
    assert event.payload.metadata == original_metadata
    assert intent.payload["metadata"] == original_metadata


def test_zero_notional_leaves_bot_routed_draft_byte_for_byte():
    bot = SchwabV2BotService(Settings(
        _env_file=None,
        strategy_schwab_1m_v2_entry_notional_usd=0,
        strategy_schwab_1m_v2_webull_entry_notional_usd=0,
    ))
    draft = TradeIntentDraft(
        symbol="TEST", side="buy", intent_type="open", quantity=Decimal("7"),
        reason="ATR Flip", metadata={"path": "ATR Flip", "order_type": "market"},
    )
    before = (draft.quantity, dict(draft.metadata))

    assert bot._apply_extended_hours_routing(draft, datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    assert (draft.quantity, draft.metadata) == before


def test_reprice_sizes_fresh_order_and_cancels_placed_quantity():
    strategy = SchwabV2Strategy(Settings(_env_file=None))
    strategy._resting_session_is_eh = lambda now=None: False
    state = strategy.watchlist_state("TEST")

    strategy._queue_resting_place(state, 3.05)
    first = strategy.drain_pending_intents()[0]
    assert first.quantity == Decimal("196")
    assert first.metadata["limit_price"] == "3.0652"
    assert first.metadata["entry_notional_target_usd"] == "600"

    strategy._queue_resting_cancel(state, reason="reprice")
    cancel = strategy.drain_pending_intents()[0]
    assert cancel.quantity == first.quantity

    strategy._queue_resting_place(state, 2.90)
    second = strategy.drain_pending_intents()[0]
    assert second.quantity == Decimal("206")
    assert second.metadata["limit_price"] == "2.9145"


def test_resting_mirror_uses_its_own_notional_and_cancel_keeps_each_placed_quantity():
    strategy = SchwabV2Strategy(Settings(
        _env_file=None,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
    ))
    strategy._resting_session_is_eh = lambda now=None: False
    state = strategy.watchlist_state("TEST")

    strategy._queue_resting_place(state, 3.05)
    schwab = strategy.drain_pending_intents()[0]
    webull = strategy.drain_webull_direct_intents()[0]
    assert schwab.quantity == Decimal("196")
    assert webull.quantity == Decimal("98")
    assert schwab.metadata["entry_notional_target_usd"] == "600"
    assert webull.metadata["entry_notional_target_usd"] == "300"
    assert webull.metadata["entry_size_price"] == schwab.metadata["entry_size_price"]

    strategy._queue_resting_cancel(state, reason="reprice")
    schwab_cancel = strategy.drain_pending_intents()[0]
    webull_cancel = strategy.drain_webull_direct_intents()[0]
    assert schwab_cancel.quantity == schwab.quantity
    assert webull_cancel.quantity == webull.quantity


def test_partial_schwab_fill_claims_one_independently_sized_webull_leg():
    strategy = SchwabV2Strategy(Settings(
        _env_file=None,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_fanout_on_fill_enabled=True,
    ))
    strategy._resting_session_is_eh = lambda now=None: False
    assert strategy._fanout_on_fill_enabled and strategy._dual_broker_fanout_enabled
    assert strategy._cw_v2_enabled
    state = strategy.watchlist_state("TEST")
    state.fanout_segment_id = 1_790_000_000_000
    state.resting_active = True
    state.resting_level = 3.05
    state.resting_trigger = 3.05
    state.last_resting_placed_slot = "first"
    state.last_quote = SimpleNamespace(ask_price=3.05)

    strategy.update_position("TEST", 197, held_qty=19)
    assert state.fanout_webull_claimed
    webull = strategy.drain_webull_fanout_intents()
    assert len(webull) == 1
    assert webull[0].quantity == Decimal("98")
    assert webull[0].metadata["entry_notional_target_usd"] == "300"
    assert state.cw_resting_taken

    strategy.update_position("TEST", 197, held_qty=19)
    assert strategy.drain_webull_fanout_intents() == []


def test_oms_resizes_at_final_limit_before_submit():
    service = object.__new__(OmsRiskService)
    service.settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    event = _v2_event("live:schwab_1m_v2", 197, "3.05")
    intent = SimpleNamespace(quantity=Decimal("197"), payload={"metadata": {}})

    assert service._finalize_v2_entry_quantity(event, intent) is None
    assert event.payload.quantity == Decimal("197")
    event.payload.metadata["limit_price"] = "2.90"
    assert service._finalize_v2_entry_quantity(event, intent) is None
    assert event.payload.quantity == intent.quantity == Decimal("207")
    assert intent.payload["metadata"]["entry_computed_shares"] == "207"
    assert intent.payload["metadata"]["entry_size_price"] == "2.90"


class _Redis:
    async def xadd(self, _stream, _fields, **_kwargs):
        return "1-0"


class _PartialEntryBroker:
    def __init__(self):
        self.submitted = []
        self.cancelled = []

    async def submit_order(self, request):
        if request.intent_type == "cancel":
            self.cancelled.append(request)
            return [ExecutionReport(
                event_type="cancelled", client_order_id=request.client_order_id,
                broker_order_id="schwab-partial-1", symbol=request.symbol, side="buy",
                intent_type="cancel", quantity=request.quantity,
                filled_quantity=Decimal("19"), reason="cancelled remainder",
                metadata=dict(request.metadata),
            )]
        self.submitted.append(request)
        raw_order = {
            "orderId": "schwab-partial-1", "status": "PARTIALLY_FILLED",
            "quantity": float(request.quantity), "filledQuantity": 19,
            "enteredTime": "2026-10-01T15:00:00Z",
            "orderActivityCollection": [{"executionLegs": [{
                "quantity": 19, "price": 3.04, "time": "2026-10-01T15:00:01Z",
            }]}],
        }
        parser = object.__new__(SchwabBrokerAdapter)
        return [parser._execution_report_from_order(
            request=request, order=raw_order, event_type=parser._map_order_status(raw_order),
            broker_order_id="schwab-partial-1",
        )]


@pytest.mark.asyncio
async def test_large_partial_entry_books_only_filled_shares_and_leaves_remainder_working():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    broker = _PartialEntryBroker()
    service = OmsRiskService(
        settings=Settings(
            _env_file=None, redis_stream_prefix="test", oms_adapter="simulated",
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
        ),
        redis_client=_Redis(), session_factory=sessions, broker_adapter=broker,
    )

    async def no_sync(*, account_names=None):
        return None

    service.sync_broker_state = no_sync
    event = _v2_event("live:schwab_1m_v2", 197, "3.05")
    event.payload.metadata.update({"resting_entry": "true", "stop_price": "3.02"})
    await service.process_trade_intent(event)

    assert len(broker.submitted) == 1
    assert broker.submitted[0].quantity == Decimal("197")
    with sessions() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "TEST"))
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "TEST"))
        assert order is not None and order.status == "partially_filled"
        assert order.quantity == Decimal("197")
        assert position is not None and position.quantity == Decimal("19")

    cancel = TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            symbol="TEST", side="buy", intent_type="cancel", quantity=Decimal("2"),
            reason="resting reprice", metadata={"resting_entry_cancel": "true"},
        ),
    )
    await service.process_trade_intent(cancel)
    assert len(broker.cancelled) == 1
    # A restart can lose strategy-local quantity; OMS cancels the persisted target order.
    assert broker.cancelled[0].quantity == Decimal("197")
    with sessions() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "TEST"))
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "TEST"))
        assert order is not None and order.status == "cancelled"
        assert position is not None and position.quantity == Decimal("19")
