from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live_sec import CountryRecord, CountryStore
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_momlive1_handoff import row


@pytest.mark.asyncio
async def test_actual_paper_router_binding_sealed_us_and_overnight_reload_without_provider():
    now = [datetime(2026, 10, 14, 23, tzinfo=UTC)]
    factory = build_test_session_factory()
    store = CountryStore(factory)
    service = MomentumPaperService(Settings(_env_file=None, momentum_live_enabled=True,
        market_data_subscription_startup_enabled=False), session_factory=factory,
        redis_client=AsyncMock(), clock=lambda: now[0])
    await service._start_country_router()
    try:
        router = service._live_country_router
        assert service._live_handoff.route_for.__self__ is router
        assert service._live_handoff.country_warning_offer.__self__ is router
        assert router.client is None  # Empty configured contact: no HTTP capability.
        next_day = datetime(2026, 10, 15, 11, tzinfo=UTC)
        store.put(CountryRecord("SCNI", "0000012345", "TX", "DE", True,
            next_day, "READY", next_day.date()))
        # Paper started the prior evening: its immutable memory is still old.
        assert router.route_for("SCNI", next_day).broker == "webull"
        now[0] = datetime(2026, 10, 15, 7, 55, tzinfo=UTC)
        service._prepare_session = AsyncMock()
        service._stop_gateway = AsyncMock()
        service._live_day_volumes["SCNI"] = (999999, datetime(2026, 10, 14, 20, tzinfo=UTC))
        await service._tick()
        assert service._live_day_volumes == {}
        assert service._live_handoff.country_prepared_day == now[0].date()
        assert service._live_country_day == now[0].date()
        assert router.route_for("SCNI", next_day).broker == "schwab"
        service._live_handoff.ledger.ready = True
        observed = int(next_day.timestamp() * 1e9)
        # CONTROLLED up-day input isolates country sealing, not historical SCNI proof.
        detected = row(payload={"prior_close": "2.50", "observed_decision_ns": observed,
            "reference": {"end_ms": 1}, "detect": {"end_ms": 3, "price": "3"}})
        assert service._live_handoff.offer(detected, day_volume=50000, now_ns=observed) == "offered"
        signal = service._live_handoff.queue._queue[0][1]
        assert signal.country_route == "schwab" and signal.country_category == "sec_us"
        # A later DB/cache change does not rewrite the already sealed decision.
        store.put(CountryRecord("SCNI", "0000012345", "F4", "E9", False,
            next_day, "READY", next_day.date()))
        await router.reload_async()
        assert router.route_for("SCNI", next_day).broker == "webull"
        assert signal.country_route == "schwab"
        service.redis.xadd.assert_not_awaited()
    finally:
        await router.close()
