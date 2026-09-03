from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.confirmation_exit import (
    ConfirmationEntry,
    ConfirmationEvaluation,
    ConfirmationExitTracker,
    confirmation_bar_start_ms,
    is_first_slot_resting,
)
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    Fill,
    PaperExitRuleConfig,
    Strategy,
    TradeIntent,
    V2ConfirmationExitEvaluation,
)
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.paper_exit import PaperExitRuntime, PaperRuleConfig, PaperSourceFill
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


def _evaluation(*, atr_state: str = "short") -> ConfirmationEvaluation:
    target = int(datetime(2026, 9, 2, 14, 0, tzinfo=UTC).timestamp() * 1000)
    return ConfirmationEvaluation(
        entry=ConfirmationEntry(
            order_id=uuid4(),
            fill_id=uuid4(),
            broker_fill_id="fill-1",
            broker_order_id="order-1",
            broker_account_name="live:schwab_1m_v2",
            symbol="LHAI",
            filled_at=datetime(2026, 9, 2, 13, 59, 30, tzinfo=UTC),
            evaluation_bar_start_ms=target,
            confirmation_bars=1,
            config_id=uuid4(),
            config_effective_at=datetime(1970, 1, 1, tzinfo=UTC),
        ),
        bar_start_ms=target,
        atr_state=atr_state,
    )


def test_live_confirmation_exit_defaults_enabled_after_operator_go() -> None:
    assert Settings().strategy_schwab_1m_v2_confirmation_exit_enabled is True


def test_dark_confirmation_records_but_cannot_reach_the_emitter() -> None:
    evaluation = _evaluation()
    service = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_confirmation_exit_enabled=False),
        session_factory=None,
    )
    terminalized = []

    class _NoLiveEmitter:
        async def emit_confirmation_exit(self, _evaluation) -> None:
            raise AssertionError("dark CONF1 reached the live emitter")

    service.intent_emitter = _NoLiveEmitter()
    service._mark_confirmation_published = terminalized.append  # type: ignore[method-assign]

    asyncio.run(service._publish_confirmation_evaluation(evaluation))

    assert terminalized == [evaluation.entry.fill_id]


def test_enabled_confirmation_reaches_the_emitter() -> None:
    evaluation = _evaluation()
    service = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_confirmation_exit_enabled=True),
        session_factory=None,
    )
    emitted = []
    terminalized = []

    class _CapturingEmitter:
        async def emit_confirmation_exit(self, item) -> None:
            emitted.append(item)

    service.intent_emitter = _CapturingEmitter()
    service._mark_confirmation_published = terminalized.append  # type: ignore[method-assign]

    asyncio.run(service._publish_confirmation_evaluation(evaluation))

    assert emitted == [evaluation]
    assert terminalized == [evaluation.entry.fill_id]


def test_unknown_atr_state_has_a_distinct_fired_marker(caplog) -> None:
    evaluation = _evaluation(atr_state="unknown")
    service = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_confirmation_exit_enabled=False),
        session_factory=None,
    )
    service._record_confirmation_evaluation = lambda _item: (True, False)  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)

    asyncio.run(service._emit_confirmation_evaluations([evaluation]))

    assert "[V2-CONFIRMATION-EXIT-FIRED-UNKNOWN]" in caplog.text


def test_confirmation_bar_is_first_full_bar_after_containing_bar() -> None:
    assert confirmation_bar_start_ms(
        datetime(2026, 9, 2, 13, 58, 37, tzinfo=UTC), 1
    ) == int(datetime(2026, 9, 2, 13, 59, tzinfo=UTC).timestamp() * 1000)
    assert confirmation_bar_start_ms(
        datetime(2026, 9, 2, 13, 58, 59, 999999, tzinfo=UTC), 1
    ) == int(datetime(2026, 9, 2, 13, 59, tzinfo=UTC).timestamp() * 1000)
    assert confirmation_bar_start_ms(
        datetime(2026, 9, 2, 13, 59, 0, tzinfo=UTC), 1
    ) == int(datetime(2026, 9, 2, 14, 0, tzinfo=UTC).timestamp() * 1000)


def test_reclaim_stamp_is_never_eligible_for_confirmation() -> None:
    first = {
        "cw_entry_slot": "first",
        "atr_variant": "CW-v2-resting",
        "resting_entry": "true",
    }
    reclaim = {**first, "cw_entry_slot": "reclaim"}
    assert is_first_slot_resting(first) is True
    assert is_first_slot_resting(reclaim) is False


def test_tracker_evaluates_each_entry_once_on_exact_bar() -> None:
    fill_at = datetime(2026, 9, 2, 14, 10, 39, tzinfo=UTC)
    target = confirmation_bar_start_ms(fill_at, 1)
    entry = ConfirmationEntry(
        order_id=uuid4(),
        fill_id=uuid4(),
        broker_fill_id="fill-1",
        broker_order_id="order-1",
        broker_account_name="live:schwab_1m_v2",
        symbol="LHAI",
        filled_at=fill_at,
        evaluation_bar_start_ms=target,
        confirmation_bars=1,
        config_id=uuid4(),
        config_effective_at=datetime(1970, 1, 1, tzinfo=UTC),
    )
    tracker = ConfirmationExitTracker()
    assert tracker.add(entry) is True
    assert tracker.evaluate_bar(symbol="LHAI", bar_start_ms=target - 60_000, atr_state="short") == []
    evaluations = tracker.evaluate_bar(symbol="LHAI", bar_start_ms=target, atr_state="short")
    assert len(evaluations) == 1
    assert evaluations[0].should_exit is True
    assert tracker.evaluate_bar(symbol="LHAI", bar_start_ms=target, atr_state="short") == []


def test_later_bar_for_another_symbol_does_not_expire_pending_entry() -> None:
    target = int(datetime(2026, 9, 2, 14, 0, tzinfo=UTC).timestamp() * 1000)
    entry = ConfirmationEntry(
        order_id=uuid4(),
        fill_id=uuid4(),
        broker_fill_id="fill-1",
        broker_order_id="order-1",
        broker_account_name="live:schwab_1m_v2",
        symbol="LHAI",
        filled_at=datetime(2026, 9, 2, 13, 59, 30, tzinfo=UTC),
        evaluation_bar_start_ms=target,
        confirmation_bars=1,
        config_id=uuid4(),
        config_effective_at=datetime(1970, 1, 1, tzinfo=UTC),
    )
    tracker = ConfirmationExitTracker()
    tracker.add(entry)

    assert tracker.expire_before(symbol="OTHER", bar_start_ms=target + 60_000) == []
    assert len(tracker.evaluate_bar(symbol="LHAI", bar_start_ms=target, atr_state="long")) == 1


def test_live_evaluation_reads_v2s_post_bar_schwab_atr_state() -> None:
    target = int(datetime(2026, 9, 2, 14, 0, tzinfo=UTC).timestamp() * 1000)
    entry = ConfirmationEntry(
        order_id=uuid4(),
        fill_id=uuid4(),
        broker_fill_id="fill-1",
        broker_order_id="order-1",
        broker_account_name="live:schwab_1m_v2",
        symbol="LHAI",
        filled_at=datetime(2026, 9, 2, 13, 59, 30, tzinfo=UTC),
        evaluation_bar_start_ms=target,
        confirmation_bars=1,
        config_id=uuid4(),
        config_effective_at=datetime(1970, 1, 1, tzinfo=UTC),
    )
    service = SchwabV2BotService(session_factory=None)
    service._confirmation_exit.add(entry)
    service.strategy._symbol_states["LHAI"] = SimpleNamespace(atr_state="short")
    captured = []

    async def capture(evaluations):
        captured.extend(evaluations)

    async def no_op(*_args, **_kwargs):
        return None

    service._strategy_on_bar = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    service._emit_confirmation_evaluations = capture  # type: ignore[method-assign]
    service._maybe_emit = no_op  # type: ignore[method-assign]
    service._drain_direct_strategy_intents = no_op  # type: ignore[method-assign]
    service._emit_webull_fanout_legs = no_op  # type: ignore[method-assign]

    asyncio.run(
        service._handle_bar(
            "LHAI",
            ChartBar("LHAI", 1.2, 1.3, 1.1, 1.15, 100, target),
            observation_phase="live",
        )
    )

    assert len(captured) == 1
    assert captured[0].atr_state == "short"
    assert captured[0].should_exit is True


def test_live_fill_census_schedules_first_slot_and_excludes_reclaim() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        session.add_all([strategy, account])
        session.flush()
        session.add(
            PaperExitRuleConfig(
                id=uuid4(),
                target_pct=Decimal("5"),
                stop_pct=Decimal("8"),
                confirmation_bars=1,
                effective_at=datetime(1970, 1, 1, tzinfo=UTC),
                changed_by="test",
            )
        )
        session.add(
            PaperExitRuleConfig(
                id=uuid4(),
                target_pct=Decimal("5"),
                stop_pct=Decimal("8"),
                confirmation_bars=3,
                effective_at=now + timedelta(minutes=1),
                changed_by="future-test",
            )
        )
        for index, slot in enumerate(("first", "reclaim"), start=1):
            metadata = {
                "cw_entry_slot": slot,
                "atr_variant": "CW-v2-resting",
                "resting_entry": "true",
            }
            intent = TradeIntent(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                symbol="TEST",
                side="buy",
                intent_type="open",
                quantity=Decimal("1"),
                reason=slot,
                status="filled",
                payload={"metadata": metadata},
            )
            session.add(intent)
            session.flush()
            order = BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"client-{index}",
                broker_order_id=f"broker-{index}",
                symbol="TEST",
                side="buy",
                order_type="stop_limit",
                time_in_force="day",
                quantity=Decimal("1"),
                status="filled",
                payload={"metadata": metadata},
            )
            session.add(order)
            session.flush()
            session.add(
                Fill(
                    order_id=order.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    broker_fill_id=f"fill-{index}",
                    symbol="TEST",
                    side="buy",
                    quantity=Decimal("1"),
                    price=Decimal("10"),
                    filled_at=now,
                    payload={"metadata": metadata},
                )
            )
        session.commit()

    service = SchwabV2BotService(
        Settings(strategy_schwab_1m_v2_account_name="live:schwab_1m_v2"),
        session_factory=factory,
    )
    entries = service._load_confirmation_entries()
    assert len(entries) == 1
    assert entries[0].broker_fill_id == "fill-1"
    # The fill precedes the future effective timestamp, so it keeps the one-bar config. A config
    # change cannot silently reach backward into an open measurement.
    assert entries[0].confirmation_bars == 1


def test_paper_confirmation_uses_stamped_first_fill_and_never_reclaim() -> None:
    config = PaperRuleConfig(
        uuid4(), Decimal("5"), Decimal("8"), datetime(1970, 1, 1, tzinfo=UTC), 1
    )
    runtime = PaperExitRuntime(config)
    first_id = uuid4()
    reclaim_id = uuid4()
    first = PaperSourceFill(
        fill_id=first_id,
        broker_fill_id="first",
        broker_account_name="live:schwab_1m_v2",
        venue="schwab",
        symbol="TEST",
        quantity=Decimal("1"),
        price=Decimal("10"),
        filled_at=datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
        fanout_slot_id="slot-first",
        entry_slot="first",
        source="cw-v2-resting",
    )
    reclaim = PaperSourceFill(
        **{
            **first.__dict__,
            "fill_id": reclaim_id,
            "broker_fill_id": "reclaim",
            "fanout_slot_id": "slot-reclaim",
            "entry_slot": "reclaim",
        }
    )
    runtime.add_mirror_fill(first)
    runtime.add_mirror_fill(reclaim)
    at = datetime(2026, 9, 2, 14, 2, tzinfo=UTC)
    assert runtime.on_confirmation_exit(
        source_fill_id=reclaim_id,
        observed_at=at,
        atr_state="short",
        confirmation_bars=1,
        config_effective_at=config.effective_at,
    ) == []
    fired = runtime.on_confirmation_exit(
        source_fill_id=first_id,
        observed_at=at,
        atr_state="short",
        confirmation_bars=1,
        config_effective_at=config.effective_at,
    )
    assert [item.event_type for item in fired] == ["CONFIRMATION_EXIT_FIRED"]
    runtime.on_trade(symbol="TEST", observed_at=at)
    exits = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("9.6"),
        ask=Decimal("9.7"),
        observed_at=at.replace(second=1),
    )
    exit_decision = next(item for item in exits if item.event_type == "PAPER_EXIT")
    assert exit_decision.detail["reason"] == "CONFIRMATION_EXIT"
    summary = runtime.summary()["paper_exit"]["confirmation_exit"]
    assert summary == {
        "status": "MEASURED",
        "evaluated": 1,
        "fired": 1,
        "state_long": 0,
        "denominator": 1,
    }


def test_late_long_confirmation_continues_without_historical_quote_replay() -> None:
    config = PaperRuleConfig(
        uuid4(), Decimal("5"), Decimal("8"), datetime(1970, 1, 1, tzinfo=UTC), 1
    )
    runtime = PaperExitRuntime(config)
    source_fill_id = uuid4()
    runtime.add_mirror_fill(
        PaperSourceFill(
            fill_id=source_fill_id,
            broker_fill_id="chpt-fill",
            broker_account_name="live:schwab_1m_v2",
            venue="schwab",
            symbol="CHPT",
            quantity=Decimal("2"),
            price=Decimal("7.73"),
            filled_at=datetime(2026, 9, 3, 14, 6, 10, tzinfo=UTC),
            fanout_slot_id="chpt-first-slot",
            entry_slot="first",
            source="cw-v2-resting",
        )
    )
    evaluation_at = datetime(2026, 9, 3, 14, 8, tzinfo=UTC)
    runtime.on_quote(
        symbol="CHPT",
        bid=Decimal("7.80"),
        ask=Decimal("7.81"),
        observed_at=evaluation_at + timedelta(seconds=1),
    )

    decisions = runtime.on_confirmation_exit(
        source_fill_id=source_fill_id,
        observed_at=evaluation_at,
        atr_state="long",
        confirmation_bars=1,
        config_effective_at=config.effective_at,
    )

    assert [item.event_type for item in decisions] == ["CONFIRMATION_STATE_LONG"]
    assert runtime.summary()["paper_exit"]["mirror_open"] == 1
    assert runtime.summary()["closed_today"] == []


def test_late_non_long_confirmation_remains_unanswerable() -> None:
    config = PaperRuleConfig(
        uuid4(), Decimal("5"), Decimal("8"), datetime(1970, 1, 1, tzinfo=UTC), 1
    )
    runtime = PaperExitRuntime(config)
    source_fill_id = uuid4()
    runtime.add_mirror_fill(
        PaperSourceFill(
            fill_id=source_fill_id,
            broker_fill_id="late-short-fill",
            broker_account_name="live:schwab_1m_v2",
            venue="schwab",
            symbol="TEST",
            quantity=Decimal("1"),
            price=Decimal("10"),
            filled_at=datetime(2026, 9, 3, 14, 6, 10, tzinfo=UTC),
            fanout_slot_id="late-short-slot",
            entry_slot="first",
            source="cw-v2-resting",
        )
    )
    evaluation_at = datetime(2026, 9, 3, 14, 8, tzinfo=UTC)
    runtime.on_quote(
        symbol="TEST",
        bid=Decimal("9.80"),
        ask=Decimal("9.81"),
        observed_at=evaluation_at + timedelta(seconds=1),
    )

    decisions = runtime.on_confirmation_exit(
        source_fill_id=source_fill_id,
        observed_at=evaluation_at,
        atr_state="short",
        confirmation_bars=1,
        config_effective_at=config.effective_at,
    )

    assert [item.event_type for item in decisions] == ["UNANSWERABLE"]
    assert runtime.summary()["closed_today"][0]["exit_price"] is None


def test_confirmation_outbox_makes_evaluation_durable_and_one_shot() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config_id = uuid4()
    order_id = uuid4()
    fill_id = uuid4()
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        config = PaperExitRuleConfig(
            id=config_id,
            target_pct=Decimal("5"),
            stop_pct=Decimal("8"),
            confirmation_bars=1,
            effective_at=datetime(1970, 1, 1, tzinfo=UTC),
            changed_by="test",
        )
        session.add_all([strategy, account, config])
        session.flush()
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="TEST",
            side="buy",
            intent_type="open",
            quantity=Decimal("1"),
            reason="first",
            status="filled",
            payload={},
        )
        session.add(intent)
        session.flush()
        session.add(
            BrokerOrder(
                id=order_id,
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id="outbox-client",
                broker_order_id="outbox-order",
                symbol="TEST",
                side="buy",
                order_type="stop_limit",
                time_in_force="day",
                quantity=Decimal("1"),
                status="filled",
                payload={},
            )
        )
        session.flush()
        session.add(
            Fill(
                id=fill_id,
                order_id=order_id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="outbox-fill",
                symbol="TEST",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("10"),
                filled_at=datetime.now(UTC),
                payload={},
            )
        )
        session.commit()
    entry = ConfirmationEntry(
        order_id=order_id,
        fill_id=fill_id,
        broker_fill_id="outbox-fill",
        broker_order_id="outbox-order",
        broker_account_name="live:schwab_1m_v2",
        symbol="TEST",
        filled_at=datetime.now(UTC),
        evaluation_bar_start_ms=1_000_000,
        confirmation_bars=1,
        config_id=config_id,
        config_effective_at=datetime(1970, 1, 1, tzinfo=UTC),
    )
    evaluation = ConfirmationExitTracker()
    assert evaluation.add(entry)
    item = evaluation.evaluate_bar(
        symbol="TEST", bar_start_ms=1_000_000, atr_state="short"
    )[0]
    service = SchwabV2BotService(session_factory=factory)

    created, publish = service._record_confirmation_evaluation(item)
    assert (created, publish) == (True, True)
    created_again, publish_again = service._record_confirmation_evaluation(item)
    assert (created_again, publish_again) == (False, True)
    service._mark_confirmation_published(fill_id)
    assert service._record_confirmation_evaluation(item) == (False, False)
    with factory() as session:
        rows = list(session.query(V2ConfirmationExitEvaluation))
    assert len(rows) == 1
