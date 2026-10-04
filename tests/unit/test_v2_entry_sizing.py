from decimal import Decimal
import inspect
import logging
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, VirtualPosition
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.strategy_core.v2_entry_sizing import resting_wire_limit, sized_entry_quantity
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


@pytest.mark.parametrize(
    ("method", "expected_calls"),
    [
        ("_build_hold_draft", 1),
        ("_maybe_atr_emit", 1),
        ("_cw_entry", 1),
        ("_cw_v2_quote", 1),
        ("_queue_resting_place", 2),
        ("_eh_resting_cross_check", 1),
        ("_build_webull_fanout_draft", 1),
    ],
)
def test_all_eight_v2_open_sites_delegate_to_shared_sizing(method, expected_calls):
    source = inspect.getsource(getattr(SchwabV2Strategy, method))
    assert source.count("self._sized_open(") == expected_calls


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


def test_oms_cap_uses_the_webull_wire_tick_and_refuses_missing_price():
    service = object.__new__(OmsRiskService)
    service.settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    service._manual_stop_symbols = set()
    service.logger = logging.getLogger(__name__)
    event = _v2_event("live:orb", 305, "1.2349")
    event.payload.metadata["stop_price"] = "1.2240"
    assert service._v2_entry_wire_price(event) == Decimal("1.23")
    assert service._evaluate_risk(event) == (False, "v2_entry_notional_cap_exceeded")
    event.payload.metadata.pop("limit_price")
    assert service._evaluate_risk(event) == (False, "v2_entry_price_unavailable")


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
    strategy = SchwabV2Strategy(Settings(_env_file=None, strategy_schwab_1m_v2_atr_reprice_handoff_enabled=True))
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
    assert strategy.drain_pending_intents() == []  # RPG owns the replacement now.
    # Current-price sizing through the actual OMS callback is covered by
    # test_rpg1_runtime::test_replacement_uses_current_dollar_size_and_preserves_economic_slot.


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
    assert schwab.metadata["entry_size_price"] == "3.0652"
    assert webull.metadata["entry_size_price"] == "3.07"

    strategy._queue_resting_cancel(state, reason="reprice")
    schwab_cancel = strategy.drain_pending_intents()[0]
    webull_cancel = strategy.drain_webull_direct_intents()[0]
    assert schwab_cancel.quantity == schwab.quantity
    assert webull_cancel.quantity == webull.quantity


def test_native_resting_cancel_uses_quantity_at_final_schwab_wire_limit(monkeypatch):
    monkeypatch.setattr("project_mai_tai.oms.service._is_regular_market_session", lambda now=None: True)
    settings = Settings(
        _env_file=None, oms_v2_emit_native_oco_bracket_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    strategy = SchwabV2Strategy(settings)
    strategy._resting_session_is_eh = lambda now=None: False
    state = strategy.watchlist_state("TEST")
    strategy._queue_resting_place(state, 3.05)
    placed = strategy.drain_pending_intents()[0]
    assert placed.metadata["limit_price"] == "3.0652"
    assert placed.metadata["entry_size_price"] == "3.07"
    assert placed.quantity == Decimal("195")

    service = object.__new__(OmsRiskService)
    service.settings = settings
    service.logger = logging.getLogger(__name__)
    service._cw_target_pct = 5.0
    service._cw_stop_pct = 8.0
    event = _v2_event("live:schwab_1m_v2", 195, "3.0652")
    event.payload.metadata.update(placed.metadata)
    service._apply_v2_oco_bracket_entry(event=event)
    intent = SimpleNamespace(quantity=Decimal("195"), payload={"metadata": {}})
    assert service._finalize_v2_entry_quantity(event, intent) is None
    assert event.payload.metadata["limit_price"] == "3.07"
    assert event.payload.quantity == Decimal("195")

    strategy._queue_resting_cancel(state, reason="reprice")
    assert strategy.drain_pending_intents()[0].quantity == intent.quantity


def test_resting_wire_limit_matches_webull_adapter_on_collapsed_band():
    stop = Decimal("1.2166")
    limit = Decimal("1.2227")
    request = SimpleNamespace(side="buy", metadata={})
    adapter_limit, _, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(
        request=request, order_type="STOP_LIMIT", limit_price=limit, stop_price=stop,
    )
    assert refusal is None
    assert resting_wire_limit(stop, limit, leg="webull") == adapter_limit == Decimal("1.23")
    assert resting_wire_limit(stop, limit, leg="schwab", native_schwab_bracket=True) == Decimal("1.23")


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
async def test_v2_dollar_open_refuses_unconfigured_account_before_broker():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    broker = _PartialEntryBroker()
    service = OmsRiskService(
        settings=Settings(
            _env_file=None, redis_stream_prefix="test", oms_adapter="simulated",
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
            strategy_schwab_1m_v2_entry_notional_usd=600,
            strategy_schwab_1m_v2_webull_entry_notional_usd=300,
        ),
        redis_client=_Redis(),
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        broker_adapter=broker,
    )
    event = _v2_event("live:unconfigured", 2, "3.05")
    intent = SimpleNamespace(quantity=event.payload.quantity, payload={"metadata": {}})

    assert service._evaluate_risk(event) == (False, "v2_entry_account_unknown")
    assert service._finalize_v2_entry_quantity(event, intent) == "v2_entry_account_unknown"
    outcomes = await service.process_trade_intent(event)
    assert outcomes[0].payload.status == "rejected"
    assert outcomes[0].payload.reason == "risk_rejected"
    assert broker.submitted == []


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

    strategy = SchwabV2Strategy(Settings(_env_file=None, strategy_schwab_1m_v2_atr_reprice_handoff_enabled=True))
    strategy._resting_session_is_eh = lambda now=None: False
    state = strategy.watchlist_state("TEST")
    strategy._queue_resting_place(state, 3.05)
    first = strategy.drain_pending_intents()[0]
    strategy._queue_resting_cancel(state, reason="reprice")
    assert strategy.drain_pending_intents()[0].quantity == first.quantity
    strategy._queue_resting_place(state, 2.90)
    assert strategy.drain_pending_intents() == []  # Never mint a bar-driven remainder BUY.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schwab_notional", "expected_replacement"),
    [(600, Decimal("188")), (0, Decimal("178"))],
)
async def test_oms_refresh_recomputes_dollar_size_net_of_prior_fills_but_legacy_stays_fixed(
    schwab_notional, expected_replacement,
):
    class Broker:
        def __init__(self):
            self.requests = []

        async def submit_order(self, request):
            self.requests.append(request)
            return [ExecutionReport(
                event_type="cancelled" if request.intent_type == "cancel" else "accepted",
                client_order_id=request.client_order_id,
                broker_order_id="old-order" if request.intent_type == "cancel" else "new-order",
                symbol=request.symbol, side="buy", intent_type=request.intent_type,
                quantity=request.quantity, filled_quantity=Decimal("19") if request.intent_type == "cancel" else Decimal("0"),
                origin="broker",
            )]

    service = object.__new__(OmsRiskService)
    service.settings = Settings(
        _env_file=None, strategy_schwab_1m_v2_entry_notional_usd=schwab_notional,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
    )
    service.logger = logging.getLogger(__name__)
    service.broker_adapter = Broker()

    async def refreshed(**_kwargs):
        return {"order_type": "limit", "limit_price": "2.90"}

    async def recorded(**_kwargs):
        return []

    service._build_refreshed_order_metadata = refreshed
    service._direct_cancel_dead_target_bound_reached = lambda *_args, **_kwargs: False
    service._record_direct_cancel_reports = lambda _session, **kwargs: kwargs["reports"][0]
    service._replacement_client_order_id = lambda coid: f"{coid}-replacement"
    service._record_order_reports = recorded
    order = SimpleNamespace(
        client_order_id="old-order", broker_order_id="old-order", symbol="TEST",
        side="buy", quantity=Decimal("197"), order_type="limit", time_in_force="day",
        strategy_id=None, broker_account_id=None, payload={"order_type": "limit", "limit_price": "3.05"},
    )
    intent = SimpleNamespace(intent_type="open", reason="ATR Flip")
    partial = ExecutionReport(
        event_type="partially_filled", client_order_id="old-order", broker_order_id="old-order",
        symbol="TEST", side="buy", intent_type="open", quantity=Decimal("197"),
        filled_quantity=Decimal("19"), fill_price=Decimal("3.04"), origin="broker",
    )

    result = await service._refresh_working_order(
        session=None, order=order, intent=intent, strategy_code="schwab_1m_v2",
        broker_account_name="live:schwab_1m_v2", report=partial,
    )
    assert result["orders"] == 1
    cancel, replacement = service.broker_adapter.requests
    assert cancel.quantity == Decimal("178")
    assert replacement.quantity == expected_replacement
    if schwab_notional:
        assert replacement.metadata["entry_total_target_shares"] == "207"
        assert replacement.metadata["entry_prior_filled_shares"] == "19"
        assert replacement.metadata["entry_size_price"] == "2.90"
        assert replacement.quantity * Decimal("2.90") <= Decimal("600") * Decimal("1.25")
    else:
        assert "entry_total_target_shares" not in replacement.metadata
