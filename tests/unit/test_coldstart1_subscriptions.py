"""COLDSTART1 replay: recorded 10-03 empty owners, plus 10-01 live symbol sets.

The Redis/client scheduling is a simulation; fixture payloads are recorded bytes.
No snapshot-batches read or broker/network connection is used in these tests.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from tests.support.retired_orb_simulation import OrbService
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from tests.support.retired_orb_settings import RetiredOrbSettings as Settings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "coldstart1"
RECORDED = json.loads((FIXTURES / "owners-final-20261003.json").read_text())
LIVE = json.loads((FIXTURES / "owners-nonempty-20261001.json").read_text())
NAMES = tuple(RECORDED["sets"])
STREAM = "test:market-data-subscriptions"
OWNERS = "test:market-data-subscription-owners"
SATURDAY = datetime(2026, 10, 3, 22, 3, 17, tzinfo=UTC)


class RedisReplay:
    def __init__(self):
        self.entries = []
        self.hashes = {}
        self.fail_next = False

    async def xadd(self, stream, fields, **kwargs):
        assert stream == STREAM
        assert kwargs == {"maxlen": 250, "approximate": True}
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("test Redis unavailable")
        message_id = f"{len(self.entries) + 1}-0"
        self.entries.append((message_id, fields))
        return message_id

    async def xrevrange(self, stream, count=1):
        assert stream == STREAM and count == 1
        return self.entries[-1:][::-1]

    async def xrange(self, stream, min="-"):
        assert stream == STREAM
        boundary = int(min.removeprefix("(").split("-")[0]) if min != "-" else 0
        return [(key, fields) for key, fields in self.entries if int(key.split("-")[0]) > boundary]

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()


def settings(**kwargs):
    return Settings(_env_file=None, redis_stream_prefix="test", massive_api_key="test",
                    market_data_warmup_enabled=False,
                    strategy_schwab_1m_v2_gateway_register_enabled=True, **kwargs)


def consumer(name, redis, monkeypatch, *, flag=True, observe=False):
    config = settings(market_data_subscription_startup_enabled=flag)
    if name == "strategy-engine":
        service = StrategyEngineService(settings=config, redis_client=redis)
        monkeypatch.setattr(service, "_spawn_background_hydration", lambda **_kw: None)
    elif name == "orb":
        service = OrbService(settings=config, redis_client=redis, session_factory=lambda: None)
    elif name == "orb-schwab":
        config = config.model_copy(update={"orb_schwab_observe_enabled": observe,
                                          "orb_live_schwab_orders_enabled": not observe})
        service = OrbSchwabService(settings=config, redis_client=redis, session_factory=lambda: None)
    elif name == "momentum-paper":
        service = MomentumPaperService(settings=config, redis_client=redis, clock=lambda: SATURDAY)
    else:
        service = SchwabV2BotService(settings=config)
        service.redis = redis
    return service


async def sync(name, service, symbols):
    if name == "strategy-engine":
        await service._sync_market_data_subscriptions(symbols)
    elif name == "momentum-paper":
        await service._sync_gateway_subscriptions(desired_override=set(symbols))
    elif name == "schwab-1m-v2":
        service._watchlist = set(symbols)
        await service._sync_gateway_subscription()
    else:
        await service._sync_gateway_subscription(symbols)


def gateway(redis):
    return MarketDataGatewayService(
        settings=settings(), redis_client=redis, snapshot_provider=object(),
        trade_stream=SimpleNamespace(sync_subscriptions=AsyncMock()), reference_cache=object(),
    )


async def drain(gw, redis, start=0):
    for message_id, fields in redis.entries[start:]:
        await gw.apply_subscription_event(
            MarketDataSubscriptionEvent.model_validate_json(fields["data"]), message_id=message_id,
        )


async def replay_recorded(gw, redis, *, nonempty=False):
    for row in RECORDED["events"]:
        await redis.xadd(STREAM, row["fields"], maxlen=250, approximate=True)
    if nonempty:
        for row in sorted(LIVE["owners"].values(), key=lambda row: row["source_id"]):
            await redis.xadd(STREAM, {"data": base64.b64decode(row["raw_b64"]).decode()},
                             maxlen=250, approximate=True)
    await drain(gw, redis)


@pytest.mark.asyncio
async def test_cold_boot_five_real_publishers_announce_recorded_empty_sets_within_60_seconds(monkeypatch):
    redis = RedisReplay()
    gw = gateway(redis)
    start = time.monotonic()
    for name in NAMES:
        service = consumer(name, redis, monkeypatch)
        await sync(name, service, RECORDED["sets"][name])
        await sync(name, service, RECORDED["sets"][name])
    await drain(gw, redis)
    assert time.monotonic() - start < 60
    assert len(redis.entries) == 5  # one first announcement, no unchanged republish
    saved = redis.hashes[OWNERS]
    assert {name: json.loads(saved[name]) for name in NAMES} == RECORDED["sets"]
    assert saved["_migration_complete"] == "1"
    assert gw.active_symbols() == set()
    gw.trade_stream.sync_subscriptions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_single_consumer_restart_refreshes_only_its_recorded_owner(name, monkeypatch):
    redis = RedisReplay()
    gw = gateway(redis)
    await replay_recorded(gw, redis, nonempty=True)
    before = redis.hashes[OWNERS].copy()
    service = consumer(name, redis, monkeypatch)
    offset = len(redis.entries)
    await sync(name, service, RECORDED["sets"][name])
    assert len(redis.entries) == offset + 1
    await drain(gw, redis, offset)
    saved = redis.hashes[OWNERS]
    assert saved[name] == "[]"
    assert all(saved[other] == before[other] for other in NAMES if other != name)
    assert gw.active_symbols() == set().union(*(set(json.loads(saved[n])) for n in NAMES))


@pytest.mark.asyncio
async def test_weekend_paper_tick_announces_empty_once_without_stream_or_reference_reads(monkeypatch):
    redis = RedisReplay()
    service = consumer("momentum-paper", redis, monkeypatch)
    monkeypatch.setattr(service, "_start_gateway", AsyncMock(side_effect=AssertionError("weekend stream")))
    monkeypatch.setattr(service, "_prepare_session", AsyncMock(side_effect=AssertionError("weekend reference")))
    await service._tick()
    await service._tick()
    assert len(redis.entries) == 1
    payload = json.loads(redis.entries[0][1]["data"])["payload"]
    assert payload == {"consumer_name": "momentum-paper", "mode": "replace", "symbols": []}
    assert service._gateway_task is None and service._engine is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_first_write_failure_does_not_latch_announcement(name, monkeypatch):
    redis = RedisReplay()
    service = consumer(name, redis, monkeypatch)
    redis.fail_next = True
    with pytest.raises(ConnectionError):
        await sync(name, service, [])
    await sync(name, service, [])
    assert len(redis.entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", tuple(LIVE["owners"]))
async def test_recorded_empty_then_live_set_then_empty_preserves_subscription_changes(name, monkeypatch):
    redis = RedisReplay()
    gw = gateway(redis)
    service = consumer(name, redis, monkeypatch)
    symbols = LIVE["owners"][name]["symbols"]
    await sync(name, service, [])
    await sync(name, service, symbols)
    await sync(name, service, symbols)
    await drain(gw, redis)
    assert len(redis.entries) == 2
    assert gw.active_symbols() == set(symbols)
    await sync(name, service, [])
    await drain(gw, redis, 2)
    assert gw.active_symbols() == set()


@pytest.mark.asyncio
async def test_two_simultaneous_restarts_keep_all_owners_and_gateway_restores_hash(monkeypatch):
    redis = RedisReplay()
    gw = gateway(redis)
    await replay_recorded(gw, redis, nonempty=True)
    before = redis.hashes[OWNERS].copy()
    offset = len(redis.entries)
    await asyncio.gather(*(sync(name, consumer(name, redis, monkeypatch), [])
                           for name in ("orb", "strategy-engine")))
    await drain(gw, redis, offset)
    saved = redis.hashes[OWNERS].copy()
    assert saved["orb"] == saved["strategy-engine"] == "[]"
    assert all(saved[name] == before[name] for name in ("schwab-1m-v2", "orb-schwab", "momentum-paper"))
    restored = gateway(redis)
    await restored._restore_subscription_state()
    assert redis.hashes[OWNERS] == saved
    assert set(restored._desired_symbols_by_consumer) == {*NAMES, "static"}
    assert restored.active_symbols() == gw.active_symbols()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("strategy-engine", "orb", "orb-schwab", "momentum-paper"))
async def test_flag_off_keeps_old_initial_empty_debounce(name, monkeypatch):
    redis = RedisReplay()
    service = consumer(name, redis, monkeypatch, flag=False)
    await sync(name, service, [])
    assert redis.entries == []


def test_flag_default_and_catalog_cover_all_four_live_processes():
    assert Settings(_env_file=None).market_data_subscription_startup_enabled is True
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    flag = next(row for row in catalog["flags"] if row["name"] == "market_data_subscription_startup_enabled")
    assert flag["expected"] is True
    assert {flag["owning_service"], *flag["also_check_services"]} == {
        "strategy", "orb-schwab", "schwab-1m-v2", "momentum-paper",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_observe_only_keeps_local_filter_but_only_announces_empty_owner(flag, monkeypatch):
    redis = RedisReplay()
    service = consumer("orb-schwab", redis, monkeypatch, flag=flag, observe=True)
    symbols = LIVE["owners"]["orb-schwab"]["symbols"]
    await sync("orb-schwab", service, symbols)
    assert service._last_gateway_symbols == symbols
    await sync("orb-schwab", service, [])
    assert service._last_gateway_symbols == []
    assert len(redis.entries) == int(flag)
    if flag:
        event = MarketDataSubscriptionEvent.model_validate_json(redis.entries[0][1]["data"])
        assert event.payload.symbols == [] and event.payload.consumer_name == "orb-schwab"


class BootComplete(Exception):
    """Stop a unit's loop after its first real startup subscription sync."""


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_healthy_startup_path_publishes_without_any_new_watchlist_event(name, monkeypatch):
    redis = RedisReplay()
    service = consumer(name, redis, monkeypatch)
    started = time.monotonic()
    if name == "strategy-engine":
        for method in ("_initialize_stream_offsets", "_prefill_alert_history_from_snapshot_batches",
                       "_publish_strategy_state_snapshot", "_publish_heartbeat",
                       "_sync_schwab_stream_subscriptions"):
            monkeypatch.setattr(service, method, AsyncMock())
        for method in ("_restore_alert_engine_state_from_dashboard_snapshot",
                       "_seed_confirmed_candidates_from_dashboard_snapshot",
                       "_restore_runtime_state_from_database", "_purge_stale_manual_stop_snapshots",
                       "_preload_manual_stop_state", "_sync_runtime_data_health_incidents"):
            monkeypatch.setattr(service, method, lambda: None)
        service._schwab_stream_client = None
        assert await service._run_init_phase(asyncio.Event()) is True
    elif name in {"orb", "orb-schwab"}:
        service.settings.orb_enabled = True
        monkeypatch.setattr(service, "_maybe_roll_session", lambda: None)
        if name == "orb":
            monkeypatch.setattr(service, "_restore_paper_lifecycle", lambda: None)
        monkeypatch.setattr(service, "_refresh_universe", lambda: None)
        monkeypatch.setattr(service, "_drain_market_data", AsyncMock(side_effect=BootComplete))
        monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.open_entries", lambda *_args: [])
        with pytest.raises(BootComplete):
            await service.run()
    elif name == "momentum-paper":
        service.settings.momentum_paper_enabled = True
        service.session_factory = lambda: None
        service.store = object()
        monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.asyncio.sleep",
                            AsyncMock(side_effect=BootComplete))
        with pytest.raises(BootComplete):
            await service.run()
    else:
        # Run the actual startup scanner task, not just the publish method.
        service.rest_client = object()
        monkeypatch.setattr(redis, "xrevrange", AsyncMock(return_value=[]))
        monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.run_resilient_loop",
                            AsyncMock(side_effect=BootComplete))
        with pytest.raises(BootComplete):
            await service._scanner_consumer_loop()
    assert time.monotonic() - started < 60
    assert len(redis.entries) == 1
    event = MarketDataSubscriptionEvent.model_validate_json(redis.entries[0][1]["data"])
    assert event.payload.consumer_name == name and event.payload.symbols == []
    gw = gateway(redis)
    # Consumers-first boot: gateway reconstructs from the actual published stream.
    await gw._restore_subscription_state()
    assert redis.hashes[OWNERS][name] == "[]"
    assert redis.hashes[OWNERS]["_migration_complete"] == "1"


@pytest.mark.asyncio
async def test_orb_schwab_startup_restores_held_only_coverage_before_first_replace(monkeypatch):
    redis = RedisReplay()
    service = consumer("orb-schwab", redis, monkeypatch)
    service.settings.orb_enabled = True
    symbols = LIVE["owners"]["orb-schwab"]["symbols"]
    # Synthetic ownership classification using recorded symbols: no entry/exit action.
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.open_entries",
                        lambda *_args: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(service, "_maybe_roll_session", lambda: None)
    monkeypatch.setattr(service, "_refresh_universe", lambda: None)
    monkeypatch.setattr(service, "_drain_market_data", AsyncMock(side_effect=BootComplete))
    with pytest.raises(BootComplete):
        await service.run()
    first = MarketDataSubscriptionEvent.model_validate_json(redis.entries[0][1]["data"])
    assert first.payload.symbols == symbols


@pytest.mark.asyncio
async def test_orb_schwab_unknown_startup_ownership_never_publishes_empty_even_in_cleanup(monkeypatch):
    redis = RedisReplay()
    service = consumer("orb-schwab", redis, monkeypatch)
    service.settings.orb_enabled = True

    def unavailable(*_args):
        raise RuntimeError("ownership unknown")

    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.open_entries", unavailable)
    with pytest.raises(RuntimeError, match="ownership unknown"):
        await service.run()
    assert redis.entries == []


@pytest.mark.asyncio
async def test_v2_announces_watchlist_union_held_only_without_entry_scope_change(monkeypatch):
    redis = RedisReplay()
    service = consumer("schwab-1m-v2", redis, monkeypatch)
    service._exit_coverage = set(LIVE["owners"]["schwab-1m-v2"]["symbols"])
    await sync("schwab-1m-v2", service, [])
    event = MarketDataSubscriptionEvent.model_validate_json(redis.entries[0][1]["data"])
    assert set(event.payload.symbols) == service._exit_coverage
    assert service._watchlist == set()


def test_v2_startup_hydrates_coverage_without_running_position_poll_or_orders(monkeypatch):
    redis = RedisReplay()
    service = consumer("schwab-1m-v2", redis, monkeypatch)
    service.session_factory = lambda: None
    symbols = LIVE["owners"]["schwab-1m-v2"]["symbols"]
    monkeypatch.setattr(service, "_fetch_position_maps", lambda: ({}, {symbols[0]: 2}))
    strict_calls = []

    def managed(*, strict=False):
        strict_calls.append(strict)
        return set(symbols[1:])

    monkeypatch.setattr(service, "_fetch_managed_symbols", managed)
    assert service._startup_gateway_exit_coverage() == set(symbols)
    assert strict_calls == [True]
    assert service._watchlist == set() and redis.entries == []


@pytest.mark.parametrize("failure", ["no_database", "virtual", "managed"])
def test_v2_startup_unknown_coverage_refuses_instead_of_empty(failure, monkeypatch):
    service = consumer("schwab-1m-v2", RedisReplay(), monkeypatch)
    service.session_factory = None if failure == "no_database" else lambda: None
    monkeypatch.setattr(service, "_fetch_position_maps", lambda: None if failure == "virtual" else ({}, {}))

    def managed(*, strict=False):
        if strict:
            raise RuntimeError("ownership read failed")
        return set()

    monkeypatch.setattr(service, "_fetch_managed_symbols", managed)
    with pytest.raises(RuntimeError):
        service._startup_gateway_exit_coverage()


def test_v2_strict_managed_read_raises_while_normal_poll_retains_coverage(monkeypatch):
    service = consumer("schwab-1m-v2", RedisReplay(), monkeypatch)
    service._exit_coverage = {"NXL"}

    def unavailable():
        raise RuntimeError("database unavailable")

    service.session_factory = unavailable
    assert service._fetch_managed_symbols() == {"NXL"}
    with pytest.raises(RuntimeError, match="database unavailable"):
        service._fetch_managed_symbols(strict=True)


@pytest.mark.asyncio
async def test_failed_or_cancelled_first_write_does_not_release_other_owners(monkeypatch):
    redis = RedisReplay()
    gw = gateway(redis)
    await replay_recorded(gw, redis, nonempty=True)
    before = redis.hashes[OWNERS].copy()
    service = consumer("orb-schwab", redis, monkeypatch)
    original = redis.xadd
    monkeypatch.setattr(redis, "xadd", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await sync("orb-schwab", service, [])
    assert not service._gateway_subscription_announced
    assert redis.hashes[OWNERS] == before
    monkeypatch.setattr(redis, "xadd", original)
    offset = len(redis.entries)
    await sync("orb-schwab", service, [])
    await drain(gw, redis, offset)
    assert redis.hashes[OWNERS]["orb-schwab"] == "[]"
    assert all(redis.hashes[OWNERS][name] == before[name] for name in NAMES if name != "orb-schwab")


@pytest.mark.asyncio
async def test_empty_owners_do_not_unsubscribe_static_symbols():
    redis = RedisReplay()
    gw = gateway(redis)
    # Synthetic static ownership assignment of a recorded symbol.
    gw._desired_symbols_by_consumer["static"] = {"NXL"}
    gw._active_symbols = {"NXL"}
    await replay_recorded(gw, redis)
    assert gw.active_symbols() == {"NXL"}
    gw.trade_stream.sync_subscriptions.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_orb_startup_failed_or_ambiguous_publish_never_cleans_up_to_empty(
    lost_ack, cancelled, monkeypatch,
):
    redis = RedisReplay()
    service = consumer("orb-schwab", redis, monkeypatch)
    service.settings.orb_enabled = True
    symbols = LIVE["owners"]["orb-schwab"]["symbols"]
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.open_entries",
                        lambda *_args: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(service, "_maybe_roll_session", lambda: None)
    monkeypatch.setattr(service, "_refresh_universe", lambda: None)
    attempts = []
    original = redis.xadd
    error = asyncio.CancelledError if cancelled else ConnectionError

    async def fail_first(stream, fields, **kwargs):
        attempts.append(json.loads(fields["data"])["payload"]["symbols"])
        if lost_ack or len(attempts) > 1:
            await original(stream, fields, **kwargs)
        if len(attempts) == 1:
            raise error()

    monkeypatch.setattr(redis, "xadd", fail_first)
    with pytest.raises(error):
        await service.run()
    assert attempts == [symbols]
    assert not service._gateway_subscription_announced


@pytest.mark.asyncio
async def test_v2_inflight_replace_serializes_new_held_coverage_against_old_ack_cache(monkeypatch):
    redis = RedisReplay()
    service = consumer("schwab-1m-v2", redis, monkeypatch)
    await sync("schwab-1m-v2", service, ["NXL", "VEEA"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = redis.xadd

    async def delayed(stream, fields, **kwargs):
        if json.loads(fields["data"])["payload"]["symbols"] == ["VEEA"]:
            entered.set()
            await release.wait()
        return await original(stream, fields, **kwargs)

    monkeypatch.setattr(redis, "xadd", delayed)
    first = asyncio.create_task(sync("schwab-1m-v2", service, ["VEEA"]))
    await asyncio.wait_for(entered.wait(), 2)
    service._exit_coverage = {"NXL"}
    second = asyncio.create_task(sync("schwab-1m-v2", service, ["VEEA"]))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert [json.loads(fields["data"])["payload"]["symbols"] for _, fields in redis.entries] == [
        ["NXL", "VEEA"], ["VEEA"], ["NXL", "VEEA"],
    ]
    assert service._last_gateway_symbols == ["NXL", "VEEA"]
    gw = gateway(redis)
    await drain(gw, redis)
    assert gw.active_symbols() == {"NXL", "VEEA"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("cancelled", [False, True])
async def test_lost_ack_never_debounces_return_to_previous_acknowledged_set(name, cancelled, monkeypatch):
    redis = RedisReplay()
    service = consumer(name, redis, monkeypatch)
    await sync(name, service, ["NXL", "VEEA"])
    if name == "momentum-paper":
        service._last_subscription_sync = None  # elapsed removal-debounce boundary
    original = redis.xadd
    error = asyncio.CancelledError if cancelled else ConnectionError

    async def accepted_but_lost_ack(stream, fields, **kwargs):
        await original(stream, fields, **kwargs)
        raise error()

    monkeypatch.setattr(redis, "xadd", accepted_but_lost_ack)
    with pytest.raises(error):
        await sync(name, service, ["VEEA"])
    monkeypatch.setattr(redis, "xadd", original)
    await sync(name, service, ["NXL", "VEEA"])
    assert len(redis.entries) == 3
    gw = gateway(redis)
    await drain(gw, redis)
    assert gw.active_symbols() == {"NXL", "VEEA"}


@pytest.mark.asyncio
async def test_v2_run_finishes_ownership_hydration_before_starting_scanner_and_poll(monkeypatch):
    from project_mai_tai.services import schwab_1m_v2_bot as module

    redis = RedisReplay()
    service = consumer("schwab-1m-v2", redis, monkeypatch)
    service.settings.strategy_schwab_1m_v2_enabled = True
    service.settings.strategy_schwab_1m_v2_tick_capture_enabled = False
    service.session_factory = lambda: None
    monkeypatch.setattr(module.Redis, "from_url", lambda *_a, **_kw: redis)
    monkeypatch.setattr(module, "SchwabV2RestClient", lambda *_a, **_kw: object())
    monkeypatch.setattr(module, "SchwabV2Streamer", lambda *_a, **_kw: object())
    monkeypatch.setattr(service, "_configure_fanout_identity_store", lambda: [])
    monkeypatch.setattr(service, "_configure_flip_entry_ownership_store", lambda *_a: None)
    monkeypatch.setattr(service, "_configure_fanout_outcome_journal", lambda *_a: None)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_a: None)
    monkeypatch.setattr(service, "_fetch_position_maps", lambda: ({}, {"NXL": 2}))
    monkeypatch.setattr(service, "_fetch_managed_symbols", lambda **_kw: {"VEEA"})

    async def before_tasks(*_args):
        assert service._exit_coverage == {"NXL", "VEEA"}
        assert not getattr(service, "_tasks", {})
        raise BootComplete()

    monkeypatch.setattr(service, "_publish_heartbeat", before_tasks)
    with pytest.raises(BootComplete):
        await service.run()


@pytest.mark.asyncio
async def test_paper_cap_remains_16_with_startup_flag_on(monkeypatch):
    redis = RedisReplay()
    service = consumer("momentum-paper", redis, monkeypatch)
    # Synthetic boundary cardinality, not represented as a recorded market event.
    symbols = {f"CAP{i}" for i in range(16)}
    await sync("momentum-paper", service, [])
    await sync("momentum-paper", service, sorted(symbols))
    with pytest.raises(RuntimeError, match="cap breached"):
        await sync("momentum-paper", service, sorted(symbols | {"EXTRA"}))
    assert len(redis.entries) == 2
    assert service._subscribed_symbols == symbols


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("orb", "orb-schwab", "momentum-paper"))
async def test_disabled_services_remain_inert(name, monkeypatch):
    redis = RedisReplay()
    service = consumer(name, redis, monkeypatch)
    service.settings.orb_enabled = False
    service.settings.momentum_paper_enabled = False
    await service.run()
    assert redis.entries == []
