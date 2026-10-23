"""Source-authored controls; reviewer trade values are not native replay receipts."""

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momentum_live.handoff import LiveHandoff
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MomentumTapeRecord


ET = ZoneInfo("America/New_York")
TRADE_CASES = [
    ("FUFU", "2026-10-16", "15:44", "0.79", "1.31"),
    ("EYPT", "2026-10-19", "07:47", "0.7597", "2.99"),
    ("EYPT", "2026-10-19", "07:49", "0.8355", "2.99"),
    ("CRMT", "2026-10-20", "15:02", "0.8937", "1.07"),
]


def target(broker="webull", *, enabled=True):
    routing = []

    def route(symbol, now):
        routing.append((symbol, now))
        return SimpleNamespace(broker=broker, reason="controlled_route")

    result = LiveHandoff(AsyncMock(), SimpleNamespace(momentum_live_enabled=enabled), route_for=route)
    result.ledger.ready = True
    return result, routing


def record(symbol="TEST", price="5.25", prior="5", *, day="2026-10-22",
           clock="08:19", strategy="momentum_30s_warn"):
    stamp = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=ET)
    observed = int(stamp.timestamp()) * 1_000_000_000
    end = observed // 1_000_000
    payload = {"prior_close": prior, "observed_decision_ns": observed,
               "reference": {"end_ms": end - 1000},
               "detect": {"end_ms": end, "price": price}}
    return MomentumTapeRecord("event", f"{symbol}-{clock}", strategy, "DETECTED",
                              stamp.date(), symbol, None, payload)


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("strategy", ["momentum_30s_warn", "momentum_60s_warn"])
@pytest.mark.parametrize("symbol,day,clock,price,prior", TRADE_CASES)
def test_reviewer_down_day_shapes_refuse_before_route_or_claim(
        broker, strategy, symbol, day, clock, price, prior, caplog):
    handoff, routing = target(broker)
    row = record(symbol, price, prior, day=day, clock=clock, strategy=strategy)
    original = deepcopy(row.payload)
    with caplog.at_level("INFO", logger="momentum-live"):
        assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "momneg1_down_day"
    assert routing == [] and not handoff.ledger.episodes and handoff.queue.empty()
    assert row.payload == original
    assert f"[MOMNEG1-REFUSED] symbol={symbol} price={price} prior_close={prior}" in caplog.text
    handoff.redis.xadd.assert_not_awaited()


@pytest.mark.parametrize("prior", [None, "bad", "NaN", "Infinity", "-Infinity", "0", "-1"])
def test_unknown_invalid_prior_close_is_not_permission(prior):
    handoff, routing = target()
    row = record(prior=prior)
    assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "momneg1_prior_close_unknown"
    assert not routing and not handoff.ledger.episodes and handoff.queue.empty()


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("clock", ["08:19", "10:19"])
@pytest.mark.parametrize("price,prior", [("5.25", "5"), ("5.25", "5.25"), ("1.20", "0.90")])
def test_up_equal_and_prior_subdollar_keep_existing_live_admission(broker, clock, price, prior):
    # GIXI price/time shape, synthetic prior: NOT its missing actual prior close.
    handoff, routing = target(broker)
    row = record("GIXI", price, prior, clock=clock)
    assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "offered"
    assert len(routing) == 1 and handoff.queue.qsize() == 1
    assert handoff.queue._queue[0][1].country_route == broker


@pytest.mark.parametrize("event_type", ["FILLED", "FINAL", "NO_FILL"])
def test_nonentry_paper_records_not_subject_to_live_rule(event_type):
    from dataclasses import replace
    handoff, routing = target()
    row = replace(record(price="0.97", prior=None), event_type=event_type)
    assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "not_live_signal"
    assert not routing and not handoff.ledger.episodes


def test_disabled_live_does_not_touch_paper_payload():
    handoff, routing = target(enabled=False)
    row = record(price="0.79", prior="1.31")
    original = deepcopy(row.payload)
    assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "disabled"
    assert row.payload == original and not routing


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [8, 10])
def test_real_engine_in_memory_prior_is_sealed_paper_detection_unchanged(broker, hour):
    stamp = datetime(2026, 10, 22, hour, 19, tzinfo=ET).astimezone(UTC)
    end = int(stamp.timestamp()) * 1000
    engine = MomentumPaperEngine(prior_closes={"TEST": Decimal("2.99")},
        condition_version="controlled", coverage_started_ms=end - 120000,
        strategy_windows={"momentum_30s_warn": 30})
    records = engine.detect_from_bucket(strategy_code="momentum_30s_warn", symbol="TEST",
        reference_price=Decimal("1"), reference_start_ms=end - 20000,
        reference_end_ms=end - 10000, signal_price=Decimal("1.20"),
        signal_start_ms=end - 1000, signal_end_ms=end, observed_decision_ns=end * 1000000,
        aggregate_received_utc_ns=end * 1000000, aggregate_published_utc_ns=end * 1000000)
    assert len(records) == 1 and records[0].payload["prior_close"] == "2.99"
    before = deepcopy(engine.active_events)
    handoff, routing = target(broker)
    assert handoff.offer(records[0], day_volume=50000, now_ns=end * 1000000) == "momneg1_down_day"
    assert engine.active_events == before and not routing and not handoff.ledger.episodes


def test_missing_prior_key_refuses_and_valid_signal_keeps_volume_guard():
    handoff, _ = target()
    row = record()
    del row.payload["prior_close"]
    assert handoff.offer(row, day_volume=50000, now_ns=row.payload["observed_decision_ns"]) == "momneg1_prior_close_unknown"
    row = record()
    assert handoff.offer(row, day_volume=None, now_ns=row.payload["observed_decision_ns"]) == "day_volume_unknown"
    assert not handoff.ledger.episodes


@pytest.mark.parametrize("guard,expected", [
    ("restore", "ownership_restore_pending"),
    ("stale", "sealed_signal_future_or_stale"),
    ("volume", "day_volume_below_50000"),
    ("window", "entry_window_closed"),
])
def test_composed_up_day_preserves_existing_admission_guards(guard, expected):
    handoff, _ = target()
    row = record()
    now_ns = row.payload["observed_decision_ns"]
    if guard == "restore":
        handoff.ledger.ready = False
    if guard == "stale":
        now_ns += 2_000_000_001
    if guard == "window":
        handoff.route_for = lambda symbol, now: SimpleNamespace(broker=None)
    assert handoff.offer(row, day_volume=49999 if guard == "volume" else 50000,
                         now_ns=now_ns) == expected
    assert handoff.queue.empty() and not handoff.ledger.episodes


def test_composed_up_day_preserves_one_episode_and_duplicate_identity():
    handoff, _ = target()
    row = record()
    now_ns = row.payload["observed_decision_ns"]
    assert handoff.offer(row, day_volume=50000, now_ns=now_ns) == "offered"
    assert handoff.offer(row, day_volume=50000, now_ns=now_ns) == "duplicate_move"
    assert len(handoff.ledger.episodes) == 1 and handoff.queue.qsize() == 1
