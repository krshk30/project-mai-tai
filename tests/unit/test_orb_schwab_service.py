import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.fanout_outcome_consumer import session_anchor
from project_mai_tai.orb_schwab_macd import MacdVerdict
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_schwab_open import OrbSchwabOpeningOrder


def test_orb_schwab_entrypoint_configures_timestamped_logging(monkeypatch) -> None:
    from project_mai_tai.services import orb_schwab_app as entrypoint

    calls = []
    monkeypatch.setattr(entrypoint, "get_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(
        entrypoint,
        "configure_logging",
        lambda name, level: calls.append((name, level)),
    )

    class FakeService:
        async def run(self):
            calls.append(("service",))

    monkeypatch.setattr(entrypoint, "OrbSchwabService", FakeService)
    asyncio.run(entrypoint.main())

    assert calls == [("orb-schwab", "INFO"), ("service",)]


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
        lambda *_args: (MacdVerdict.ALLOWED, "nonnegative", 0.1),
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


def test_live_decision_log_names_negative_macd_at_entry_cutoff(monkeypatch, caplog) -> None:
    service = OrbSchwabService(
        settings=Settings(
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_broker_provider="schwab",
        ),
        session_factory=lambda: None,
    )
    service._universe = {"VEEA"}
    monkeypatch.setattr(service, "_session_open_utc", lambda: OPEN)
    monkeypatch.setattr(service, "_processing_time", lambda: OPEN - timedelta(seconds=30))
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.0013),
    )
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service._on_bar("VEEA", _bar(2, 5.5))
    asyncio.run(service._process_closed_bars())
    assert "reason=macd_negative_no_entry" in caplog.text
    assert "symbol=VEEA" in caplog.text
    assert "macd=negative histogram=-0.0013 action=none" in caplog.text
    assert service._opening_orders["VEEA"].cancelled is True


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
    decisions = iter(((MacdVerdict.ALLOWED, "nonnegative", 0.1),
                      (MacdVerdict.NEGATIVE, "negative", -0.1)))
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


@pytest.mark.parametrize("write_delay,expected_opens", [(3, 1), (89, 1), (90, 0), (91, 0)])
def test_late_macd_write_waits_without_weakening_live_bar_age(
    monkeypatch, caplog, write_delay, expected_opens
) -> None:
    caplog.set_level(logging.WARNING, logger="orb-schwab")
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
    clock = [OPEN]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    third_close = OPEN - timedelta(minutes=2)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (
            (MacdVerdict.ALLOWED, "nonnegative", 0.1)
            if clock[0] >= third_close + timedelta(seconds=write_delay)
            else (MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None)
        ),
    )
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture
    )
    for index in range(2):
        bar = _bar(index, 5.3 + index * 0.1)
        clock[0] = bar.timestamp + timedelta(minutes=1)
        service._on_bar("CLRO", bar)
        asyncio.run(service._process_closed_bars())
    bar = _bar(2, 5.5)
    clock[0] = third_close + timedelta(milliseconds=200)
    service._on_bar("CLRO", bar)
    asyncio.run(service._process_closed_bars())
    assert emitted == []
    assert service._opening_orders["CLRO"].cancelled is False
    clock[0] = third_close + timedelta(seconds=write_delay)
    asyncio.run(service._process_closed_bars())
    assert len(emitted) == expected_opens
    if expected_opens:
        assert emitted[0].payload.intent_type == "open"
        assert emitted[0].payload.metadata["orb_deferred_macd_bar_close"] == third_close.isoformat()
    else:
        assert service._opening_orders["CLRO"].cancelled is True
        assert "[ORB-SCHWAB-BAR-MISSING]" in caplog.text or "[ORB-SCHWAB-ENTRY-CUTOFF]" in caplog.text


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
        lambda *_args: (MacdVerdict.ALLOWED, "nonnegative", 0.1),
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
        lambda *_args: (MacdVerdict.ALLOWED, "nonnegative", 0.1),
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
    # The cross is hypothetical; body decisions still wait for the Schwab close.
    for seconds, price in ((0, 5.69), (2, 5.60), (5, 5.50), (10, 5.70)):
        clock[0] = OPEN + timedelta(seconds=seconds)
        service._handle_market_data(_trade(clock[0], price))
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_completed_atr_bars",
                        lambda *_args: ([], "missing_last_closed_schwab_bar"))
    asyncio.run(service._process_strategy_exits(clock[0]))
    rows = [row for row in _observations(caplog) if row["kind"] == "conditional_exit"]
    assert len(rows) == 1 and rows[0]["reason"] is None
    bar = OrbBar(timestamp=OPEN, open=5.69, high=5.70, low=5.50, close=5.70, volume=100)
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_completed_atr_bars",
                        lambda *_args: ([bar], "insufficient_schwab_atr_history"))
    clock[0] = OPEN + timedelta(minutes=1, seconds=3)
    asyncio.run(service._process_strategy_exits(clock[0]))
    row = [row for row in _observations(caplog) if row["kind"] == "conditional_exit"][-1]
    assert row["reason"] == BODY_REASON
    assert row["fill_status"] == "UNMEASURED"
    assert row["broker_orders_sent"] == 0
    assert "NOT_A_BROKER_FILL" in row["assumption"]


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


def test_observer_negative_macd_never_proposes_buy(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
    )
    _observe_bars(service, clock)
    rows = [row for row in _observations(caplog) if row["kind"] == "decision"]
    assert len(rows) == 3
    assert all(row["proposed_action"] == "none" and not row.get("macd_allowed") for row in rows)
    assert all(row.get("macd_reason") in {"negative", None} for row in rows)


def test_observer_missing_bar_waits_then_records_no_entry(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orb-schwab")
    service, clock = _observer(monkeypatch)
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None),
    )
    _observe_bars(service, clock, count=3)
    assert service._opening_orders["CLRO"].cancelled is False
    assert [row for row in _observations(caplog) if row["kind"] == "decision"] == []
    clock[0] = _bar(2, 5.5).timestamp + timedelta(minutes=2, seconds=31)
    asyncio.run(service._process_closed_bars())
    assert service._opening_orders["CLRO"].cancelled is True
    rows = [row for row in _observations(caplog) if row["kind"] == "decision"]
    assert rows[-1]["decision_reason"] == "bar_missing"


@pytest.mark.parametrize(
    "symbol,index,checked_at,saved_at",
    [
        ("VBIO", 2, "13:28:00.119289", "13:28:02.683878"),
        ("LGHL", 2, "13:28:00.307461", "13:28:02.697906"),
        ("LGHL", 3, "13:29:00.126399", "13:29:01.782602"),
        ("VBIO", 3, "13:29:00.503973", "13:29:02.838123"),
        ("VBIO", 3, "13:29:01.372932", "13:29:02.838123"),
        ("LGHL", 3, "13:29:01.411551", "13:29:01.782602"),
        ("VBIO", 4, "13:30:00.316341", "13:30:03.019082"),
        ("LGHL", 4, "13:30:00.987426", "13:30:03.259540"),
    ],
)
@pytest.mark.parametrize("eventual_verdict", [MacdVerdict.NEGATIVE, MacdVerdict.ALLOWED])
def test_sept30_eight_decisions_wait_for_actual_schwab_write(
    monkeypatch, symbol, index, checked_at, saved_at, eventual_verdict
):
    # Times are the real 09-30 observer log and strategy_bar_history.created_at.
    # The eventual real MACD was negative for both names. ALLOWED is a synthetic
    # counterfactual on the same observed write timing, not a claim about the tape.
    session = "2026-09-30T"
    check = datetime.fromisoformat(session + checked_at + "+00:00")
    saved = datetime.fromisoformat(session + saved_at + "+00:00")
    opening = datetime(2026, 9, 30, 13, 30, tzinfo=UTC)
    service = OrbSchwabService(
        settings=Settings(orb_enabled=True, orb_schwab_observe_enabled=True),
        redis_client=_ReadOnlyRedis(), session_factory=lambda: None,
    )
    service._universe = {symbol}
    monkeypatch.setattr(service, "_session_open_utc", lambda: opening)
    clock = [check]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    monkeypatch.setattr(
        "project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
        lambda *_args: (
            (MacdVerdict.BAR_NOT_YET, "missing_last_closed_schwab_bar", None)
            if clock[0] < saved else (
                eventual_verdict,
                "negative" if eventual_verdict == MacdVerdict.NEGATIVE else "nonnegative",
                -0.01 if eventual_verdict == MacdVerdict.NEGATIVE else 0.01,
            )
        ),
    )
    order = OrbSchwabOpeningOrder(opening)
    for prior in range(index):
        minute = opening - timedelta(minutes=5 - prior)
        order.bars[minute] = OrbBar(
            timestamp=minute, open=5.2, high=5.2, low=5.2, close=5.2,
            volume=100, breakout_high=5.2,
        )
    order.placed = index > 2
    if order.placed:
        order.last_requested_level = Decimal("5.5")
    service._opening_orders[symbol] = order
    bar = OrbBar(
        timestamp=opening - timedelta(minutes=5 - index),
        open=5.4, high=5.4, low=5.4, close=5.4,
        volume=100, breakout_high=5.4,
    )
    service._on_bar(symbol, bar)
    asyncio.run(service._process_closed_bars())
    assert order.cancelled is False
    assert service._closed_bars
    clock[0] = max(saved + timedelta(milliseconds=1), check + timedelta(seconds=1))
    asyncio.run(service._process_closed_bars())
    assert order.cancelled is (eventual_verdict == MacdVerdict.NEGATIVE and index > 2)
    if eventual_verdict == MacdVerdict.ALLOWED and index == 2:
        assert order.placed is True
    assert not service._closed_bars


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
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
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


@pytest.mark.parametrize("today", [(2026, 9, 29), (2026, 9, 30), (2026, 10, 1)])
def test_observer_session_roll_resets_all_observation_evidence(monkeypatch, today):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(*today, 12, tzinfo=UTC)
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("project_mai_tai.services.orb_app.datetime", FakeDatetime)
    service, clock = _observer(monkeypatch)
    # The fixture's opening session, not the machine date, owns this state.
    service._session_date = OPEN.astimezone(ZoneInfo("America/New_York")).date()
    service._scanner_session_start = session_anchor(OPEN)
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
        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1),
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
