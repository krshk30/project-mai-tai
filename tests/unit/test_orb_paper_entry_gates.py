from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from project_mai_tai.orb_paper_store import (
    ORB_PAPER_ENTRY_GATE_EVENT_TYPE,
    ORB_PAPER_EVENT_TYPE,
    ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
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


def _service(
    *,
    atr_gate: bool = False,
    red_delay: bool = False,
    store: _Store | None = None,
) -> OrbService:
    service = OrbService(
        settings=Settings(
            orb_running_high_enabled=True,
            orb_resting_entry_enabled=True,
            orb_paper_atr_entry_gate_enabled=atr_gate,
            orb_paper_four_red_delay_enabled=red_delay,
        ),
        redis_client=MagicMock(),
        paper_store=store,
    )
    service._universe = {"FOO"}
    service._last_gateway_symbols = ["FOO"]
    return service


def _bar(
    service: OrbService,
    minute: int,
    *,
    open_price: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
) -> OrbBar:
    return OrbBar(
        timestamp=service._session_open_utc() + timedelta(minutes=minute),
        open=open_price,
        high=max(open_price, close) if high is None else high,
        low=min(open_price, close) if low is None else low,
        close=close,
        volume=100.0,
    )


def _seed_opening(
    service: OrbService,
    *,
    red_count: int,
    level: float = 10.5,
) -> None:
    for offset, minute in enumerate(range(-5, 0)):
        if offset < red_count:
            bar = _bar(
                service,
                minute,
                open_price=10.4,
                close=10.0,
                high=level if offset == 0 else 10.4,
                low=9.9,
            )
        else:
            bar = _bar(
                service,
                minute,
                open_price=10.0,
                close=10.2,
                high=level if offset == 0 else 10.3,
                low=9.9,
            )
        service._on_bar(service._last_gateway_symbols[0], bar)


def _seed_atr_history(service: OrbService, *, close: float = 10.0) -> None:
    for minute in range(-14, -5):
        service._on_bar(
            "FOO",
            _bar(
                service,
                minute,
                open_price=close,
                close=close,
                high=close + 0.05,
                low=close - 0.05,
            ),
        )


def test_shipped_entry_gate_defaults_are_independently_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAI_TAI_ORB_PAPER_ATR_ENTRY_GATE_ENABLED", raising=False)
    monkeypatch.delenv("MAI_TAI_ORB_PAPER_FOUR_RED_DELAY_ENABLED", raising=False)

    settings = Settings(_env_file=None)

    assert settings.orb_paper_atr_entry_gate_enabled is False
    assert settings.orb_paper_four_red_delay_enabled is False


def test_dark_entry_gates_preserve_the_existing_early_resting_fill() -> None:
    service = _service()
    _seed_opening(service, red_count=5)
    state = service._states["FOO"]
    state.atr_state = "short"
    open_at = service._session_open_utc()

    service._check_fixed_resting_fill("FOO", 10.6, open_at + timedelta(seconds=1))

    assert state.opening_red_count == 5
    assert state.resting_order is not None
    assert state.resting_order.filled_at == open_at + timedelta(seconds=1)
    assert [item.event_type for item in service._pending_paper_entries] == [
        ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
        ORB_PAPER_EVENT_TYPE,
    ]


def test_atr_purple_pulls_then_cyan_rearms_without_a_backfilled_fill() -> None:
    service = _service(atr_gate=True)
    _seed_atr_history(service)
    for minute in range(-5, 0):
        service._on_bar(
            "FOO",
            _bar(
                service,
                minute,
                open_price=8.0,
                close=8.0,
                high=8.05,
                low=7.95,
            ),
        )

    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None
    assert state.atr_state == "short"
    assert order.entry_gate_armed is False
    assert order.entry_gate_reason == "ATR_PURPLE_ORDER_PULLED"

    open_at = service._session_open_utc()
    service._check_fixed_resting_fill("FOO", 9.0, open_at + timedelta(seconds=10))
    assert order.filled_at is None
    assert state.entry_break_evaluations == 1
    assert state.entry_breaks_held == 1

    rearm_at = open_at + timedelta(minutes=1)
    service._on_bar(
        "FOO",
        _bar(
            service,
            0,
            open_price=8.0,
            close=11.0,
            high=11.05,
            low=7.95,
        ),
        observed_at=rearm_at,
        observed_price=11.10,
    )
    assert state.atr_state == "long"
    assert order.entry_gate_armed is True
    assert order.current_level == 11.05
    assert order.fresh_cross_ready is False

    service._check_fixed_resting_fill("FOO", 11.10, rearm_at)
    service._check_fixed_resting_fill("FOO", 11.20, rearm_at + timedelta(seconds=1))
    assert order.filled_at is None

    service._check_fixed_resting_fill("FOO", 11.00, rearm_at + timedelta(seconds=2))
    service._check_fixed_resting_fill("FOO", 11.10, rearm_at + timedelta(seconds=3))
    assert order.filled_at == rearm_at + timedelta(seconds=3)
    assert order.fill_price == 11.05


def test_unknown_atr_fails_closed_until_a_completed_bar_produces_a_state() -> None:
    service = _service(atr_gate=True)
    _seed_opening(service, red_count=0)
    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None

    assert state.atr_state is None
    assert order.entry_gate_armed is False
    assert order.entry_gate_reason == "ATR_STATE_UNANSWERABLE_ORDER_WITHHELD"
    assert state.entry_gate_atr_unanswerable == 1

    for minute in range(0, 4):
        observed_at = service._session_open_utc() + timedelta(minutes=minute + 1)
        service._on_bar(
            "FOO",
            _bar(
                service,
                minute,
                open_price=10.2,
                close=10.2,
                high=10.3,
                low=10.1,
            ),
            observed_at=observed_at,
            observed_price=10.2,
        )

    assert state.atr_state == "long"
    assert order.entry_gate_armed is True


def test_atr_long_at_placement_keeps_the_early_0930_fill_available() -> None:
    service = _service(atr_gate=True)
    _seed_atr_history(service)
    _seed_opening(service, red_count=0)
    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None
    assert state.atr_state == "long"
    assert order.entry_gate_armed is True

    fill_at = service._session_open_utc() + timedelta(seconds=1)
    service._check_fixed_resting_fill("FOO", 10.6, fill_at)

    assert order.filled_at == fill_at
    assert order.fill_price == 10.5


def test_four_red_bars_delay_only_the_first_minute_then_require_a_fresh_break() -> None:
    service = _service(red_delay=True)
    _seed_opening(service, red_count=4)
    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None
    assert state.opening_red_count == 4
    assert order.entry_gate_armed is False
    assert order.entry_gate_reason == "FOUR_OF_FIVE_RED_FIRST_MINUTE_DELAY"

    open_at = service._session_open_utc()
    service._check_fixed_resting_fill("FOO", 10.6, open_at + timedelta(seconds=30))
    assert order.filled_at is None

    rearm_at = open_at + timedelta(minutes=1)
    service._evaluate_fixed_entry_gates(
        "FOO",
        evaluated_at=rearm_at,
        observed_price=10.6,
        bar_at=None,
        record_unchanged=False,
    )
    assert order.entry_gate_armed is True
    assert order.fresh_cross_ready is False

    service._check_fixed_resting_fill("FOO", 10.7, rearm_at + timedelta(seconds=1))
    assert order.filled_at is None
    service._check_fixed_resting_fill("FOO", 10.4, rearm_at + timedelta(seconds=2))
    service._check_fixed_resting_fill("FOO", 10.6, rearm_at + timedelta(seconds=3))
    assert order.filled_at == rearm_at + timedelta(seconds=3)


def test_three_red_bars_do_not_delay_and_the_0930_bar_is_not_counted() -> None:
    service = _service(red_delay=True)
    _seed_opening(service, red_count=3)
    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None
    assert state.opening_red_count == 3
    assert order.entry_gate_armed is True

    service._on_bar(
        "FOO",
        _bar(
            service,
            0,
            open_price=11.0,
            close=10.0,
            high=11.0,
            low=9.9,
        ),
        observed_at=service._session_open_utc() + timedelta(minutes=1),
        observed_price=10.0,
    )

    assert state.opening_red_count == 3
    assert order.entry_gate_armed is True


def test_completed_atr_flip_pulls_before_the_rollover_trade_can_fill() -> None:
    service = _service(atr_gate=True)
    _seed_atr_history(service)
    _seed_opening(service, red_count=0, level=10.5)
    state = service._states["FOO"]
    order = state.resting_order
    assert order is not None and order.entry_gate_armed is True

    open_at = service._session_open_utc()
    aggregator = OrbTickAggregator(session_open=service._observe_open_utc())
    aggregator.add_tick(open_at, 10.2, 100)
    aggregator.add_tick(open_at + timedelta(seconds=30), 8.0, 100)
    service._aggregators["FOO"] = aggregator
    observed_at = open_at + timedelta(minutes=1)
    service._handle_market_data(
        {
            "data": json.dumps(
                {
                    "event_type": "trade_tick",
                    "payload": {
                        "symbol": "FOO",
                        "price": "10.6",
                        "size": 100,
                        "timestamp_ns": int(observed_at.timestamp() * 1_000_000_000),
                    },
                }
            )
        }
    )

    assert state.atr_state == "short"
    assert order.entry_gate_armed is False
    assert order.filled_at is None


def test_entry_gate_heartbeat_carries_decisions_and_denominators() -> None:
    service = _service(atr_gate=True, red_delay=True)
    _seed_opening(service, red_count=4)

    health = service._build_heartbeat_payload().data_health["paper_entry_gates"]

    assert health["status"] == "ACTIVE"
    assert health["atr_live_gate_enabled"] is True
    assert health["four_red_first_minute_delay_enabled"] is True
    assert health["evaluations"] == 1
    assert health["initial_withholds"] == 1
    assert health["atr_unanswerable"] == 1
    assert health["red_delayed_name_days"] == 1
    assert health["denominator"] == health["evaluations"]
    assert "09:31 ET" in health["rules"]["four_red"]
    assert "no retroactive fill" in health["rules"]["rearm"]
    event = service._pending_paper_entries[-1]
    assert event.event_type == ORB_PAPER_ENTRY_GATE_EVENT_TYPE
    assert event.detail["check_kind"] == "live"
    assert event.detail["opening_red_count"] == 4
    assert event.detail["atr_state"] == "UNANSWERABLE"


def test_a_pulled_gate_decision_is_durable_and_visible_on_the_paper_screen() -> None:
    store = _Store()
    service = _service(red_delay=True, store=store)
    _seed_opening(service, red_count=4)

    asyncio.run(service._record_pending_paper_entries())

    assert len(store.decisions) == 1
    assert store.decisions[0].event_type == ORB_PAPER_ENTRY_GATE_EVENT_TYPE
    row = service._build_heartbeat_payload().recent_decisions[0]
    assert row["status"] == "withhold"
    assert row["reason"] == "FOUR_OF_FIVE_RED_FIRST_MINUTE_DELAY"
    assert row["entry_gate_armed"] is False
    assert row["check_kind"] == "live"


def test_entry_gates_refuse_to_run_without_the_fixed_resting_model() -> None:
    with pytest.raises(RuntimeError, match="fixed resting entry model"):
        OrbService(
            settings=Settings(orb_paper_atr_entry_gate_enabled=True),
            redis_client=MagicMock(),
        )
