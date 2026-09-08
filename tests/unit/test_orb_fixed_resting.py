from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import MagicMock

from project_mai_tai.orb_paper_store import (
    ORB_PAPER_EVENT_TYPE,
    ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
    ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE,
    ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
    ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE,
)
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator


class _Store:
    def __init__(self) -> None:
        self.decisions = []

    def append(self, decision) -> bool:
        self.decisions.append(decision)
        return True


def _service(*, store=None) -> OrbService:
    service = OrbService(
        settings=Settings(
            orb_running_high_enabled=True,
            orb_resting_entry_enabled=True,
        ),
        redis_client=MagicMock(),
        paper_store=store,
    )
    service._universe = {"FOO"}
    service._last_gateway_symbols = ["FOO"]
    return service


def _bar(service: OrbService, minute: int, *, high: float, close: float | None = None) -> OrbBar:
    value = high if close is None else close
    return OrbBar(
        timestamp=service._session_open_utc() + timedelta(minutes=minute),
        open=value,
        high=high,
        low=value,
        close=value,
        volume=100.0,
    )


def _seed_initial_level(service: OrbService) -> None:
    highs = (10.10, 10.20, 10.50, 10.30, 10.40)
    for minute, high in zip(range(-5, 0), highs, strict=True):
        service._on_bar("FOO", _bar(service, minute, high=high))


def test_places_one_order_at_0929_high_at_092959() -> None:
    service = _service()

    _seed_initial_level(service)

    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None
    assert order.initial_level == 10.50
    assert order.current_level == 10.50
    assert order.order_id.endswith(":FOO")
    assert order.placed_at == service._session_open_utc() - timedelta(seconds=1)
    assert order.source_minutes == ("09:25", "09:26", "09:27", "09:28", "09:29")
    assert [item.event_type for item in service._pending_paper_entries] == [
        ORB_PAPER_ORDER_PLACED_EVENT_TYPE
    ]
    detail = service._pending_paper_entries[0].detail
    assert detail["check_kind"] == "day_gate"
    assert detail["modeled_order_id"] == order.order_id
    assert detail["level_derivation"] == "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET"
    assert detail["fill_assumption"].startswith("MODELED_AT_RESTING_LEVEL")


def test_intrabar_0930_break_fills_at_0929_level_and_never_adjusts() -> None:
    service = _service()
    _seed_initial_level(service)
    open_at = service._session_open_utc()

    service._check_fixed_resting_fill("FOO", 10.80, open_at + timedelta(seconds=5))
    service._on_bar(
        "FOO",
        _bar(service, 0, high=11.00, close=10.90),
        observed_at=open_at + timedelta(minutes=1),
        observed_price=10.90,
    )

    order = service._states["FOO"].resting_order
    assert order is not None
    assert order.fill_price == 10.50
    assert order.filled_at == open_at + timedelta(seconds=5)
    assert order.final_level == 11.00
    assert order.adjusted_at is None
    assert order.adjustment_outcome == "NOT_APPLICABLE_FILLED_BEFORE_09:30_CLOSE"
    events = [item.event_type for item in service._pending_paper_entries]
    assert events == [
        ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
        ORB_PAPER_EVENT_TYPE,
        ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
    ]
    assert service._pending_paper_entries[1].detail["check_kind"] == "live"
    assert service._pending_paper_entries[2].detail["check_kind"] == "post-fill"
    assert {
        item.detail["modeled_order_id"] for item in service._pending_paper_entries
    } == {order.order_id}


def test_real_tick_path_fills_during_0930_without_waiting_for_bar_close() -> None:
    service = _service()
    open_at = service._session_open_utc()

    for minute, price in zip(
        range(-5, 1),
        (10.10, 10.20, 10.50, 10.30, 10.40, 10.80),
        strict=True,
    ):
        at = open_at + timedelta(minutes=minute, seconds=1)
        service._handle_market_data(
            {
                "data": json.dumps(
                    {
                        "event_type": "trade_tick",
                        "payload": {
                            "symbol": "FOO",
                            "price": str(price),
                            "size": 100,
                            "timestamp_ns": int(at.timestamp() * 1_000_000_000),
                        },
                    }
                )
            }
        )

    order = service._states["FOO"].resting_order
    assert order is not None
    assert order.placed_at == open_at - timedelta(seconds=1)
    assert order.fill_price == 10.50
    assert order.filled_at == open_at + timedelta(seconds=1)
    assert service._aggregators["FOO"]._bucket == open_at


def test_0930_high_is_included_and_adjustment_is_modeled_only_when_proven_timely() -> None:
    service = _service()
    _seed_initial_level(service)
    open_at = service._session_open_utc()
    state = service._states["FOO"]
    state.latest_bid = 10.60
    state.latest_ask = 10.70
    state.latest_quote_at = open_at + timedelta(minutes=1, milliseconds=100)

    service._on_bar(
        "FOO",
        _bar(service, 0, high=10.80, close=10.60),
        observed_at=state.latest_quote_at,
        observed_price=10.70,
    )

    order = state.resting_order
    assert order is not None
    assert order.final_level == 10.80
    assert order.current_level == 10.80
    assert order.adjusted_at == open_at + timedelta(seconds=59)
    assert order.adjustment_outcome == "MODELED_ADJUSTMENT_LANDED"
    assert state.adjustment_opportunities == 1
    assert service._pending_paper_entries[-1].event_type == ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE


def test_unproven_adjustment_is_recorded_and_neither_left_nor_pulled() -> None:
    service = _service()
    _seed_initial_level(service)
    open_at = service._session_open_utc()
    state = service._states["FOO"]
    state.latest_bid = 10.84
    state.latest_ask = 10.86
    state.latest_quote_at = open_at + timedelta(minutes=1, milliseconds=100)

    service._on_bar(
        "FOO",
        _bar(service, 0, high=10.80, close=10.70),
        observed_at=state.latest_quote_at,
        observed_price=10.85,
    )

    order = state.resting_order
    assert order is not None
    assert order.current_level == 10.50
    assert order.adjusted_at is None
    assert order.decision_blocked is True
    assert state.adjustment_unanswerable == 1
    event = service._pending_paper_entries[-1]
    assert event.event_type == ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE
    assert event.detail["status"] == "UNANSWERABLE"
    assert event.detail["reason"] == "HIGHER_09:30_HIGH_BUT_ADJUSTMENT_NOT_PROVEN_IN_TIME"

    service._check_fixed_resting_fill("FOO", 12.00, open_at + timedelta(minutes=2))
    assert order.filled_at is None, "an unresolved leave-or-pull decision must not pick a default"


def test_no_0930_trade_bar_finalizes_the_level_instead_of_hanging() -> None:
    service = _service()
    _seed_initial_level(service)
    observed_at = service._session_open_utc() + timedelta(minutes=1, seconds=1)

    service._finalize_fixed_resting_without_0930_bar("FOO", observed_at=observed_at)

    order = service._states["FOO"].resting_order
    assert order is not None
    assert order.final_level == 10.50
    assert order.adjustment_outcome == "NOT_NEEDED_NO_09:30_TRADE_HIGH"
    event = service._pending_paper_entries[-1]
    assert event.event_type == ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE
    assert event.detail["reason"] == "NO_09:30_TRADE_BAR_LEVEL_UNCHANGED"
    assert event.detail["decision_observed_at"] == observed_at.isoformat()


def test_quote_tick_closes_the_bar_and_is_recorded_as_adjustment_evidence() -> None:
    service = _service()
    open_at = service._session_open_utc()
    agg = service._aggregators.setdefault(
        "FOO", OrbTickAggregator(session_open=service._observe_open_utc())
    )
    for minute, high in zip(range(-5, 0), (10.10, 10.20, 10.50, 10.30, 10.40), strict=True):
        completed = agg.add_tick(open_at + timedelta(minutes=minute), high, 100)
        if completed is not None:
            service._on_bar("FOO", completed)

    quote_at = open_at + timedelta(milliseconds=100)
    service._handle_market_data(
        {
            "data": json.dumps(
                {
                    "event_type": "quote_tick",
                    "produced_at": quote_at.isoformat(),
                    "payload": {"symbol": "FOO", "bid_price": "10.39", "ask_price": "10.41"},
                }
            )
        }
    )

    state = service._states["FOO"]
    assert state.latest_quote_at == quote_at
    assert state.latest_bid == 10.39
    assert state.latest_ask == 10.41
    assert state.resting_order is not None
    assert state.resting_order.placed_at == open_at - timedelta(seconds=1)


def test_all_modeled_decisions_are_durable_and_entry_count_only_tracks_fill() -> None:
    store = _Store()
    service = _service(store=store)
    _seed_initial_level(service)
    open_at = service._session_open_utc()
    service._check_fixed_resting_fill("FOO", 10.60, open_at + timedelta(seconds=1))
    service._on_bar(
        "FOO",
        _bar(service, 0, high=10.70),
        observed_at=open_at + timedelta(minutes=1),
        observed_price=10.60,
    )

    asyncio.run(service._record_pending_paper_entries())

    assert [item.event_type for item in store.decisions] == [
        ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
        ORB_PAPER_EVENT_TYPE,
        ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
    ]
    assert service._states["FOO"].paper_entries == 1
    assert all(item.detail["metadata"]["broker_route"] == "none" for item in store.decisions)


def test_adjustment_metric_uses_real_denominator_and_zero_is_unexercised() -> None:
    service = _service()
    payload = service._build_heartbeat_payload()
    assert payload.data_health["resting_adjustment_timing"] == {
        "status": "UNEXERCISED",
        "filled_before_adjustment": 0,
        "modeled_adjustments_landed": 0,
        "unanswerable": 0,
        "denominator": 0,
    }

    _seed_initial_level(service)
    state = service._states["FOO"]
    state.adjustment_opportunities = 1
    state.adjustment_unanswerable = 1
    payload = service._build_heartbeat_payload()
    assert payload.data_health["resting_adjustment_timing"] == {
        "status": "MEASURED",
        "filled_before_adjustment": 0,
        "modeled_adjustments_landed": 0,
        "unanswerable": 1,
        "denominator": 1,
    }
