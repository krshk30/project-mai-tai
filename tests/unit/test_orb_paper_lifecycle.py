from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.orb_paper_lifecycle import (
    PAPER_LIFECYCLE_VERSION,
    OrbPaperPosition,
    compute_paper_atr_trail,
)
from project_mai_tai.orb_paper_store import (
    ORB_PAPER_ATR_BAR_EVENT_TYPE,
    ORB_PAPER_EVENT_TYPE,
    ORB_PAPER_EXIT_EVENT_TYPE,
    OrbPaperDecision,
)
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.services.control_plane import (
    _build_bot_position_rows,
    _build_completed_position_rows,
    _durable_paper_decision_symbols,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    SymbolState,
)


class _Store:
    def __init__(self, decisions: list[OrbPaperDecision] | None = None) -> None:
        self.decisions = list(decisions or [])

    def append(self, decision: OrbPaperDecision) -> bool:
        if any(item.event_key == decision.event_key for item in self.decisions):
            return False
        self.decisions.append(decision)
        return True

    def load_lifecycle(self) -> list[OrbPaperDecision]:
        return list(self.decisions)


def _service(*, store: _Store | None = None) -> OrbService:
    service = OrbService(
        settings=Settings(
            orb_running_high_enabled=True,
            orb_resting_entry_enabled=True,
            orb_paper_lifecycle_enabled=True,
            orb_paper_target_pct=5.0,
            orb_paper_stop_pct=8.0,
            orb_paper_min_break_body_pct=45.0,
            orb_paper_atr_exit_enabled=True,
            orb_reclaim_quantity=2,
        ),
        redis_client=MagicMock(),
        paper_store=store or _Store(),  # type: ignore[arg-type]
    )
    service._universe = {"FOO"}
    service._last_gateway_symbols = ["FOO"]
    return service


def _bar(service: OrbService, minute: int, value: float) -> OrbBar:
    return OrbBar(
        timestamp=service._session_open_utc() + timedelta(minutes=minute),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100,
    )


def _open_at_ten(service: OrbService) -> datetime:
    for minute, high in zip(range(-5, 0), (9.8, 9.9, 10.0, 9.95, 9.9), strict=True):
        service._on_bar("FOO", _bar(service, minute, high))
    fill_at = service._session_open_utc() + timedelta(seconds=5)
    aggregator = OrbTickAggregator(session_open=service._observe_open_utc())
    service._aggregators["FOO"] = aggregator
    aggregator.add_tick(fill_at - timedelta(seconds=1), 9.90, 100)
    aggregator.add_tick(fill_at, 10.01, 100)
    service._check_fixed_resting_fill("FOO", 10.01, fill_at)
    asyncio.run(service._record_pending_paper_entries())
    assert service._paper_positions["FOO"].entry_price == 10.0
    return fill_at


def test_plus_five_touch_closes_at_the_executable_bid_and_reports_pnl() -> None:
    store = _Store()
    service = _service(store=store)
    fill_at = _open_at_ten(service)

    service._evaluate_paper_position_quote(
        "FOO", bid=10.50, observed_at=fill_at + timedelta(seconds=1)
    )
    asyncio.run(service._record_pending_paper_entries())

    exit_decision = store.decisions[-1]
    assert exit_decision.event_type == ORB_PAPER_EXIT_EVENT_TYPE
    assert exit_decision.detail["exit_reason"] == "TARGET_PLUS_5_PCT_TOUCH"
    assert exit_decision.detail["price_basis"] == "EXECUTABLE_BID"
    assert exit_decision.detail["exit_price"] == 10.50
    assert exit_decision.detail["pnl"] == 1.0
    assert service._paper_positions == {}
    payload = service._build_heartbeat_payload()
    assert payload.daily_pnl == 1.0
    assert payload.closed_today[0]["exit_price"] == 10.50
    rendered, count, rendered_pnl = _build_completed_position_rows(
        {
            "strategy_code": "orb",
            "account_name": "paper:orb",
            "closed_today": payload.closed_today,
        },
        [],
        [],
    )
    assert count == 1
    assert rendered_pnl == 1.0
    assert all(
        expected in rendered
        for expected in ("FOO", "$10.00", "$10.50", "$+1.00 (+5.0%)", "Target Plus 5 Pct Touch")
    )


def test_minus_eight_touch_closes_at_the_gap_through_executable_bid() -> None:
    store = _Store()
    service = _service(store=store)
    fill_at = _open_at_ten(service)

    service._evaluate_paper_position_quote(
        "FOO", bid=9.10, observed_at=fill_at + timedelta(seconds=1)
    )
    asyncio.run(service._record_pending_paper_entries())

    exit_decision = store.decisions[-1]
    assert exit_decision.detail["exit_reason"] == "HARD_STOP_MINUS_8_PCT_TOUCH"
    assert exit_decision.detail["exit_price"] == 9.10
    assert exit_decision.detail["pnl_pct"] == -9.0


def test_break_bar_body_under_45_exits_on_the_first_post_fill_bid() -> None:
    store = _Store()
    service = _service(store=store)
    for minute, high in zip(range(-5, 0), (9.8, 9.9, 10.0, 9.95, 9.9), strict=True):
        service._on_bar("FOO", _bar(service, minute, high))
    fill_at = service._session_open_utc() + timedelta(seconds=5)
    aggregator = OrbTickAggregator(session_open=service._observe_open_utc())
    service._aggregators["FOO"] = aggregator
    aggregator.add_tick(fill_at - timedelta(seconds=2), 10.00, 100)
    aggregator.add_tick(fill_at - timedelta(seconds=1), 9.90, 100)
    aggregator.add_tick(fill_at, 10.01, 100)

    service._check_fixed_resting_fill("FOO", 10.01, fill_at)
    asyncio.run(service._record_pending_paper_entries())
    position = service._paper_positions["FOO"]
    assert position.break_body_pct is not None and position.break_body_pct < 45.0

    service._evaluate_paper_position_quote(
        "FOO", bid=9.99, observed_at=fill_at + timedelta(milliseconds=100)
    )
    asyncio.run(service._record_pending_paper_entries())

    assert store.decisions[-1].detail["exit_reason"] == "BREAK_BAR_BODY_UNDER_45_PCT"
    assert store.decisions[-1].detail["exit_decision_at"] == fill_at.isoformat()


def test_break_bar_body_at_exactly_45_does_not_trigger_the_body_exit() -> None:
    service = _service()
    for minute, high in zip(range(-5, 0), (98.0, 98.5, 99.0, 98.8, 98.7), strict=True):
        service._on_bar("FOO", _bar(service, minute, high))
    fill_at = service._session_open_utc() + timedelta(seconds=5)
    aggregator = OrbTickAggregator(session_open=service._observe_open_utc())
    service._aggregators["FOO"] = aggregator
    aggregator.add_tick(fill_at - timedelta(seconds=2), 91.0, 100)
    aggregator.add_tick(fill_at - timedelta(seconds=1), 80.0, 100)
    aggregator.add_tick(fill_at, 100.0, 100)

    service._check_fixed_resting_fill("FOO", 100.0, fill_at)
    asyncio.run(service._record_pending_paper_entries())
    position = service._paper_positions["FOO"]

    service._evaluate_paper_position_quote(
        "FOO", bid=98.9, observed_at=fill_at + timedelta(milliseconds=100)
    )

    assert position.break_body_pct == pytest.approx(45.0)
    assert position.body_exit_pending is False
    assert service._pending_paper_entries == []
    assert "FOO" in service._paper_positions


def test_missing_break_bar_evidence_is_visible_and_never_opens_a_gradable_position() -> None:
    store = _Store()
    service = _service(store=store)
    for minute, high in zip(range(-5, 0), (9.8, 9.9, 10.0, 9.95, 9.9), strict=True):
        service._on_bar("FOO", _bar(service, minute, high))
    fill_at = service._session_open_utc() + timedelta(seconds=5)

    service._check_fixed_resting_fill("FOO", 10.01, fill_at)
    asyncio.run(service._record_pending_paper_entries())

    assert service._paper_positions == {}
    assert store.decisions[-1].detail["paper_position_status"] == "UNANSWERABLE"
    assert (
        store.decisions[-1].detail["lifecycle_unanswerable_reason"]
        == "BREAK_BAR_EVIDENCE_MISSING_AT_MODELED_FILL"
    )
    assert service._build_heartbeat_payload().recent_decisions[0]["status"] == (
        "paper_trade_unanswerable"
    )


def test_atr_sell_flip_waits_for_the_bar_close_then_exits_on_the_next_bid() -> None:
    store = _Store()
    service = _service(store=store)
    start = service._session_open_utc() - timedelta(minutes=10)
    position = OrbPaperPosition(
        entry_event_key="entry:foo",
        symbol="FOO",
        entry_time=start + timedelta(minutes=8, seconds=30),
        entry_price=10.8,
        quantity=2,
        mode="fixed_opening_high_resting",
    )
    service._paper_positions["FOO"] = position
    closes = (10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.7, 10.4, 10.0)
    for index, close in enumerate(closes):
        bar = OrbBar(
            timestamp=start + timedelta(minutes=index),
            open=close,
            high=close + 0.05,
            low=close - 0.05,
            close=close,
            volume=100,
        )
        service._update_paper_atr("FOO", bar, observed_at=bar.timestamp + timedelta(minutes=1))

    assert position.atr_exit_pending is True
    assert position.atr_decision_at == start + timedelta(minutes=12, seconds=59)
    service._evaluate_paper_position_quote(
        "FOO", bid=9.98, observed_at=position.atr_decision_at + timedelta(milliseconds=10)
    )
    asyncio.run(service._record_pending_paper_entries())

    assert store.decisions[-1].detail["exit_reason"] == "ATR_TURNED_PURPLE_AT_BAR_CLOSE"
    assert store.decisions[-1].detail["atr_period"] == 5
    assert store.decisions[-1].detail["atr_factor"] == 3.5
    assert store.decisions[-1].detail["atr_average"] == "WILDERS"


def test_paper_atr_matches_live_v2_across_a_gap_and_the_0400_session_reset() -> None:
    et = ZoneInfo("America/New_York")
    pre_open = datetime(2026, 9, 9, 3, 50, tzinfo=et)
    after_open = datetime(2026, 9, 9, 4, 0, tzinfo=et)
    timestamps = [
        *(pre_open + timedelta(minutes=index) for index in range(9)),
        *(after_open + timedelta(minutes=index) for index in range(9)),
        after_open + timedelta(minutes=11),
    ]
    bars = [
        OrbBar(
            timestamp=timestamp.astimezone(UTC),
            open=10.0 + index / 100,
            high=10.05 + index / 100,
            low=9.95 + index / 100,
            close=10.0 + index / 100,
            volume=100,
        )
        for index, timestamp in enumerate(timestamps)
    ]
    paper_rows = compute_paper_atr_trail(bars)
    live = SchwabV2Strategy(Settings(_env_file=None))
    live_state = SymbolState(symbol="FOO")

    for bar, paper in zip(bars, paper_rows, strict=True):
        actual = live._update_atr_state(
            live_state,
            OHLCVBar(
                timestamp_ms=int(bar.timestamp.timestamp() * 1000),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=int(bar.volume),
            ),
        )
        assert paper["state"] == (actual["state"] if actual is not None else None)
        assert paper["flip"] == (actual["flip"] if actual is not None else None)
        if actual is None:
            assert paper["trail"] is None
        else:
            assert float(paper["trail"]) == pytest.approx(float(actual["trail"]))

    assert paper_rows[8]["state"] == "long"
    assert paper_rows[9]["state"] is None
    assert paper_rows[-1]["state"] == "long"


def test_paper_atr_matches_live_v2_through_a_sell_flip() -> None:
    start = datetime(2026, 9, 9, 9, 20, tzinfo=ZoneInfo("America/New_York"))
    closes = (10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.7, 10.4, 10.0)
    bars = [
        OrbBar(
            timestamp=(start + timedelta(minutes=index)).astimezone(UTC),
            open=close,
            high=close + 0.05,
            low=close - 0.05,
            close=close,
            volume=100,
        )
        for index, close in enumerate(closes)
    ]
    paper_rows = compute_paper_atr_trail(bars)
    live = SchwabV2Strategy(Settings(_env_file=None))
    live_state = SymbolState(symbol="FOO")
    live_flips: list[str | None] = []

    for bar, paper in zip(bars, paper_rows, strict=True):
        actual = live._update_atr_state(
            live_state,
            OHLCVBar(
                timestamp_ms=int(bar.timestamp.timestamp() * 1000),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=int(bar.volume),
            ),
        )
        actual_flip = actual["flip"] if actual is not None else None
        live_flips.append(actual_flip)
        assert paper["state"] == (actual["state"] if actual is not None else None)
        assert paper["flip"] == actual_flip
        if actual is None:
            assert paper["trail"] is None
        else:
            assert paper["trail"] == pytest.approx(actual["trail"])

    assert "SELL" in live_flips
    assert [row["flip"] for row in paper_rows] == live_flips


def test_clock_never_closes_a_position_and_open_symbol_keeps_its_feed() -> None:
    service = _service()
    fill_at = _open_at_ten(service)
    after_hours = fill_at.replace(hour=23, minute=59)

    service._evaluate_paper_position_quote("FOO", bid=10.10, observed_at=after_hours)

    assert "FOO" in service._paper_positions
    service._universe.clear()
    assert service._paper_market_symbols() == ["FOO"]
    assert service._pending_paper_entries == []


def test_restart_restores_an_open_position_and_same_day_closed_history() -> None:
    at = datetime.now(UTC).replace(microsecond=0)
    entry = OrbPaperDecision(
        event_key="entry:open",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="OPEN",
        observed_at=at,
        entry_price=Decimal("2.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "OPEN",
            "target_pct": 5.0,
            "stop_pct": 8.0,
            "break_bar_body_pct_at_fill": 60.0,
        },
    )
    closed_entry = OrbPaperDecision(
        event_key="entry:closed",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="CLOSED",
        observed_at=at,
        entry_price=Decimal("1.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "OPEN",
        },
    )
    closed_exit = OrbPaperDecision(
        event_key="exit:closed",
        event_type=ORB_PAPER_EXIT_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="CLOSED",
        observed_at=at + timedelta(seconds=1),
        entry_price=Decimal("1.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "entry_event_key": "entry:closed",
            "entry_time": at.isoformat(),
            "exit_time": (at + timedelta(seconds=1)).isoformat(),
            "exit_price": 1.05,
            "pnl": 0.10,
            "pnl_pct": 5.0,
            "exit_reason": "TARGET_PLUS_5_PCT_TOUCH",
        },
    )
    service = _service(store=_Store([entry, closed_entry, closed_exit]))

    service._restore_paper_lifecycle()

    assert set(service._paper_positions) == {"OPEN"}
    assert service._paper_market_symbols() == ["FOO", "OPEN"]
    assert service._paper_closed_today[0]["ticker"] == "CLOSED"
    assert service._build_heartbeat_payload().daily_pnl == 0.10
    assert service._paper_exit_counts == {"TARGET_PLUS_5_PCT_TOUCH": 1}


def test_restart_restores_exact_atr_bars_and_a_pending_sell_flip() -> None:
    at = datetime.now(UTC).replace(second=0, microsecond=0)
    bars = [
        OrbBar(
            timestamp=at - timedelta(minutes=5 - index),
            open=2.0 + index / 100,
            high=2.02 + index / 100,
            low=1.98 + index / 100,
            close=2.0 + index / 100,
            volume=100,
        )
        for index in range(5)
    ]
    entry = OrbPaperDecision(
        event_key="entry:atr",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="ATR",
        observed_at=at,
        entry_price=Decimal("2.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "OPEN",
            "target_pct": 5.0,
            "stop_pct": 8.0,
            "break_bar_body_pct_at_fill": 60.0,
            "atr_bars_at_entry": [OrbService._paper_bar_payload(bar) for bar in bars],
        },
    )
    flip_bar = OrbBar(
        timestamp=at + timedelta(minutes=1),
        open=2.0,
        high=2.01,
        low=1.89,
        close=1.90,
        volume=200,
    )
    atr = OrbPaperDecision(
        event_key="atr:bar",
        event_type=ORB_PAPER_ATR_BAR_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="ATR",
        observed_at=at + timedelta(minutes=2),
        entry_price=Decimal("1.90"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "entry_event_key": "entry:atr",
            "atr_state": "SELL",
            "atr_trail": 1.97,
            "atr_flip": "SELL",
            "bar": OrbService._paper_bar_payload(flip_bar),
        },
    )
    later_bar = OrbBar(
        timestamp=at + timedelta(minutes=10),
        open=3.0,
        high=3.01,
        low=2.99,
        close=3.0,
        volume=100,
    )
    later_entry = OrbPaperDecision(
        event_key="entry:later",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="LATER",
        observed_at=at + timedelta(minutes=9),
        entry_price=Decimal("3.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "OPEN",
            "target_pct": 5.0,
            "stop_pct": 8.0,
            "break_bar_body_pct_at_fill": 60.0,
        },
    )
    later_atr = OrbPaperDecision(
        event_key="atr:later",
        event_type=ORB_PAPER_ATR_BAR_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="LATER",
        observed_at=at + timedelta(minutes=11),
        entry_price=Decimal("3.00"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "entry_event_key": "entry:later",
            "atr_state": "BUY",
            "atr_trail": 2.90,
            "atr_flip": None,
            "bar": OrbService._paper_bar_payload(later_bar),
        },
    )
    service = _service(store=_Store([entry, atr, later_entry, later_atr]))

    service._restore_paper_lifecycle()

    position = service._paper_positions["ATR"]
    assert position.atr_exit_pending is True
    assert position.atr_decision_at == flip_bar.timestamp.replace(second=59)
    assert [bar.timestamp for bar in service._states["ATR"].atr_bars] == [
        *(bar.timestamp for bar in bars),
        flip_bar.timestamp,
    ]
    assert service._paper_atr_bars_restored == 7
    assert service._md_offset == f"{int((flip_bar.timestamp.timestamp() + 60) * 1000) - 1}-0"


def test_dashboard_keeps_paper_history_visible_after_the_watchlist_clears() -> None:
    runtime = {
        "watchlist": [],
        "positions": [],
        "recent_decisions": [{"ticker": "YMAT", "status": "paper_trade_closed"}],
        "closed_today": [{"ticker": "YMAT", "pnl": 0.25}],
    }

    assert _durable_paper_decision_symbols("orb_paper", runtime) == {"YMAT"}
    assert _durable_paper_decision_symbols("macd", runtime) == set()


def test_pre_lifecycle_entry_evidence_is_not_reopened_as_a_phantom_position() -> None:
    at = datetime.now(UTC).replace(microsecond=0)
    old_entry = OrbPaperDecision(
        event_key="entry:old-ymat",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="YMAT",
        observed_at=at,
        entry_price=Decimal("1.91"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={"status": "RECORDED_NOT_A_BROKER_FILL"},
    )
    service = _service(store=_Store([old_entry]))

    service._restore_paper_lifecycle()

    assert service._paper_positions == {}
    assert service._paper_closed_today == []


def test_unanswerable_lifecycle_entry_is_not_reopened_as_a_phantom_position() -> None:
    at = datetime.now(UTC).replace(microsecond=0)
    unanswerable = OrbPaperDecision(
        event_key="entry:unanswerable",
        event_type=ORB_PAPER_EVENT_TYPE,
        session_date=at.astimezone().date(),
        symbol="YMAT",
        observed_at=at,
        entry_price=Decimal("1.91"),
        quantity=Decimal("2"),
        attempt=1,
        mode="fixed_opening_high_resting",
        detail={
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "UNANSWERABLE",
            "lifecycle_unanswerable_reason": "BREAK_BAR_EVIDENCE_MISSING_AT_MODELED_FILL",
        },
    )
    service = _service(store=_Store([unanswerable]))

    service._restore_paper_lifecycle()

    assert service._paper_positions == {}
    assert service._paper_recent_decisions[0]["status"] == "paper_trade_unanswerable"


def test_open_paper_position_is_labeled_as_a_model_not_a_broker_position() -> None:
    data = {"virtual_positions": [], "account_positions": []}
    bot = {
        "strategy_code": "orb",
        "account_name": "paper:orb",
        "runtime_kind": "orb_paper",
        "positions": [
            {
                "ticker": "YMAT",
                "quantity": 2,
                "entry_price": 1.91,
                "current_price": 1.95,
                "entry_time": "2026-09-09T09:57:21-04:00",
            }
        ],
    }

    rendered = _build_bot_position_rows(data, bot)

    assert "PAPER MODEL / NO BROKER" in rendered
    assert "GHOST" not in rendered
