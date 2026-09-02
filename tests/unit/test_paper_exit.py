from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    Fill,
    PaperExitEvent,
    PaperExitRuleConfig,
    Strategy,
    TradeIntent,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.paper_exit import (
    PaperDecision,
    PaperExitRuntime,
    PaperRuleConfig,
    PaperSourceFill,
    completed_session_acceptance,
    logical_mirror_id,
    mirror_acceptance,
    resting_fill_classification,
    terminal_evidence_covers,
)
from project_mai_tai.paper_exit_store import PaperExitStore
from project_mai_tai.services.strategy_engine_app import (
    PaperDisabledPolygonEntryEngine,
    PaperPolygonRuntimeAdapter,
    StrategyBotRuntime,
    StrategyEngineService,
)
from project_mai_tai.strategy_core import TradingConfig

AT = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
CONFIG_ID = UUID("11111111-1111-1111-1111-111111111111")


def config(*, target: str = "5", stop: str = "8", at: datetime = AT) -> PaperRuleConfig:
    return PaperRuleConfig(CONFIG_ID, Decimal(target), Decimal(stop), at)


def fill(
    *,
    fill_id: str,
    broker_fill_id: str,
    venue: str,
    slot: str = "slot-1",
    at: datetime = AT + timedelta(minutes=1),
    quantity: str = "2",
    price: str = "10",
) -> PaperSourceFill:
    return PaperSourceFill(
        fill_id=UUID(fill_id),
        broker_fill_id=broker_fill_id,
        broker_account_name="live:schwab_1m_v2" if venue == "schwab" else "live:orb",
        venue=venue,  # type: ignore[arg-type]
        symbol="TEST",
        quantity=Decimal(quantity),
        price=Decimal(price),
        filled_at=at,
        fanout_slot_id=slot,
        source="rth_resting_mirror" if venue == "webull" else "cw-v2-resting",
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"cw_entry_slot": "first", "atr_variant": "CW-v2-resting", "resting_entry": "true"},
        {"cw_entry_slot": "first", "fanout_source": "rth_resting"},
        {"cw_entry_slot": "first", "fanout_source": "rth_resting_mirror"},
        {"cw_entry_slot": "first", "fanout_source": "eh_resting"},
    ],
)
def test_exact_resting_sources_are_accepted(metadata: dict[str, str]) -> None:
    assert resting_fill_classification(metadata)[0] is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"cw_entry_slot": "reclaim", "resting_entry": "true", "fanout_source": "reactive"},
        {"cw_entry_slot": "first", "fanout_source": "reactive"},
        {"cw_entry_slot": "first", "fanout_source": "unknown"},
        {
            "cw_entry_slot": "first",
            "fanout_source": "reactive",
            "atr_variant": "CW-v2-resting",
            "resting_entry": "true",
        },
        {"resting_entry": "true", "atr_variant": "CW-v2-resting"},
        {"cw_entry_slot": "first", "atr_variant": "CW-v2-resting"},
    ],
)
def test_reclaim_missing_and_contradictory_stamps_fail_closed(metadata: dict[str, str]) -> None:
    assert resting_fill_classification(metadata)[0] is False


def test_both_venues_collapse_only_on_the_stamped_slot() -> None:
    schwab = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="schwab-fill",
        venue="schwab",
    )
    webull = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="webull-fill",
        venue="webull",
        at=AT + timedelta(minutes=1, seconds=3),
    )
    assert logical_mirror_id(schwab) == logical_mirror_id(webull)
    runtime = PaperExitRuntime(config())
    assert runtime.add_mirror_fill(schwab)[0].event_type == "MIRROR_ENTRY"
    assert runtime.add_mirror_fill(webull)[0].event_type == "MIRROR_LEG_COLLAPSED"
    assert runtime.summary()["paper_exit"]["mirror_open"] == 1  # type: ignore[index]


def test_missing_durable_marker_is_reemitted_after_runtime_mutation() -> None:
    source = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="schwab-fill",
        venue="schwab",
    )
    runtime = PaperExitRuntime(config())
    assert runtime.add_mirror_fill(source)[0].event_type == "MIRROR_ENTRY"
    assert runtime.add_mirror_fill(source) == []
    retry = runtime.add_mirror_fill(source, reemit_evidence=True)
    assert retry[0].event_type == "LATE_MIRROR"
    assert retry[0].source_fill_id == source.fill_id


def test_collapsed_entry_is_quantity_weighted_and_arrival_order_invariant() -> None:
    schwab = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="schwab-fill",
        venue="schwab",
        quantity="2",
        price="10",
    )
    webull = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="webull-fill",
        venue="webull",
        quantity="1",
        price="11",
        at=AT + timedelta(minutes=1, seconds=2),
    )
    entries = []
    for ordered in ((schwab, webull), (webull, schwab)):
        runtime = PaperExitRuntime(config())
        for source in ordered:
            runtime.add_mirror_fill(source)
        entries.append(runtime.summary()["positions"][0])
    assert entries[0]["quantity"] == 3.0
    assert entries[0]["entry_price"] == pytest.approx(31 / 3)
    assert entries[0] == entries[1]


def test_timing_chain_cannot_merge_different_slots() -> None:
    first = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="a",
        venue="schwab",
        slot="slot-a",
    )
    middle = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="b",
        venue="webull",
        slot="slot-b",
        at=AT + timedelta(minutes=1, seconds=7),
    )
    last = fill(
        fill_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        broker_fill_id="c",
        venue="schwab",
        slot="slot-c",
        at=AT + timedelta(minutes=1, seconds=14),
    )
    assert len({logical_mirror_id(first), logical_mirror_id(middle), logical_mirror_id(last)}) == 3


def test_first_timestamped_target_stop_flip_close_priority() -> None:
    runtime = PaperExitRuntime(config())
    source = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="fill-1",
        venue="schwab",
    )
    runtime.add_mirror_fill(source)
    runtime.mark_atr_sell("TEST", AT + timedelta(minutes=2))
    decisions = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )
    assert decisions[0].detail["reason"] == "TARGET"


def test_earlier_atr_sell_beats_a_target_seen_on_the_next_quote() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    assert runtime.on_atr_sell(symbol="TEST", observed_at=AT + timedelta(minutes=2)) == []
    decision = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2, seconds=1),
    )[0]
    assert decision.detail["reason"] == "ATR_SELL"


def test_late_arriving_earlier_atr_sell_is_unanswerable_without_quote_replay() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    first = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=3),
    )[0]
    assert first.detail["reason"] == "TARGET"

    correction = runtime.on_atr_sell(
        symbol="TEST", observed_at=AT + timedelta(minutes=2)
    )[0]
    assert correction.event_type == "UNANSWERABLE"
    assert correction.price is None
    assert correction.detail["supersedes_reason"] == "TARGET"


def test_late_atr_sell_after_a_non_exiting_quote_is_unanswerable() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    assert runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.10"),
        ask=Decimal("10.11"),
        observed_at=AT + timedelta(minutes=3),
    ) == []

    correction = runtime.on_atr_sell(
        symbol="TEST", observed_at=AT + timedelta(minutes=2)
    )[0]
    assert correction.event_type == "UNANSWERABLE"
    assert correction.price is None
    assert correction.detail["supersedes_reason"] == "OPEN"
    assert runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.20"),
        ask=Decimal("10.21"),
        observed_at=AT + timedelta(minutes=4),
    ) == []


def test_terminal_decision_remains_pending_until_durable_acknowledgement() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    decision = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )[0]
    assert runtime.pending_terminal_decisions() == [decision]
    runtime.acknowledge_decisions({decision.event_key})
    assert runtime.pending_terminal_decisions() == []


def test_terminal_evidence_must_cover_final_collapsed_leg() -> None:
    assert terminal_evidence_covers(
        final_fill_at=AT + timedelta(minutes=2),
        final_quantity=Decimal("4"),
        terminal_at=AT + timedelta(minutes=1),
        terminal_quantity=Decimal("2"),
    ) is False
    assert terminal_evidence_covers(
        final_fill_at=AT + timedelta(minutes=2),
        final_quantity=Decimal("4"),
        terminal_at=AT + timedelta(minutes=3),
        terminal_quantity=Decimal("4"),
    ) is True


def test_failed_terminal_persist_is_retried_before_restart_restore() -> None:
    runtime = PaperExitRuntime(config())
    source = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="fill-1",
        venue="schwab",
    )
    runtime.add_mirror_fill(source)
    decision = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )[0]

    class _FailOnceStore:
        def __init__(self) -> None:
            self.calls = 0
            self.saved: list[PaperDecision] = []

        def append_decisions(self, decisions: list[PaperDecision]) -> int:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database unavailable")
            self.saved.extend(decisions)
            return len(decisions)

    store = _FailOnceStore()
    service = StrategyEngineService.__new__(StrategyEngineService)
    service.logger = logging.getLogger("paper-test")
    service.paper_polygon_runtime = None
    service.paper_exit_runtime = runtime
    service.paper_exit_store = store
    with pytest.raises(RuntimeError, match="database unavailable"):
        service._persist_paper_decisions([decision])
    assert runtime.pending_terminal_decisions() == [decision]
    assert service._persist_paper_decisions() == 1
    assert runtime.pending_terminal_decisions() == []

    restarted = PaperExitRuntime(config())
    restarted.add_mirror_fill(source)
    durable = store.saved[0]
    restarted.restore_exit(
        logical_id=durable.logical_id,
        observed_at=durable.observed_at,
        price=durable.price,
        reason=str(durable.detail["reason"]),
    )
    assert restarted.on_quote(
        symbol="TEST",
        bid=Decimal("9.00"),
        ask=Decimal("9.01"),
        observed_at=AT + timedelta(minutes=3),
    ) == []


def test_config_change_never_changes_an_open_window_and_survives_in_decision() -> None:
    runtime = PaperExitRuntime(config())
    first = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="fill-1",
        venue="schwab",
    )
    runtime.add_mirror_fill(first)
    next_config = PaperRuleConfig(
        UUID("22222222-2222-2222-2222-222222222222"),
        Decimal("7"),
        Decimal("10"),
        AT + timedelta(minutes=2),
    )
    runtime.update_config(next_config)
    decision = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=3),
    )[0]
    assert decision.config_id == CONFIG_ID
    assert decision.detail["target_pct"] == "5"
    second = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="fill-2",
        venue="webull",
        slot="slot-2",
        at=AT + timedelta(minutes=4),
    )
    assert runtime.add_mirror_fill(second)[0].config_id == next_config.id


@pytest.mark.parametrize(
    ("live", "matched", "missed", "phantom", "expected"),
    [
        (0, 0, 0, 0, "UNEXERCISED"),
        (1, 0, 1, 0, "FAIL"),
        (0, 0, 0, 1, "FAIL"),
        (2, 2, 0, 0, "PASS"),
    ],
)
def test_mirror_acceptance_uses_the_live_denominator(
    live: int, matched: int, missed: int, phantom: int, expected: str
) -> None:
    assert mirror_acceptance(live=live, matched=matched, missed=missed, phantom=phantom) == expected


@pytest.mark.parametrize(
    ("coupling", "complete", "matched", "terminal", "expected"),
    [
        ("UNEXERCISED", True, 0, 0, "UNEXERCISED"),
        ("FAIL", False, 1, 0, "FAIL"),
        ("PASS", False, 1, 1, "IN_PROGRESS"),
        ("PASS", True, 1, 0, "IN_PROGRESS"),
        ("PASS", True, 1, 1, "PASS"),
    ],
)
def test_first_session_acceptance_requires_close_and_terminal_evidence(
    coupling: str, complete: bool, matched: int, terminal: int, expected: str
) -> None:
    assert (
        completed_session_acceptance(
            coupling_verdict=coupling,
            session_complete=complete,
            matched=matched,
            terminal=terminal,
        )
        == expected
    )


def test_paper_module_has_no_order_or_broker_imports() -> None:
    source = Path("src/project_mai_tai/paper_exit.py").read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("broker_adapter" in name for name in imports)
    assert "TradeIntentEvent" not in source
    assert "TradeIntentPayload" not in source


def test_installed_paper_runtime_is_not_a_trading_runtime_and_hides_order_apis() -> None:
    assert not issubclass(PaperPolygonRuntimeAdapter, StrategyBotRuntime)
    adapter = PaperPolygonRuntimeAdapter(  # type: ignore[arg-type]
        object(), PaperExitRuntime(config()), now_provider=lambda: AT
    )
    assert not hasattr(adapter, "positions")
    assert not hasattr(adapter, "exit_engine")
    assert not hasattr(adapter, "emergency_close_for_data_halt")


def test_paper_adapter_consumes_a_legacy_intent_before_any_outer_refusal() -> None:
    class _MaliciousLegacyRuntime:
        def handle_trade_tick(self, *args: object, **kwargs: object) -> list[TradeIntentEvent]:
            del args, kwargs
            return [_polygon_intent()]

    adapter = PaperPolygonRuntimeAdapter(  # type: ignore[arg-type]
        _MaliciousLegacyRuntime(), PaperExitRuntime(config()), now_provider=lambda: AT
    )
    assert adapter.handle_trade_tick("TEST", 10.0, 1, None, None) == []


class _NoPublishRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def xadd(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1


def _polygon_intent() -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="test",
        payload=TradeIntentPayload(
            strategy_code="polygon_30s",
            broker_account_name="live:anything",
            symbol="TEST",
            side="buy",
            quantity=Decimal("1"),
            intent_type="open",
            reason="mutant",
            metadata={},
        ),
    )


def _v2_resting_intent(
    *,
    account: str = "live:schwab_1m_v2",
    slot: str = "slot-1",
    entry_slot: str = "first",
    source: str = "rth_resting",
) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-v2-test",
        produced_at=AT,
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol="TEST",
            side="buy",
            quantity=Decimal("2"),
            intent_type="open",
            reason="stamped resting attempt",
            metadata={
                "cw_entry_slot": entry_slot,
                "fanout_source": source,
                "fanout_slot_id": slot,
                "cw_flip_level": "10.25",
            },
        ),
    )


@pytest.mark.asyncio
async def test_strategy_engine_refuses_polygon_intent_before_redis() -> None:
    service = StrategyEngineService.__new__(StrategyEngineService)
    service.redis = _NoPublishRedis()
    service.logger = logging.getLogger("paper-test")
    await service._publish_intent(_polygon_intent())
    assert service.redis.calls == 0


@pytest.mark.asyncio
async def test_oms_refuses_polygon_intent_before_database_or_broker() -> None:
    service = OmsRiskService.__new__(OmsRiskService)
    service.logger = logging.getLogger("paper-test")
    service.session_factory = lambda: (_ for _ in ()).throw(AssertionError("DB reached"))
    service.broker_adapter = object()
    assert await service.process_trade_intent(_polygon_intent()) == []


def test_retired_polygon_entry_rules_cannot_construct_an_intent() -> None:
    engine = PaperDisabledPolygonEntryEngine(TradingConfig())
    assert engine.check_entry("TEST", {"price": 10.0}, 1, object()) is None


@pytest.mark.asyncio
async def test_independent_arm_uses_stamped_v2_resting_attempt_and_collapses_venues() -> None:
    service = StrategyEngineService.__new__(StrategyEngineService)
    service.logger = logging.getLogger("paper-test")
    service.paper_exit_runtime = PaperExitRuntime(config())
    service.paper_polygon_runtime = None
    service.paper_exit_store = None
    first = _v2_resting_intent()
    duplicate = _v2_resting_intent(account="live:orb")

    await service._handle_stream_message("test:strategy-intents", {"data": first.model_dump_json()})
    await service._handle_stream_message(
        "test:strategy-intents", {"data": duplicate.model_dump_json()}
    )

    summary = service.paper_exit_runtime.summary()
    assert summary["pending_open_symbols"] == ["TEST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_slot", "source"),
    [("reclaim", "rth_resting"), ("first", "reactive"), ("first", "unknown")],
)
async def test_independent_arm_refuses_reclaim_and_unknown_sources(
    entry_slot: str, source: str
) -> None:
    service = StrategyEngineService.__new__(StrategyEngineService)
    service.logger = logging.getLogger("paper-test")
    service.paper_exit_runtime = PaperExitRuntime(config())
    service.paper_polygon_runtime = None
    service.paper_exit_store = None
    event = _v2_resting_intent(entry_slot=entry_slot, source=source)

    await service._handle_stream_message("test:strategy-intents", {"data": event.model_dump_json()})

    assert service.paper_exit_runtime.summary()["pending_open_symbols"] == []


def test_append_only_config_and_decision_tape_are_durable() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = PaperExitStore(factory)
    first = store.append_config(
        target_pct=Decimal("5"),
        stop_pct=Decimal("8"),
        effective_at=AT,
        changed_by="operator",
    )
    second = store.append_config(
        target_pct=Decimal("6"),
        stop_pct=Decimal("9"),
        effective_at=AT + timedelta(minutes=5),
        changed_by="operator",
    )
    runtime = PaperExitRuntime(first)
    runtime.update_config(second)
    decision = runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
            at=AT + timedelta(minutes=6),
        )
    )
    assert decision[0].config_id == second.id
    assert store.append_decisions(decision) == 1
    assert store.append_decisions(decision) == 0
    with factory() as session:
        assert len(list(session.scalars(select(PaperExitRuleConfig)))) == 2
        assert len(list(session.scalars(select(PaperExitEvent)))) == 1


def test_entry_assumptions_keep_modelled_and_actual_fills_separate() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    schwab = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="schwab-fill",
        venue="schwab",
        quantity="2",
        price="10",
    )
    webull = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="webull-fill",
        venue="webull",
        quantity="1",
        price="11",
        at=AT + timedelta(minutes=1, seconds=2),
    )
    with factory() as session:
        session.add_all(
            [
                PaperExitEvent(
                    event_key="independent-entry-matched",
                    logical_id="independent:slot-1",
                    arm="independent",
                    event_type="INDEPENDENT_ENTRY",
                    session_date=AT.date(),
                    symbol="TEST",
                    venue="modelled",
                    observed_at=AT + timedelta(seconds=30),
                    price=Decimal("10.5"),
                    quantity=Decimal("1"),
                    payload={"independent_attempt_id": "slot-1"},
                ),
                PaperExitEvent(
                    event_key="independent-entry-only",
                    logical_id="independent:slot-2",
                    arm="independent",
                    event_type="INDEPENDENT_ENTRY",
                    session_date=AT.date(),
                    symbol="OTHER",
                    venue="modelled",
                    observed_at=AT + timedelta(minutes=2),
                    price=Decimal("4.2"),
                    quantity=Decimal("1"),
                    payload={"independent_attempt_id": "slot-2"},
                ),
            ]
        )
        session.commit()

    rows = PaperExitStore(factory).entry_assumption_rows(
        start=AT,
        end=AT + timedelta(hours=1),
        source_fills=[schwab, webull],
    )

    matched = next(row for row in rows if row["fanout_slot_id"] == "slot-1")
    assert matched["status"] == "MATCHED_ASSUMPTION"
    assert matched["mirror_venues"] == ["schwab", "webull"]
    assert matched["mirror_legs"] == 2
    assert Decimal(str(matched["mirror_fill_price"])) == Decimal(31) / Decimal(3)
    assert Decimal(str(matched["independent_assumed_fill"])) == Decimal("10.5")
    assert Decimal(str(matched["assumed_vs_actual_pct"])) > 0

    independent_only = next(row for row in rows if row["fanout_slot_id"] == "slot-2")
    assert independent_only["status"] == "INDEPENDENT_ONLY"
    assert independent_only["mirror_fill_price"] == ""
    assert independent_only["independent_assumed_fill"] == "4.20000000"


def test_authoritative_fill_census_reads_both_live_venues_and_collapses_the_slot() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        schwab = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="live")
        webull = BrokerAccount(name="live:v2_webull", provider="webull", environment="live")
        session.add_all([strategy, schwab, webull])
        session.flush()
        for index, (account, source) in enumerate(
            ((schwab, "CW-v2-resting"), (webull, "rth_resting_mirror")),
            start=1,
        ):
            metadata = {
                "cw_entry_slot": "first",
                "fanout_slot_id": "shared-slot",
                "fanout_source": source,
                "resting_entry": "true",
            }
            intent = TradeIntent(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                symbol="TEST",
                side="buy",
                intent_type="open",
                quantity=Decimal("2"),
                reason="resting",
                status="filled",
                payload={"metadata": metadata},
            )
            session.add(intent)
            session.flush()
            order = BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"order-{index}",
                broker_order_id=f"broker-order-{index}",
                symbol="TEST",
                side="buy",
                order_type="stop_limit",
                time_in_force="day",
                quantity=Decimal("2"),
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
                    quantity=Decimal("2"),
                    price=Decimal("10"),
                    filled_at=AT + timedelta(minutes=index),
                    payload={"metadata": metadata},
                )
            )
        session.commit()
    fills, refused = PaperExitStore(factory).live_resting_fills(
        start=AT,
        end=AT + timedelta(hours=1),
    )
    assert refused == []
    assert {item.venue for item in fills} == {"schwab", "webull"}
    assert len({logical_mirror_id(item) for item in fills}) == 1

    logical_id = logical_mirror_id(fills[0])
    with factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        accounts = list(session.scalars(select(BrokerAccount)))
        assert strategy is not None
        for index, (account, price) in enumerate(zip(accounts, ("10.2", "10.4")), start=10):
            order = BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"exit-order-{index}",
                broker_order_id=f"exit-broker-order-{index}",
                symbol="TEST",
                side="sell",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("2"),
                status="filled",
                payload={},
            )
            session.add(order)
            session.flush()
            session.add(
                Fill(
                    order_id=order.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    broker_fill_id=f"exit-fill-{index}",
                    symbol="TEST",
                    side="sell",
                    quantity=Decimal("2"),
                    price=Decimal(price),
                    filled_at=AT + timedelta(minutes=10),
                    payload={},
                )
            )
        session.add(
            PaperExitEvent(
                event_key="paper-exit-grade",
                logical_id=logical_id,
                arm="mirror",
                event_type="PAPER_EXIT",
                session_date=AT.date(),
                symbol="TEST",
                venue="both",
                observed_at=AT + timedelta(minutes=9),
                price=Decimal("10.5"),
                quantity=Decimal("4"),
                payload={"reason": "TARGET"},
            )
        )
        session.commit()
    grades = PaperExitStore(factory).mirror_grades(
        start=AT, end=AT + timedelta(hours=1), source_fills=fills
    )
    assert grades[0]["gradable"] is True
    assert Decimal(str(grades[0]["paper_pct"])) == Decimal("5.00")
    assert Decimal(str(grades[0]["real_pct"])) == Decimal("3.00")

    with factory() as session:
        paper = session.scalar(
            select(PaperExitEvent).where(PaperExitEvent.event_key == "paper-exit-grade")
        )
        assert paper is not None
        paper.observed_at = AT + timedelta(minutes=1, seconds=30)
        session.commit()
    predates = PaperExitStore(factory).mirror_grades(
        start=AT, end=AT + timedelta(hours=1), source_fills=fills
    )[0]
    assert predates["gradable"] is False
    assert predates["reason"] == "paper exit predates a collapsed source leg"

    with factory() as session:
        paper = session.scalar(
            select(PaperExitEvent).where(PaperExitEvent.event_key == "paper-exit-grade")
        )
        assert paper is not None
        paper.observed_at = AT + timedelta(minutes=9)
        paper.quantity = Decimal("3")
        session.commit()
    wrong_quantity = PaperExitStore(factory).mirror_grades(
        start=AT, end=AT + timedelta(hours=1), source_fills=fills
    )[0]
    assert wrong_quantity["gradable"] is False
    assert wrong_quantity["reason"] == "paper exit quantity mismatch (3.00000000/4.00000000)"


def test_malformed_first_slot_fill_is_counted_as_unanswerable_not_dropped() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        account = BrokerAccount(name="live:v2_webull", provider="webull", environment="live")
        session.add_all([strategy, account])
        session.flush()
        order = BrokerOrder(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            client_order_id="malformed-order",
            broker_order_id="malformed-broker-order",
            symbol="TEST",
            side="buy",
            order_type="stop_limit",
            time_in_force="day",
            quantity=Decimal("1"),
            status="filled",
            payload={"metadata": {"cw_entry_slot": "first"}},
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                broker_fill_id="malformed-fill",
                symbol="TEST",
                side="buy",
                quantity=Decimal("1"),
                price=Decimal("10"),
                filled_at=AT + timedelta(minutes=1),
                payload={"metadata": {}},
            )
        )
        session.commit()

    fills, refused = PaperExitStore(factory).live_resting_fills(
        start=AT, end=AT + timedelta(hours=1)
    )
    assert fills == []
    assert [decision.event_type for decision in refused] == ["UNANSWERABLE"]


def test_quote_less_close_is_unanswerable_not_silently_open_or_passed() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
            at=datetime(2026, 9, 2, 15, 0, tzinfo=UTC),
        )
    )
    decisions = runtime.on_clock(datetime(2026, 9, 2, 20, 1, tzinfo=UTC))
    assert decisions[0].event_type == "UNANSWERABLE"
    assert decisions[0].price is None
