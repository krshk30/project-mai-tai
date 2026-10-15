from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.services import momentum_paper_app as app
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_warning import START, bar


def setup(monkeypatch, caplog, *, is_us=False, status="READY", hour=11):
    now = [datetime(2026, 10, 15, hour, 10, tzinfo=UTC)]
    origin = int(now[0].timestamp() * 1000)

    def forbidden(*args, **kwargs):
        pytest.fail("warning diagnostics reached DB/provider")

    service = app.MomentumPaperService(
        Settings(_env_file=None, momentum_live_enabled=True, market_data_momentum_warning_enabled=True),
        redis_client=AsyncMock(), session_factory=forbidden,
        rest_client_factory=forbidden, clock=lambda: now[0])
    service._warning = MomentumWarningConsumer(prior_closes={"SPRC": Decimal(10), "OTHER": Decimal(10)},
        condition_version="controlled", coverage_started_ms=origin - 60_000)
    service._engine = MomentumWarningConsumer(prior_closes={}, condition_version="controlled",
        coverage_started_ms=origin - 60_000).engine
    service._session_date = now[0].date()
    router = CountryRouter(SimpleNamespace(load=forbidden))
    if status is not None:
        router._records["SPRC"] = CountryRecord("SPRC", "0000000001", "CA" if is_us else "L3",
            "DE" if is_us else "L3", is_us, now[0] - timedelta(days=7), status, now[0].date())
    service._live_country_router = router
    service._live_country_day = now[0].date()
    service._live_handoff.country_prepared_day = now[0].date()
    service._live_handoff.route_for = router.route_for
    service._live_handoff.ledger.ready = True
    service._live_day_volumes["SPRC"] = (55000, now[0])
    service._persist = AsyncMock()
    service._warning_metrics = AsyncMock()
    service._sync_gateway_subscriptions = AsyncMock()
    service._start_gateway = AsyncMock()
    service._publish_state = AsyncMock()
    monkeypatch.setattr(app.wall_time, "time_ns", lambda: int(now[0].timestamp() * 1e9))
    caplog.set_level(logging.INFO, logger=app.logger.name)

    async def handle(offset, *, symbol="SPRC", high="11", sequence=None):
        item = bar(offset, symbol=symbol, high=high, sequence=sequence)
        shift = origin - START
        item = replace(item, start_ms=item.start_ms + shift, end_ms=item.end_ms + shift,
            received_utc_ns=item.received_utc_ns + shift * 1_000_000,
            published_utc_ns=item.published_utc_ns + shift * 1_000_000)
        now[0] = datetime.fromtimestamp(item.published_utc_ns / 1e9, UTC)
        await service._handle_warning_event(json.dumps(
            {**item.__dict__, "version": 1, "event_type": "aggregate"}, default=str))

    return service, now, handle


def lines(caplog, marker):
    return [record.getMessage() for record in caplog.records
            if record.name == app.logger.name and marker in record.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_us,status,hour,route,reason", [
    (False, "READY", 11, "webull", "SEC_NON_US"),
    (True, "READY", 11, "schwab", "SEC_US"),
    (None, None, 11, "webull", "SEC_UNKNOWN"),
    (None, "FETCH_FAILED", 11, "webull", "SEC_UNKNOWN"),
    (True, "READY", 9, "webull", "ALL_BEFORE_07"),
])
async def test_one_add_route_log_is_in_memory_and_refresh_duplicate_do_not_repeat(
    monkeypatch, caplog, is_us, status, hour, route, reason,
):
    service, _, handle = setup(monkeypatch, caplog, is_us=is_us, status=status, hour=hour)
    await handle(0, high="10")
    await handle(1)
    await handle(1)  # Same sequence: ignored, not another ADD.
    await handle(2, high="11.5")  # Warning refreshed, not another ADD.
    added = lines(caplog, "action=ADD")
    routes = lines(caplog, "[MOMLIVE1-WARN-ROUTE]")
    assert len(added) == len(routes) == 1
    assert "symbol=SPRC trigger_price=11" in added[0]
    assert f"route={route} reason={reason}" in routes[0]
    assert f"status={status or 'UNKNOWN'}" in routes[0]
    assert "day_volume=55000" in routes[0] and "informational_only=true" in routes[0]
    if is_us is False:
        assert "country=L3/L3" in routes[0] and "as_of=2026-10-08" in routes[0]
    assert service._warning_log_prices == {"SPRC": Decimal(11)}
    assert service._live_handoff.queue.empty()
    service.redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("volume,age", [(None, 0), (55000, 16), (55000, -3), (55000, 86400)])
async def test_missing_stale_future_previous_day_volume_logged_unknown(monkeypatch, caplog, volume, age):
    service, now, handle = setup(monkeypatch, caplog)
    service._live_day_volumes["SPRC"] = (volume, now[0] - timedelta(seconds=age))
    await handle(0)
    assert "day_volume=UNKNOWN" in lines(caplog, "[MOMLIVE1-WARN-ROUTE]")[0]


@pytest.mark.asyncio
async def test_expired_drop_logs_its_own_add_price_without_dropping_other_symbol(monkeypatch, caplog):
    service, now, handle = setup(monkeypatch, caplog)
    await handle(0)
    await handle(1, symbol="OTHER")
    now[0] += timedelta(seconds=61)
    service._last_snapshot_at = now[0]
    service._warning.candidates.observe_eligible_trade("OTHER", observed_ms=service._now_ms())
    await service._tick()
    drops = lines(caplog, "action=DROP")
    assert len(drops) == 1 and "symbol=SPRC trigger_price=11" in drops[0]
    assert "reason=expired_no_data" in drops[0]
    assert service._warning.desired_symbols(set()) == {"OTHER"}
    assert service._warning_log_prices == {"OTHER": Decimal(11)}


@pytest.mark.asyncio
async def test_global_transport_gap_does_not_invent_warning_drop(monkeypatch, caplog):
    service, _, handle = setup(monkeypatch, caplog)
    await handle(0)
    await handle(1, symbol="OTHER")
    await handle(2, sequence=4)  # Real sequence gap invalidates coverage, not the leases.
    assert lines(caplog, "action=DROP") == []
    assert service._warning.desired_symbols(set()) == {"SPRC", "OTHER"}
    assert service._warning_log_prices == {"SPRC": Decimal(11), "OTHER": Decimal(11)}


@pytest.mark.asyncio
async def test_warning_route_is_not_sealed_detected_route(monkeypatch, caplog):
    service, now, handle = setup(monkeypatch, caplog)
    await handle(0)
    assert "route=webull reason=SEC_NON_US" in lines(caplog, "[MOMLIVE1-WARN-ROUTE]")[0]
    service._live_country_router._records["SPRC"] = CountryRecord("SPRC", "0000000001", "CA", "DE",
        True, now[0], "READY", now[0].date())
    await handle(1, high="12")
    signals = [signal for _, signal in service._live_handoff.queue._queue]
    assert signals and all(signal.country_route == "schwab" and signal.country_category == "sec_us"
                           for signal in signals)
    assert len(lines(caplog, "[MOMLIVE1-WARN-ROUTE]")) == 1
    assert all(row.event_type == "DETECTED" for row in service._persist.await_args.args[0])
    service.redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_no_country_router_logs_unknown_without_new_dependency(monkeypatch, caplog):
    service, _, handle = setup(monkeypatch, caplog)
    service._live_handoff = None
    service._live_country_router = None
    service._live_day_volumes.clear()
    await handle(0)
    route = lines(caplog, "[MOMLIVE1-WARN-ROUTE]")[0]
    assert "route=UNKNOWN reason=SEC_TABLE_UNAVAILABLE status=UNKNOWN" in route
    assert "country=UNKNOWN/UNKNOWN as_of=UNKNOWN day_volume=UNKNOWN" in route
    assert service._warning.desired_symbols(set()) == {"SPRC"}
    service.redis.xadd.assert_not_awaited()
