from __future__ import annotations

import asyncio
import types

from project_mai_tai.services.orb_app import OrbService


class _Boom:
    """Redis stand-in that fails the test if ANY attribute is touched."""

    def __getattr__(self, name):  # pragma: no cover - only hit on regression
        raise AssertionError(f"disabled ORB service touched redis: {name!r}")


def _settings(orb_enabled: bool):
    return types.SimpleNamespace(
        orb_enabled=orb_enabled,
        redis_stream_prefix="mai_tai",
        redis_market_data_subscription_stream_maxlen=250,
        redis_strategy_intent_stream_maxlen=2000,
        orb_or_minutes=5,
        orb_vol_mult=1.5,
        orb_width_max_pct=12.0,
        orb_width_min_pct=2.0,
        orb_cutoff_minutes=60,
        orb_trail_pct=8.0,
        orb_universe_lead_minutes=5,
        orb_execution_mode="bar_close",
        orb_broker_account_name="paper:orb",
        orb_quantity=10,
    )


def test_disabled_is_inert_never_touches_redis():
    svc = OrbService(settings=_settings(orb_enabled=False), redis_client=_Boom())
    # run() must return immediately and never register a consumer / drain / read the DB.
    asyncio.run(svc.run())
    assert svc._last_gateway_symbols == []
    assert svc._pending_intents == []


def test_pre_open_universe_empty_without_db():
    # No session_factory (default) -> safe empty universe (ORB sits the day out), no crash.
    svc = OrbService(settings=_settings(orb_enabled=True), redis_client=_Boom())
    assert svc.session_factory is None
    assert svc._pre_open_universe() == []
