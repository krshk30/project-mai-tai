from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
    mirrored_fill_classification,
    mirror_acceptance,
    resting_fill_classification,
    terminal_evidence_covers,
)
from project_mai_tai.paper_exit_store import (
    PAPER_EXIT_EVIDENCE_CUTOVER_SHA,
    PaperExitStore,
)
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
    entry_slot: str = "first",
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
        entry_slot=entry_slot,  # type: ignore[arg-type]
        source="rth_resting_mirror" if venue == "webull" else "cw-v2-resting",
    )


def release_candidate_after_print(
    runtime: PaperExitRuntime,
    *,
    bid: Decimal,
    ask: Decimal,
    observed_at: datetime,
) -> PaperDecision:
    """Stage an uncertain quote, then prove the short gap ended and use the next quote."""
    assert runtime.on_quote(
        symbol="TEST", bid=bid, ask=ask, observed_at=observed_at
    ) == []
    runtime.on_trade(symbol="TEST", observed_at=observed_at + timedelta(milliseconds=1))
    decisions = runtime.on_quote(
        symbol="TEST",
        bid=bid,
        ask=ask,
        observed_at=observed_at + timedelta(milliseconds=2),
    )
    return next(decision for decision in decisions if decision.event_type == "PAPER_EXIT")


@pytest.mark.parametrize(
    "metadata",
    [
        {"cw_entry_slot": "first", "atr_variant": "CW-v2-resting", "resting_entry": "true"},
        {"cw_entry_slot": "first", "fanout_source": "rth_resting"},
        {"cw_entry_slot": "first", "fanout_source": "rth_resting_mirror"},
        {"cw_entry_slot": "first", "fanout_source": "eh_resting"},
        {
            "cw_entry_slot": "first",
            "fanout_source": "eh_resting",
            "atr_variant": "CW-v2-fanout",
        },
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


@pytest.mark.parametrize(
    "metadata",
    [
        {"cw_entry_slot": "reclaim", "fanout_source": "reactive"},
        {"cw_entry_slot": "reclaim", "atr_variant": "CW-v2"},
        {"cw_entry_slot": "reclaim", "fanout_source": "rth_resting"},
        {
            "cw_entry_slot": "reclaim",
            "atr_variant": "CW-v2-resting",
            "resting_entry": "true",
        },
    ],
)
def test_mirror_accepts_each_live_reclaim_shape(metadata: dict[str, str]) -> None:
    assert mirrored_fill_classification(metadata)[0] is True


def test_mirror_rejects_unstamped_or_conflicting_reclaim() -> None:
    assert mirrored_fill_classification({"cw_entry_slot": "reclaim"})[0] is False
    assert mirrored_fill_classification(
        {"cw_entry_slot": "reclaim", "fanout_source": "unknown"}
    )[0] is False


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


def test_first_and_reclaim_are_distinct_even_when_live_reuses_the_slot_id() -> None:
    first = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="first-fill",
        venue="schwab",
        entry_slot="first",
    )
    reclaim = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="reclaim-fill",
        venue="schwab",
        entry_slot="reclaim",
        at=AT + timedelta(minutes=2),
    )
    assert logical_mirror_id(first) != logical_mirror_id(reclaim)
    runtime = PaperExitRuntime(config())
    assert runtime.add_mirror_fill(first)[0].event_type == "MIRROR_ENTRY"
    assert runtime.add_mirror_fill(reclaim)[0].event_type == "MIRROR_ENTRY"
    assert runtime.summary()["paper_exit"]["mirror_open"] == 2  # type: ignore[index]


def test_reclaim_legs_from_both_venues_collapse_to_one_position() -> None:
    schwab = fill(
        fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        broker_fill_id="schwab-reclaim",
        venue="schwab",
        entry_slot="reclaim",
    )
    webull = fill(
        fill_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        broker_fill_id="webull-reclaim",
        venue="webull",
        entry_slot="reclaim",
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
    runtime.on_atr_sell(symbol="TEST", observed_at=AT + timedelta(minutes=2))
    decision = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )
    assert decision.detail["reason"] == "TARGET"


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
    decision = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2, seconds=1),
    )
    assert decision.detail["reason"] == "ATR_SELL"


def test_confirmed_halt_suppresses_target_until_first_quote_after_reopen() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    last_print = AT + timedelta(minutes=1)
    runtime.on_trade(symbol="TEST", observed_at=last_print)

    assert runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=last_print + timedelta(seconds=1),
    ) == []
    confirmed = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.60"),
        ask=Decimal("10.61"),
        observed_at=last_print + timedelta(seconds=285),
    )

    assert [decision.event_type for decision in confirmed] == [
        "HALT_CONFIRMED",
        "HALT_TRIGGER_SUPPRESSED",
    ]
    assert all(decision.event_type != "PAPER_EXIT" for decision in confirmed)

    reopen = last_print + timedelta(minutes=5)
    runtime.on_trade(symbol="TEST", observed_at=reopen)
    released = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("9.75"),
        ask=Decimal("9.76"),
        observed_at=reopen + timedelta(milliseconds=1),
    )
    exit_decision = next(
        decision for decision in released if decision.event_type == "PAPER_EXIT"
    )
    assert exit_decision.detail["reason"] == "TARGET"
    assert exit_decision.price == Decimal("9.75")
    assert runtime.summary()["paper_exit"]["halt_suppression"] == {
        "status": "MEASURED",
        "suppressed_triggers": 1,
        "confirmed_halts": 1,
        "denominator": 1,
    }


@pytest.mark.parametrize(
    ("reason", "bid"),
    [("HARD_STOP", Decimal("9.20")), ("ATR_SELL", Decimal("10.00"))],
)
def test_stop_and_flip_cannot_exit_inside_a_confirmed_halt(
    reason: str, bid: Decimal
) -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    last_print = AT + timedelta(minutes=1)
    runtime.on_trade(symbol="TEST", observed_at=last_print)
    runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=last_print + timedelta(seconds=1),
    )
    runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=last_print + timedelta(seconds=285),
    )
    trigger_at = last_print + timedelta(seconds=286)
    if reason == "ATR_SELL":
        decisions = runtime.on_atr_sell(symbol="TEST", observed_at=trigger_at)
    else:
        decisions = runtime.on_quote(
            symbol="TEST",
            bid=bid,
            ask=bid + Decimal("0.01"),
            observed_at=trigger_at,
        )

    assert [decision.event_type for decision in decisions] == [
        "HALT_TRIGGER_SUPPRESSED"
    ]
    assert decisions[0].detail["reason"] == reason
    assert runtime.summary()["paper_exit"]["mirror_open"] == 1


def test_suspected_halt_never_grants_a_fill_and_missing_reopen_is_unanswerable() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
            at=datetime(2026, 9, 2, 19, 50, tzinfo=UTC),
        )
    )
    runtime.on_trade(
        symbol="TEST", observed_at=datetime(2026, 9, 2, 19, 55, tzinfo=UTC)
    )
    assert runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=datetime(2026, 9, 2, 19, 55, 1, tzinfo=UTC),
    ) == []
    confirmed = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=datetime(2026, 9, 2, 19, 59, 45, tzinfo=UTC),
    )
    assert [decision.event_type for decision in confirmed] == ["HALT_CONFIRMED"]
    close_decisions = runtime.on_quote(
        symbol="TEST",
        bid=Decimal("10.00"),
        ask=Decimal("10.01"),
        observed_at=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
    )
    assert [decision.event_type for decision in close_decisions] == [
        "HALT_TRIGGER_SUPPRESSED"
    ]
    assert close_decisions[0].detail["reason"] == "16:00"

    decisions = runtime.on_clock(datetime(2026, 9, 2, 20, 1, tzinfo=UTC))

    assert [decision.event_type for decision in decisions] == ["UNANSWERABLE"]
    assert "no post-reopen quote" in str(decisions[0].detail["reason"])


def test_late_arriving_earlier_atr_sell_is_unanswerable_without_quote_replay() -> None:
    runtime = PaperExitRuntime(config())
    runtime.add_mirror_fill(
        fill(
            fill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            broker_fill_id="fill-1",
            venue="schwab",
        )
    )
    first = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=3),
    )
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
    decision = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )
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
    decision = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=2),
    )

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
    decision = release_candidate_after_print(
        runtime,
        bid=Decimal("10.50"),
        ask=Decimal("10.51"),
        observed_at=AT + timedelta(minutes=3),
    )
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
async def test_market_trade_event_time_reaches_the_live_halt_tracker() -> None:
    service = StrategyEngineService.__new__(StrategyEngineService)
    captured: list[datetime] = []

    class _Runtime:
        def on_trade(self, *, symbol: str, observed_at: datetime) -> list[PaperDecision]:
            assert symbol == "TEST"
            captured.append(observed_at)
            return []

    service.paper_exit_runtime = _Runtime()
    service.state = SimpleNamespace(handle_trade_tick=lambda **_kwargs: [])
    service._generic_market_data_strategy_codes = lambda _symbol: []

    async def _flush() -> None:
        return None

    service._flush_pending_persists = _flush
    service._persist_paper_decisions = lambda _decisions=None: 0
    event_at = AT + timedelta(seconds=7)
    payload = {
        "event_type": "trade_tick",
        "source_service": "market-data-gateway",
        "produced_at": (event_at + timedelta(seconds=10)).isoformat(),
        "payload": {
            "symbol": "TEST",
            "price": "10.00",
            "size": 100,
            "timestamp_ns": int(event_at.timestamp() * 1_000),
        },
    }

    await service._handle_stream_message("test:market-data", {"data": json.dumps(payload)})

    assert captured == [event_at]


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


def test_daily_grade_refuses_a_window_spanning_the_evidence_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    cutover = AT + timedelta(minutes=30)
    with factory() as session:
        session.add(
            PaperExitRuleConfig(
                id=CONFIG_ID,
                target_pct=Decimal("5"),
                stop_pct=Decimal("8"),
                effective_at=datetime(1970, 1, 1, tzinfo=UTC),
                changed_by="migration-initial-v1",
                created_at=cutover,
            )
        )
        session.commit()
    store = PaperExitStore(factory)

    def invalid_mixed_window(**_kwargs: object) -> list[dict[str, object]]:
        pytest.fail("a cutover-spanning report reached the P&L join")

    monkeypatch.setattr(store, "mirror_grades", invalid_mixed_window)
    report_window = store.report_window(
        start=cutover - timedelta(hours=1),
        end=cutover + timedelta(hours=1),
    )
    grade = store.daily_grade(report_window=report_window, source_fills=[])

    assert report_window.evidence_start == cutover
    assert grade["status"] == "REFUSED_SPANS_EVIDENCE_CUTOVER"
    assert grade["boundary_sha"] == PAPER_EXIT_EVIDENCE_CUTOVER_SHA
    assert grade["boundary_at"] == cutover.isoformat()
    assert grade["matched"] is None
    assert grade["total"] is None
    assert grade["paper_pct"] == ""
    assert grade["real_pct"] == ""
    assert grade["rows"] == []


def test_daily_grade_cannot_tell_when_cutover_record_is_missing() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = PaperExitStore(factory)

    report_window = store.report_window(start=AT, end=AT + timedelta(hours=1))
    grade = store.daily_grade(report_window=report_window, source_fills=[])

    assert report_window.evidence_start is None
    assert grade["status"] == "COULD_NOT_TELL"
    assert grade["matched"] is None
    assert grade["total"] is None


def test_daily_grade_preserves_post_cutover_denominator_and_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    cutover = AT - timedelta(hours=1)
    with factory() as session:
        session.add(
            PaperExitRuleConfig(
                id=CONFIG_ID,
                target_pct=Decimal("5"),
                stop_pct=Decimal("8"),
                effective_at=datetime(1970, 1, 1, tzinfo=UTC),
                changed_by="migration-initial-v1",
                created_at=cutover,
            )
        )
        session.commit()
    store = PaperExitStore(factory)
    rows = [
        {"gradable": True, "paper_pct": "5.25", "real_pct": "3.50"},
        {"gradable": False, "paper_pct": "", "real_pct": ""},
    ]
    monkeypatch.setattr(store, "mirror_grades", lambda **_kwargs: rows)

    report_window = store.report_window(start=AT, end=AT + timedelta(hours=1))
    grade = store.daily_grade(report_window=report_window, source_fills=[])

    assert grade["status"] == "READY"
    assert grade["matched"] == 1
    assert grade["total"] == 2
    assert grade["paper_pct"] == "5.25"
    assert grade["real_pct"] == "3.50"
    assert grade["rows"] == rows
    assert grade["halt_suppression"] == {
        "status": "UNEXERCISED",
        "suppressed_triggers": 0,
        "confirmed_halts": 0,
        "denominator": 0,
    }


def test_daily_halt_line_counts_confirmed_windows_and_suppressed_triggers() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = PaperExitStore(factory)
    store.append_decisions(
        [
            PaperDecision(
                event_key="HALT_CONFIRMED:TEST:1",
                logical_id="halt:TEST:1",
                arm="mirror",
                event_type="HALT_CONFIRMED",
                session_date=AT.date(),
                symbol="TEST",
                observed_at=AT,
                price=None,
                quantity=None,
                config_id=None,
            ),
            PaperDecision(
                event_key="HALT_TRIGGER_SUPPRESSED:TEST:1",
                logical_id="logical-1",
                arm="mirror",
                event_type="HALT_TRIGGER_SUPPRESSED",
                session_date=AT.date(),
                symbol="TEST",
                observed_at=AT + timedelta(seconds=1),
                price=None,
                quantity=Decimal("1"),
                config_id=None,
            ),
        ]
    )

    assert store.halt_suppression_grade(
        start=AT - timedelta(seconds=1), end=AT + timedelta(minutes=1)
    ) == {
        "status": "MEASURED",
        "suppressed_triggers": 1,
        "confirmed_halts": 1,
        "denominator": 1,
    }


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
        schwab = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        webull = BrokerAccount(name="live:orb", provider="webull", environment="production")
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


def test_authoritative_fill_census_keeps_reclaim_distinct_and_collapses_its_venues() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        schwab = BrokerAccount(
            name="live:schwab_1m_v2", provider="schwab", environment="production"
        )
        webull = BrokerAccount(name="live:orb", provider="webull", environment="production")
        session.add_all([strategy, schwab, webull])
        session.flush()
        legs = (
            (schwab, "first", "CW-v2-resting"),
            (webull, "first", "rth_resting_mirror"),
            (schwab, "reclaim", "reactive"),
            (webull, "reclaim", "reactive"),
        )
        for index, (account, entry_slot, source) in enumerate(legs, start=1):
            metadata = {
                "cw_entry_slot": entry_slot,
                "fanout_slot_id": "live-reused-slot",
                "fanout_source": source,
                "resting_entry": "true",
            }
            intent = TradeIntent(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                symbol="TEST",
                side="buy",
                intent_type="open",
                quantity=Decimal("1"),
                reason=entry_slot,
                status="filled",
                payload={"metadata": metadata},
            )
            session.add(intent)
            session.flush()
            order = BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"composition-order-{index}",
                broker_order_id=f"composition-broker-order-{index}",
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
                    broker_fill_id=f"composition-fill-{index}",
                    symbol="TEST",
                    side="buy",
                    quantity=Decimal("1"),
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
    assert len(fills) == 4
    assert {item.entry_slot for item in fills} == {"first", "reclaim"}
    grouped: dict[str, list[PaperSourceFill]] = {}
    for source_fill in fills:
        grouped.setdefault(logical_mirror_id(source_fill), []).append(source_fill)
    assert len(grouped) == 2
    assert {tuple(sorted(item.venue for item in group)) for group in grouped.values()} == {
        ("schwab", "webull")
    }


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
        account = BrokerAccount(name="live:orb", provider="webull", environment="production")
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
