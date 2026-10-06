import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, StrategyBarHistory, TradeIntent
from project_mai_tai.orb_schwab_atr_entry import (
    AtrEntryGate, completed_atr_entry_gate, schwab_atr_entry_gate,
)
from project_mai_tai.orb_schwab_exits import ATR_SOURCE, decode_bar
from project_mai_tai.orb_schwab_macd import MacdVerdict
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from tests.unit.test_orb_schwab_order_route import OPEN, _service

EVIDENCE = json.loads((Path(__file__).parents[1] / "fixtures/orbpurple1_production_20261006.json").read_text())
CASES = [
    ("NXL", "2026-10-01", 8.8, 7.215259490784897, True),
    ("AMOD", "2026-10-02", 3.47, 3.120682375318361, True),
    ("APUS", "2026-10-05", 5.07, 5.421986558524449, False),
    ("MI", "2026-10-05", 2.77, 2.467644000636459, True),
    ("XHG", "2026-10-06", 3.17, 2.746434587062273, True),
    ("JAGX", "2026-10-06", 6.55, 6.362537980756465, True),
]


@pytest.fixture
def history():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as session:
        for row in EVIDENCE["bars"]:
            bar = decode_bar(row)
            session.add(StrategyBarHistory(
                strategy_code="schwab_1m_v2", symbol=row["symbol"], interval_secs=60,
                bar_time=bar.timestamp, open_price=bar.open, high_price=bar.high,
                low_price=bar.low, close_price=bar.close, volume=int(bar.volume), source=row["source"],
            ))
    yield factory
    engine.dispose()


def _producer(monkeypatch, factory, symbol, day, *, enabled=True, observe=False):
    opening = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    service = OrbSchwabService(settings=Settings(
        orb_enabled=True, orb_live_schwab_orders_enabled=not observe,
        orb_schwab_observe_enabled=observe, orb_schwab_atr_entry_gate_enabled=enabled,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
    ), session_factory=factory)
    service._universe = {symbol}
    clock = [opening - timedelta(minutes=2) + timedelta(seconds=3)]
    monkeypatch.setattr(service, "_session_open_utc", lambda: opening)
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    emitted = []

    async def capture(_redis, _settings, event, _now):
        emitted.append(event)

    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", capture)
    for row in EVIDENCE["bars"]:
        bar = decode_bar(row)
        if row["symbol"] == symbol and opening - timedelta(minutes=5) <= bar.timestamp < opening - timedelta(minutes=2):
            service._on_bar(symbol, replace(bar, breakout_high=bar.high),
                            observed_at=bar.timestamp + timedelta(minutes=1, seconds=3))
    return service, clock, emitted


@pytest.mark.parametrize("symbol,day,close,line,allowed", CASES)
def test_all_recorded_placement_candidates_replay_through_live_gate(
    monkeypatch, history, symbol, day, close, line, allowed,
):
    service, clock, emitted = _producer(monkeypatch, history, symbol, day)
    gate = schwab_atr_entry_gate(history, symbol, clock[0])
    assert gate.close == close and gate.trail == pytest.approx(line)
    assert (gate.verdict == "allowed") is allowed
    assert gate.evidence()["source"] == ATR_SOURCE
    assert gate.bar_at == clock[0].replace(second=0) - timedelta(minutes=1)
    asyncio.run(service._process_closed_bars())
    assert [event.payload.intent_type for event in emitted] == (["open"] if allowed else [])
    assert service._opening_orders[symbol].cancelled is not allowed
    if allowed:
        metadata = emitted[0].payload.metadata
        assert metadata["native_oco_bracket"] == "true"
        assert metadata["orb_target_pct"] == "5" and metadata["orb_stop_pct"] == "8"
        assert emitted[0].payload.quantity == 2
        assert emitted[0].payload.broker_account_name == "live:schwab_1m_v2"
    assert not service._paper_positions and not service._pending_paper_entries


def test_filled_entry_population_and_actual_realized_outcomes_are_complete():
    buys = [row for row in EVIDENCE["orders"] if row["side"] == "buy" and row["fills"]]
    assert [(row["symbol"], row["first_fill"]) for row in buys] == [
        ("MI", "2026-10-05T13:30:22+00:00"), ("JAGX", "2026-10-06T13:30:14+00:00"),
    ]
    for buy, pnl in zip(buys, [-0.4996, -0.1398], strict=True):
        sell = next(row for row in EVIDENCE["orders"] if row["symbol"] == buy["symbol"] and row["side"] == "sell")
        entry, exit_fill = buy["fills"][0], sell["fills"][0]
        assert entry["qty"] == exit_fill["qty"] == 2
        assert (exit_fill["price"] - entry["price"]) * 2 == pytest.approx(pnl)


@pytest.mark.parametrize("enabled", [True, False])
def test_apus_purple_is_withheld_and_rollback_restores_one_buy(monkeypatch, history, enabled):
    service, clock, emitted = _producer(monkeypatch, history, "APUS", "2026-10-05", enabled=enabled)
    asyncio.run(service._process_closed_bars())
    assert len(emitted) == (0 if enabled else 1)
    if enabled:
        # Later bars turning long cannot retroactively create a withheld entry.
        clock[0] += timedelta(minutes=1)
        bar = OrbBar(timestamp=clock[0].replace(second=0) - timedelta(minutes=1),
                     open=6, high=6, low=6, close=6, volume=100, breakout_high=6)
        service._on_bar("APUS", bar)
        asyncio.run(service._process_closed_bars())
        assert not emitted


@pytest.mark.parametrize("status", ["missing_last_closed_schwab_bar", "schwab_bar_read_unavailable",
                                    "unrecognized_schwab_bar_provenance", "insufficient_schwab_atr_history"])
def test_unknown_atr_waits_then_withholds_without_paper_or_broker_entry(monkeypatch, history, status):
    service, clock, emitted = _producer(monkeypatch, history, "JAGX", "2026-10-06")
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_atr_entry_gate",
                        lambda *_args: AtrEntryGate("unknown", status))
    asyncio.run(service._process_closed_bars())
    assert not emitted and service._closed_bars
    assert not service._opening_orders["JAGX"].cancelled
    clock[0] = clock[0].replace(second=0) + timedelta(seconds=90)
    asyncio.run(service._process_closed_bars())
    assert not emitted and not service._closed_bars
    assert service._opening_orders["JAGX"].cancelled


def test_delayed_atr_bar_recovers_inside_existing_wait(monkeypatch, history):
    service, clock, emitted = _producer(monkeypatch, history, "JAGX", "2026-10-06")
    original = schwab_atr_entry_gate(history, "JAGX", clock[0])
    results = iter([AtrEntryGate("unknown", "missing_last_closed_schwab_bar"), original])
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_atr_entry_gate", lambda *_args: next(results))
    asyncio.run(service._process_closed_bars())
    clock[0] += timedelta(seconds=1)
    asyncio.run(service._process_closed_bars())
    assert len(emitted) == 1
    assert emitted[0].payload.metadata["orb_deferred_macd_bar_close"] == "2026-10-06T13:28:00+00:00"


def test_negative_macd_still_withholds_above_atr(monkeypatch, history):
    service, _clock, emitted = _producer(monkeypatch, history, "JAGX", "2026-10-06")
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_completed_bar_macd_gate",
                        lambda *_args: (MacdVerdict.NEGATIVE, "negative", -0.1))
    asyncio.run(service._process_closed_bars())
    assert not emitted and not service._opening_orders["JAGX"].placed


def test_observer_reports_purple_without_publishing(monkeypatch, history, caplog):
    caplog.set_level("INFO", logger="orb-schwab")
    service, _clock, emitted = _producer(monkeypatch, history, "APUS", "2026-10-05", observe=True)
    asyncio.run(service._process_closed_bars())
    assert not emitted and "atr_prior_close_under_atr" in caplog.text
    assert '"trail": 5.421986558524449' in caplog.text


def test_default_on_independent_from_paper_and_paper_service_is_unchanged(monkeypatch):
    settings = Settings()
    assert settings.orb_schwab_atr_entry_gate_enabled is True
    assert settings.orb_paper_atr_entry_gate_enabled is False
    monkeypatch.setenv("MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED", "false")
    assert Settings().orb_schwab_atr_entry_gate_enabled is False
    settings.orb_schwab_atr_entry_gate_enabled = True
    paper = OrbService(settings=settings)
    assert paper._paper_entry_gates_enabled() is False


@pytest.mark.parametrize("bars,status", [([], "complete"), ([], "schwab_bar_read_unavailable")])
def test_empty_or_unavailable_completed_history_never_passes(bars, status):
    assert completed_atr_entry_gate(bars, status, OPEN).verdict == "unknown"


@pytest.mark.parametrize("fault", ["forming", "stale", "unordered", "unwarmed"])
def test_bad_bar_evidence_never_authorizes_an_entry(fault):
    bars = [decode_bar(row) for row in EVIDENCE["bars"] if row["symbol"] == "JAGX"
            and datetime.fromisoformat(row["at"]) < datetime(2026, 10, 6, 13, 28, tzinfo=UTC)]
    now = datetime(2026, 10, 6, 13, 28, tzinfo=UTC)
    if fault == "forming":
        bars.insert(0, replace(bars[0], timestamp=now))
    elif fault == "stale":
        bars.pop()
    elif fault == "unordered":
        bars[0], bars[1] = bars[1], bars[0]
    else:
        bars = bars[-3:]
    assert completed_atr_entry_gate(bars, "complete", now).verdict == "unknown"


@pytest.mark.parametrize("verdict", ["below_line", "unknown"])
def test_oms_cannot_bypass_producer_atr_gate(monkeypatch, verdict):
    service, factory, broker = _service(monkeypatch)
    service.settings.orb_schwab_atr_entry_gate_enabled = True
    monkeypatch.setattr("project_mai_tai.oms.service.schwab_atr_entry_gate",
                        lambda *_args: AtrEntryGate(verdict, "prior_close_under_atr"))
    event = build_orb_schwab_open_intent(service.settings, "JAGX", Decimal("6.67"))
    assert asyncio.run(service.process_trade_intent(event)) == []
    assert not broker.submitted and not broker.previewed
    with factory() as session:
        assert session.scalar(select(BrokerOrder)) is None


def test_oms_rechecks_atr_after_preview_and_records_local_refusal(monkeypatch):
    service, factory, broker = _service(monkeypatch)
    service.settings.orb_schwab_atr_entry_gate_enabled = True
    results = iter([AtrEntryGate("allowed", "prior_close_at_or_above_atr"),
                    AtrEntryGate("below_line", "prior_close_under_atr")])
    monkeypatch.setattr("project_mai_tai.oms.service.schwab_atr_entry_gate", lambda *_args: next(results))
    event = build_orb_schwab_open_intent(service.settings, "JAGX", Decimal("6.67"))
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.previewed) == 1 and not broker.submitted
    with factory() as session:
        intent = session.scalar(select(TradeIntent))
        assert intent.status == "rejected"
        assert intent.payload["refusal_origin"] == "client_abort"
        assert intent.payload["refusal_code"] == "orb_schwab_atr_prior_close_under_atr"


def test_close_equal_to_atr_line_is_allowed():
    bars = [OrbBar(timestamp=OPEN - timedelta(minutes=9-i), open=10, high=10,
                   low=10, close=10, volume=100) for i in range(9)]
    result = completed_atr_entry_gate(bars, "complete", OPEN)
    assert result.close == result.trail == 10 and result.verdict == "allowed"


@pytest.mark.parametrize("symbol,day,allowed", [("MI", "2026-10-05", True),
                                               ("JAGX", "2026-10-06", True),
                                               ("APUS", "2026-10-05", False)])
def test_oms_real_schwab_history_passes_both_fills_and_blocks_purple(monkeypatch, tmp_path, symbol, day, allowed):
    service, _factory, broker = _service(monkeypatch)
    # Separate read/write connections mirror PostgreSQL transactions. StaticPool
    # would roll back the pending intent when the ATR reader closes its session.
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'oms.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    service.session_factory = factory
    service.settings.orb_schwab_atr_entry_gate_enabled = True
    now = datetime.fromisoformat(f"{day}T13:28:04+00:00")
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: now)
    with factory.begin() as session:
        for row in EVIDENCE["bars"]:
            if row["symbol"] != symbol:
                continue
            bar = decode_bar(row)
            session.add(StrategyBarHistory(
                strategy_code="schwab_1m_v2", symbol=symbol, interval_secs=60,
                bar_time=bar.timestamp, open_price=bar.open, high_price=bar.high,
                low_price=bar.low, close_price=bar.close, volume=int(bar.volume), source=row["source"],
            ))
    event = build_orb_schwab_open_intent(service.settings, symbol, Decimal("6.67"))
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.submitted) == int(allowed)
    assert len(broker.previewed) == int(allowed)
