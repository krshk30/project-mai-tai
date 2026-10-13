"""Controlled clocks and fake owner transport; no provider/socket/load benchmark."""

from dataclasses import replace
import asyncio
from datetime import UTC, datetime
from decimal import Decimal as D
import json
import logging
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MOMENTUM_STRATEGIES, MOMENTUM_WARNING_STRATEGIES
from project_mai_tai.momentum_paper.warning_window import CoverageEvidence
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot, _store
from tests.unit.test_momentum_warning import EPOCH, START, bar, consumer, trade


def admitted():
    model = consumer()
    item = replace(bar(high="11"), close=D("10"))
    outcome, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert outcome.status == "ADMITTED" and not records and not model.engine.active_symbols
    return model


def expire(model, offset, *, legacy_active=frozenset()):
    return model.expire_quiet(observed_ms=START+offset, legacy_active=set(legacy_active))


def test_exact_60s_no_data_is_expired_not_cooled_and_has_no_cooldown():
    model = admitted()
    assert not any(row.status == "DROPPED" for row in expire(model, 60_999))
    assert model.desired_symbols(set()) == {"TEST"}
    dropped = expire(model, 61_000)
    assert [(row.status, row.reason) for row in dropped] == [("DROPPED", "expired_no_data")]
    assert model.desired_symbols(set()) == set()
    assert model.last_status == "expired_no_data" and model.cooling_status == {"TEST": "expired_no_data"}
    assert "TEST" not in model.candidates._cooldowns
    assert not expire(model, 61_001)


def test_repeated_aggregate_warnings_do_not_extend_a_no_trade_lease():
    model = admitted()
    fresh = replace(bar(59, high="11"), close=D("10"), sequence=2)
    outcome, _ = model.observe_aggregate(fresh, observed_ns=fresh.published_utc_ns, legacy_active=set())
    assert outcome.status == "RETAINED"
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_000))


def test_eligible_callback_observation_not_sip_clock_resets_exact_60s():
    model = admitted()
    model.observe_trade(trade(59_000), sip_timestamp_ns=(START+59_000)*1_000_000,
                        observed_ns=(START+59_500)*1_000_000)
    assert not any(row.status == "DROPPED" for row in expire(model, 119_499))
    assert any(row.reason == "expired_no_data" for row in expire(model, 119_500))


@pytest.mark.parametrize("offset", [1001, 30_000, 59_999, 60_999])
def test_noneligible_prints_never_extend_admission_lease(offset):
    model = admitted()
    model.observe_trade(trade(offset, eligible=False), sip_timestamp_ns=(START+offset)*1_000_000,
                        observed_ns=(START+offset)*1_000_000)
    assert not model.engine.active_symbols
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_000))


@pytest.mark.parametrize("kind", ["future_trade", "stale_trade", "future_aggregate"])
def test_future_or_stale_evidence_cannot_refresh_no_data_lease(kind):
    model = admitted()
    if kind == "future_aggregate":
        future = replace(bar(80, high="11"), sequence=2)
        outcome, _ = model.observe_aggregate(future, observed_ns=(START+60_000)*1_000_000,
                                             legacy_active=set())
        assert outcome.reason == "aggregate_clock_or_stale"
    else:
        sip = START+59_000
        observed = sip-1 if kind == "future_trade" else sip+1001
        model.observe_trade(trade(59_000), sip_timestamp_ns=sip*1_000_000, observed_ns=observed*1_000_000)
        assert model.last_status == "raw_future_or_stale"
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_000))


@pytest.mark.parametrize("offset", [59_000, 58_500])
def test_duplicate_or_reordered_print_does_not_refresh_eligible_lease(offset):
    model = admitted()
    model.observe_trade(trade(59_000), sip_timestamp_ns=(START+59_000)*1_000_000,
                        observed_ns=(START+59_000)*1_000_000)
    model.observe_trade(trade(offset), sip_timestamp_ns=(START+offset)*1_000_000,
                        observed_ns=(START+59_500)*1_000_000)
    assert model.last_status == "raw_duplicate_or_out_of_order"
    assert any(row.reason == "expired_no_data" for row in expire(model, 119_000))


def test_expiry_clock_regression_does_not_release_or_use_future_evidence():
    model = admitted()
    # The new lease fails closed; existing cooling-window clock errors are unchanged.
    assert not model.candidates.expire_no_data(observed_ms=START+999, active_path_symbols=set())
    assert model.desired_symbols(set()) == {"TEST"}
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_000))


def ledger(code, *, state):
    seconds = {**MOMENTUM_STRATEGIES, **MOMENTUM_WARNING_STRATEGIES}[code]
    engine = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                                 coverage_started_ms=START, strategy_windows={code: seconds})
    engine.ingest(trade(59_000))
    detected = engine.ingest(trade(60_000, "12"))
    assert any(row.event_type == "DETECTED" for row in detected)
    if state == "OPEN":
        filled = engine.ingest(trade(60_001, "12.1"))
        assert any(row.event_type == "FILLED" for row in filled)
    assert engine.active_events[0]["status"] == state
    return engine


@pytest.mark.parametrize("code", [*MOMENTUM_STRATEGIES, *MOMENTUM_WARNING_STRATEGIES])
@pytest.mark.parametrize("state", ["WAITING_FILL", "OPEN"])
def test_pending_or_open_in_either_ledger_inhibits_expiry(code, state):
    model = admitted()
    engine = ledger(code, state=state)
    legacy = set()
    if code in MOMENTUM_WARNING_STRATEGIES:
        model.engine = engine
    else:
        legacy = engine.active_symbols
    assert not any(row.status == "DROPPED" for row in expire(model, 61_000, legacy_active=legacy))
    assert model.desired_symbols(legacy) == {"TEST"}
    # Once the path is resolved, no fabricated eligible observation is needed to release.
    engine.close_session(START+61_001)
    assert not engine.active_symbols
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_001))


def test_new_warning_readmits_and_cannot_bridge_raw_coverage_across_owner_gap():
    model = admitted()
    model._raw_first_ms["TEST"] = START-60_000
    model._raw_coverage["TEST"] = CoverageEvidence(EPOCH, START-60_000, START, 0, True)
    assert any(row.reason == "expired_no_data" for row in expire(model, 61_000))
    assert "TEST" not in model._raw_first_ms and "TEST" not in model._raw_coverage
    fresh = replace(bar(61, high="11"), close=D("10"), sequence=2)
    outcome, _ = model.observe_aggregate(fresh, observed_ns=fresh.published_utc_ns, legacy_active=set())
    assert outcome.status == "ADMITTED"
    stamp = START+62_001
    records = model.observe_trade(trade(62_001), sip_timestamp_ns=stamp*1_000_000,
        observed_ns=stamp*1_000_000, raw_coverage=CoverageEvidence(EPOCH, START-60_000, stamp, 0, True))
    assert model.last_status == "whole_bucket_cold_reference"
    assert model._raw_first_ms["TEST"] == stamp and not records


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [*MOMENTUM_STRATEGIES, *MOMENTUM_WARNING_STRATEGIES])
@pytest.mark.parametrize("state", ["WAITING_FILL", "OPEN"])
async def test_actual_tick_preserves_pending_or_open_both_ledgers(monkeypatch, code, state):
    redis = AsyncMock()
    service = MomentumPaperService(Settings(_env_file=None), redis_client=redis, store=_store())
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                                          coverage_started_ms=START)
    service._warning = admitted()
    engine = ledger(code, state=state)
    if code in MOMENTUM_WARNING_STRATEGIES:
        service._warning.engine = engine
    else:
        service._engine = engine
    now_ms = START+61_000
    service._clock = lambda: datetime.fromtimestamp(now_ms/1000, tz=UTC)
    service._session_date = service._clock().date()
    service._last_heartbeat = service._clock()
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ms*1_000_000)
    service._gateway_task = object()
    try:
        await service._tick()
    finally:
        service._gateway_task = None
    assert engine.active_events[0]["status"] == state
    assert service._warning_subscribed_symbols == {"TEST"}
    assert not any(reason == "expired_no_data" for reason in service._warning.cooling_status.values())


@pytest.mark.asyncio
async def test_actual_service_logs_expires_rewarns_and_preserves_other_owner_TQ(monkeypatch, caplog):
    from tests.unit.test_momwarn1_gateway import event, service as fake_gateway

    redis = AsyncMock()
    service = MomentumPaperService(Settings(_env_file=None), redis_client=redis, store=_store())
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                                          coverage_started_ms=START)
    service._warning = admitted()
    service._condition_snapshot = _condition_snapshot()
    service._session_date = datetime.fromtimestamp(START/1000, tz=UTC).date()
    now_ms = START+1000
    service._clock = lambda: datetime.fromtimestamp(now_ms/1000, tz=UTC)
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ms*1_000_000)
    gateway, provider, _, _ = fake_gateway()
    await gateway.apply_subscription_event(event("v2", ["TEST"]))
    await service._sync_gateway_subscriptions()

    async def apply_warning_requests():
        requests = [MarketDataSubscriptionEvent.model_validate_json(call.args[1]["data"])
                    for call in redis.xadd.await_args_list if call.args[0].endswith(":market-data-subscriptions")]
        for request in requests:
            if request.payload.consumer_name == "momentum-warning":
                await gateway.apply_subscription_event(request)
        return requests

    await apply_warning_requests()
    assert provider._warning_trade_symbols == {"TEST"}
    redis.reset_mock()
    service._gateway_task = object()  # Inert reader token: no socket/task is created.
    now_ms = START+61_000
    service._last_heartbeat = service._clock()
    try:
        with caplog.at_level(logging.INFO, logger="momentum-paper"):
            await service._tick()
    finally:
        service._gateway_task = None
    requests = await apply_warning_requests()
    assert [r.payload.symbols for r in requests if r.payload.consumer_name == "momentum-warning"] == [[]]
    assert provider._warning_trade_symbols == set()
    assert provider._subscriptions == {"TEST"} and gateway.active_symbols() == {"TEST"}
    assert "T.TEST" not in provider._ws.removed and "Q.TEST" not in provider._ws.removed
    assert service._warning_subscribed_symbols == set()
    assert any("reason=expired_no_data" in row.message for row in caplog.records)
    assert not any("cooled" in row.message for row in caplog.records)
    redis.reset_mock()
    fresh = replace(bar(61, high="11"), close=D("10"), sequence=2)
    now_ms = fresh.end_ms
    await service._handle_warning_event(json.dumps({**fresh.__dict__, "version": 1, "event_type": "aggregate"}, default=str))
    requests = await apply_warning_requests()
    assert [r.payload.symbols for r in requests if r.payload.consumer_name == "momentum-warning"] == [["TEST"]]
    assert provider._warning_trade_symbols == {"TEST"} and provider._subscriptions == {"TEST"}


@pytest.mark.asyncio
async def test_rewarning_during_pending_expiry_write_is_last_owner_replacement(monkeypatch):
    redis = AsyncMock()
    service = MomentumPaperService(Settings(_env_file=None), redis_client=redis, store=_store())
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                                          coverage_started_ms=START)
    service._warning = admitted()
    now_ms = START+1000
    service._clock = lambda: datetime.fromtimestamp(now_ms/1000, tz=UTC)
    service._session_date = service._clock().date()
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ms*1_000_000)
    await service._sync_gateway_subscriptions()
    redis.reset_mock()
    release_started, release_finish, rewarning_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    completed_replacements = []

    async def write(stream, fields, **_kwargs):
        data = json.loads(fields["data"])
        if stream.endswith(":market-data-subscriptions"):
            payload = data["payload"]
            if payload["consumer_name"] == "momentum-warning":
                if payload["symbols"] == []:
                    release_started.set()
                    await release_finish.wait()
                completed_replacements.append(payload["symbols"])
        elif data.get("event_type") == "decision" and data.get("status") == "ADMITTED":
            rewarning_seen.set()
        return "CONTROLLED-redis-id"

    redis.xadd.side_effect = write
    service._gateway_task = object()
    now_ms = START+61_000
    service._last_heartbeat = service._clock()
    expiry = asyncio.create_task(service._tick())
    rewarning = None
    try:
        await asyncio.wait_for(release_started.wait(), 1)
        fresh = replace(bar(61, high="11"), close=D("10"), sequence=2)
        now_ms = fresh.end_ms
        rewarning = asyncio.create_task(service._handle_warning_event(
            json.dumps({**fresh.__dict__, "version": 1, "event_type": "aggregate"}, default=str)))
        await asyncio.wait_for(rewarning_seen.wait(), 1)
        await asyncio.sleep(0)
        assert not rewarning.done() and completed_replacements == []
        release_finish.set()
        await asyncio.wait_for(asyncio.gather(expiry, rewarning), 1)
        assert completed_replacements == [[], ["TEST"]]
        assert service._warning_subscribed_symbols == {"TEST"}
        assert not service._warning_owner_uncertain
    finally:
        release_finish.set()
        for task in (expiry, rewarning):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (expiry, rewarning) if task is not None), return_exceptions=True)
        service._gateway_task = None
