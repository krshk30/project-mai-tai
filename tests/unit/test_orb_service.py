from __future__ import annotations

import asyncio
import json
import types

from project_mai_tai.services.orb_app import OrbService, _normalize_trade_ts_ns


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


def test_normalize_trade_ts_ns_units():
    # The gateway labels the field _ns but the magnitude varies by source.
    ms = 1782135713372  # 13-digit Massive/Polygon milliseconds (2026-06-22 13:41:53.372Z)
    assert _normalize_trade_ts_ns(ms) == ms * 1_000_000  # ms -> ns
    assert _normalize_trade_ts_ns(ms * 1_000_000) == ms * 1_000_000  # already ns -> unchanged
    assert _normalize_trade_ts_ns(ms * 1_000) == ms * 1_000_000  # us -> ns
    assert _normalize_trade_ts_ns(ms // 1_000) == (ms // 1_000) * 1_000_000_000  # s -> ns
    for junk in (None, 0, "", "abc"):
        assert _normalize_trade_ts_ns(junk) is None


def test_handle_market_data_buckets_polygon_ms_tick_to_real_time():
    # Regression: a Massive/Polygon trade_tick carries MILLISECONDS in timestamp_ns.
    # Before the fix, ms / 1e9 lands at ~1970 and the session-anchored aggregator
    # drops it -> no OR bar ever. After the fix it must bucket to the real minute.
    svc = OrbService(settings=_settings(orb_enabled=True), redis_client=_Boom())
    svc._last_gateway_symbols = ["EHGO"]
    fields = {
        "data": json.dumps(
            {
                "event_type": "trade_tick",
                "payload": {
                    "symbol": "EHGO",
                    "price": "3.44",
                    "size": 100,
                    "timestamp_ns": 1782135713372,  # ms, NOT ns
                },
            }
        )
    }
    svc._handle_market_data(fields)
    agg = svc._aggregators.get("EHGO")
    assert agg is not None, "tick was dropped before reaching the aggregator"
    assert agg._bucket is not None
    # the floored-minute bucket must be 2026 (real time), never the 1970 epoch bug
    assert agg._bucket.year == 2026
    assert (agg._bucket.hour, agg._bucket.minute) == (13, 41)
