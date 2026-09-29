import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar


OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)


def _bar(index: int, level: float) -> OrbBar:
    return OrbBar(
        timestamp=OPEN - timedelta(minutes=5 - index),
        open=level,
        high=level,
        low=level,
        close=level,
        volume=200,
        breakout_high=level,
    )


def test_live_service_emits_one_open_two_reprices_and_no_paper_order(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    processing_at = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: processing_at[0])
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index, level in enumerate((5.3, 5.4, 5.5, 5.6, 5.7)):
        bar = _bar(index, level)
        processing_at[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
        asyncio.run(service._process_closed_bars())
    assert [event.payload.reason for event in emitted] == [
        "ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT",
        "ORB_RAISE_EXISTING_SCHWAB_STOP_LIMIT",
        "ORB_RAISE_EXISTING_SCHWAB_STOP_LIMIT",
    ]
    assert [event.payload.intent_type for event in emitted] == ["open", "cancel", "cancel"]
    assert [event.payload.metadata["stop_price"] for event in emitted] == [
        "5.50", "5.60", "5.70"
    ]
    assert service._states == {}


def test_negative_completed_schwab_bar_requests_cancel_not_reprice(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    processing_at = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: processing_at[0])
    decisions = iter(((True, "nonnegative", 0.1), (False, "negative", -0.1)))
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: next(decisions),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index in range(4):
        bar = _bar(index, 5.3 + index * 0.1)
        processing_at[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
        asyncio.run(service._process_closed_bars())
    assert [event.payload.intent_type for event in emitted] == ["open", "cancel"]
    assert emitted[-1].payload.metadata == {"orb_schwab_cancel": "true"}


def test_on_time_market_timestamp_does_not_hide_late_processing(monkeypatch) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index in range(3):
        bar = _bar(index, 5.3 + index * 0.1)
        service._on_bar("CLRO", bar, observed_at=bar.timestamp + timedelta(minutes=1))
    monkeypatch.setattr(service, "_processing_time", lambda: OPEN - timedelta(minutes=1, seconds=53))
    asyncio.run(service._process_closed_bars())
    assert emitted == []


class _ReadOnlyRedis:
    async def xadd(self, *_args, **_kwargs):
        pytest.fail("Observation must not write intents or gateway subscriptions")


def _observer(monkeypatch):
    service = OrbSchwabService(
        settings=Settings(orb_enabled=True, orb_schwab_observe_enabled=True),
        redis_client=_ReadOnlyRedis(),
        session_factory=lambda: None,
    )
    service._universe = {"CLRO"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    clock = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (True, "nonnegative", 0.1),
    )

    async def no_publish(*_args, **_kwargs):
        pytest.fail("An observer must not even call the order publisher")

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", no_publish
    )
    return service, clock


def _observe_bars(service, clock, count=5):
    for index in range(count):
        bar = _bar(index, 5.3 + index * 0.1)
        clock[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar, observed_at=clock[0])
        asyncio.run(service._process_closed_bars())


def _observations(caplog):
    return [
        json.loads(record.message.split("[ORB-SCHWAB-OBSERVE] ", 1)[1])
        for record in caplog.records if "[ORB-SCHWAB-OBSERVE] " in record.message
    ]


def _trade(at, price, symbol="CLRO"):
    return {"data": json.dumps({
        "event_type": "trade_tick", "produced_at": at.isoformat(),
        "payload": {"symbol": symbol, "price": price, "size": 100,
                    "timestamp_ns": int(at.timestamp()) * 1_000_000_000},
    })}


@pytest.mark.parametrize("flag", ["orb_paper_atr_entry_gate_enabled", "orb_paper_four_red_delay_enabled"])
def test_inactive_paper_entry_filters_cannot_be_silently_enabled_on_live_route(flag):
    with pytest.raises(ValueError, match="Optional paper entry gates"):
        OrbSchwabService(settings=Settings(orb_schwab_observe_enabled=True,
                                           orb_running_high_enabled=True,
                                           orb_resting_entry_enabled=True, **{flag: True}))


def test_observer_exit_is_conditional_and_never_writes_or_publishes(monkeypatch, caplog):
    from project_mai_tai.orb_schwab_exits import BODY_REASON

    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    clock[0] = OPEN - timedelta(minutes=5)
    asyncio.run(service._sync_gateway_subscription(["CLRO"]))
    _observe_bars(service, clock)
    # A high/low swing followed by an in-cap cross gives a small forming body.
    for seconds, price in ((0, 5.69), (2, 5.60), (5, 5.50), (10, 5.70)):
        clock[0] = OPEN + timedelta(seconds=seconds)
        service._handle_market_data(_trade(clock[0], price))
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_completed_atr_bars",
                        lambda *_args: ([], "missing_last_closed_schwab_bar"))
    asyncio.run(service._process_strategy_exits(clock[0]))
    rows = [row for row in _observations(caplog) if row["kind"] == "conditional_exit"]
    assert len(rows) == 1 and rows[0]["reason"] == BODY_REASON
    assert rows[0]["fill_status"] == "UNMEASURED"
    assert rows[0]["broker_orders_sent"] == 0
    assert "NOT_A_BROKER_FILL" in rows[0]["assumption"]


def test_observer_records_same_three_actions_with_sending_off(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    rows = [row for row in _observations(caplog) if row["kind"] == "decision"]
    assert [row["proposed_action"] for row in rows] == ["place", "reprice", "reprice"]
    assert [row["prices"]["stop_price"] for row in rows] == ["5.50", "5.60", "5.70"]
    assert [row["range_bars_seen"] for row in rows] == [3, 4, 5]
    assert all(row["fill_status"] == "UNMEASURED" and row["broker_orders_sent"] == 0 for row in rows)
    assert not service.settings.orb_live_schwab_orders_enabled
    assert service._states == {} and service._pending_paper_entries == []


def test_observer_subscription_and_shutdown_do_not_change_gateway_owners(monkeypatch):
    service, _clock = _observer(monkeypatch)
    asyncio.run(service._sync_gateway_subscription(["CLRO", "CLRO"]))
    assert service._last_gateway_symbols == ["CLRO"]
    asyncio.run(service._sync_gateway_subscription([]))
    assert service._last_gateway_symbols == []


def test_observer_runs_with_live_flag_off_and_cleans_up_read_only(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    clock[0] = OPEN - timedelta(minutes=1)
    monkeypatch.setattr(service, "_maybe_roll_session", lambda: None)
    monkeypatch.setattr(service, "_refresh_universe", lambda: ["CLRO"])
    iterations = []

    async def drain():
        if iterations:
            raise asyncio.CancelledError
        iterations.append(True)
        return 1

    monkeypatch.setattr(service, "_drain_market_data", drain)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run())
    assert iterations == [True]
    assert service._last_gateway_symbols == []
    assert "mode=OBSERVE_ONLY live_sending=False" in caplog.text
    coverage = next(row for row in _observations(caplog) if row["kind"] == "coverage")
    assert len(coverage["missing_closed_range_minutes"]["CLRO"]) == 4
    assert coverage["proposed_orders"] == 0


def test_live_and_observation_flags_cannot_be_combined():
    with pytest.raises(ValueError, match="cannot be enabled together"):
        OrbSchwabService(settings=Settings(
            orb_enabled=True, orb_schwab_observe_enabled=True,
            orb_live_schwab_orders_enabled=True,
        ))


def test_both_modes_off_does_not_start_or_touch_database():
    service = OrbSchwabService(settings=Settings(orb_enabled=True), redis_client=_ReadOnlyRedis())
    asyncio.run(service.run())
    assert service.session_factory is None


@pytest.mark.parametrize("enabled", [False, True])
def test_observer_still_respects_master_orb_off(monkeypatch, enabled):
    service = OrbSchwabService(settings=Settings(orb_schwab_observe_enabled=enabled))
    asyncio.run(service.run())
    assert service.session_factory is None


@pytest.mark.parametrize("reason,histogram", [("negative", -0.1), ("missing_last_bar", None)])
def test_observer_negative_or_missing_macd_never_proposes_buy(monkeypatch, caplog, reason, histogram):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (False, reason, histogram),
    )
    _observe_bars(service, clock)
    rows = [row for row in _observations(caplog) if row["kind"] == "decision"]
    assert len(rows) == 3
    assert all(row["proposed_action"] == "none" and not row["macd_allowed"] for row in rows)
    assert all(row["macd_reason"] == reason for row in rows)


def test_observer_records_price_crosses_not_fills_or_profit(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    asyncio.run(service._sync_gateway_subscription(["CLRO"]))
    clock[0] = OPEN + timedelta(seconds=1)
    for price in (5.69, 5.70, 5.71, 5.80, 5.80):
        service._handle_market_data(_trade(clock[0], price))
    rows = [row for row in _observations(caplog) if row["kind"] == "price_cross_only"]
    assert [row["relation"] for row in rows] == ["within_cap", "above_cap"]
    assert all(row["fill_status"] == "UNMEASURED" and row["execution"] == "NOT_TESTED" for row in rows)
    assert service._paper_positions == {} and service._paper_closed_today == []


@pytest.mark.parametrize("seconds,age", [(-1, 0), (1800, 0), (1, 6), (1, -1)])
def test_observer_does_not_claim_crosses_from_wrong_window_or_stale_tape(
    monkeypatch, caplog, seconds, age,
):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    trade_at = OPEN + timedelta(seconds=seconds)
    clock[0] = trade_at + timedelta(seconds=age)
    service._handle_market_data(_trade(trade_at, 5.70))
    assert not any(row["kind"] == "price_cross_only" for row in _observations(caplog))


def test_observer_marks_post_open_macd_cancel_as_conditional_even_after_cross(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    clock[0] = OPEN + timedelta(seconds=1)
    service._handle_market_data(_trade(clock[0], 5.70))
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (False, "negative", -0.1),
    )
    clock[0] = OPEN + timedelta(minutes=1)
    asyncio.run(service._observe_working_plans(clock[0]))
    asyncio.run(service._observe_working_plans(clock[0]))
    rows = [row for row in _observations(caplog) if row["kind"] == "working_plan_check"]
    assert len(rows) == 1
    assert rows[0]["proposed_action"] == "cancel_if_still_unfilled"
    assert rows[0]["fill_status"] == "UNMEASURED"
    assert service._opening_orders["CLRO"].cancelled


def test_observer_ten_oclock_cancels_only_hypothetical_unfilled_order(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    clock[0] = OPEN + timedelta(minutes=30)
    asyncio.run(service._observe_working_plans(clock[0]))
    rows = [row for row in _observations(caplog) if row["kind"] == "working_plan_check"]
    assert rows[0]["proposed_action"] == "cancel_if_still_unfilled"
    assert rows[0]["reason"] == "entry_window_ended"
    assert service._paper_positions == {}


def test_observer_session_roll_resets_all_observation_evidence(monkeypatch):
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock)
    clock[0] = OPEN + timedelta(seconds=1)
    service._handle_market_data(_trade(clock[0], 5.70))
    service._closed_bars.append(("CLRO", _bar(4, 5.7), clock[0]))
    service._maybe_roll_session(OPEN + timedelta(days=1))
    assert not service._opening_orders
    assert not service._closed_bars
    assert not service._observe_plan_at
    assert not service._observe_crosses
    assert not service._observe_seen_bars


def test_observer_does_not_call_received_bars_missing_after_conditional_cancel(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    _observe_bars(service, clock, count=3)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (False, "negative", -0.1),
    )
    for index in (3, 4):
        bar = _bar(index, 5.7)
        clock[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar)
        asyncio.run(service._process_closed_bars())
    asyncio.run(service._observe_working_plans(clock[0]))
    row = next(row for row in _observations(caplog) if row["kind"] == "coverage")
    assert row["missing_closed_range_minutes"]["CLRO"] == []
    assert service._opening_orders["CLRO"].cancelled


def test_observer_ignores_malformed_gateway_envelope(monkeypatch):
    service, _clock = _observer(monkeypatch)
    for event in ([], {"event_type": "trade_tick", "payload": []}):
        service._handle_market_data({"data": json.dumps(event)})
    assert not service._observe_crosses
