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
    )


def test_disabled_is_inert_never_touches_redis():
    svc = OrbService(settings=_settings(orb_enabled=False), redis_client=_Boom())
    # run() must return immediately and never register a consumer or drain anything.
    asyncio.run(svc.run())
    assert svc._last_gateway_symbols == []
    assert svc._bar_count == 0


def test_pre_open_universe_is_empty_stub_in_3a():
    svc = OrbService(settings=_settings(orb_enabled=True), redis_client=_Boom())
    assert svc._pre_open_universe() == []  # slice 3b fills this in
