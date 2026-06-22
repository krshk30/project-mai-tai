from __future__ import annotations

import asyncio
import json
import types
from decimal import Decimal

from project_mai_tai.market_data.tick_time import normalize_ts_ns, ns_to_datetime
from project_mai_tai.services.market_capture_app import MarketCaptureService


class _Boom:
    def __getattr__(self, name):  # pragma: no cover - only hit on regression
        raise AssertionError(f"disabled/parse-only capture touched redis: {name!r}")


def _settings(enabled: bool):
    return types.SimpleNamespace(
        market_capture_enabled=enabled,
        market_capture_batch_size=1000,
        market_capture_flush_secs=2.0,
        market_capture_provider_tag="massive",
        market_capture_stats_every=30,
        redis_url="redis://localhost:6379/0",
        redis_stream_prefix="mai_tai",
    )


def _svc():
    return MarketCaptureService(settings=_settings(enabled=True), redis_client=_Boom())


def test_normalize_ts_ns_units():
    ms = 1782135713372  # 13-digit live-WS milliseconds (2026-06-22)
    assert normalize_ts_ns(ms) == ms * 1_000_000
    assert normalize_ts_ns(ms * 1_000_000) == ms * 1_000_000  # already ns
    assert normalize_ts_ns(ms * 1_000) == ms * 1_000_000  # us
    assert normalize_ts_ns(ms // 1_000) == (ms // 1_000) * 1_000_000_000  # s
    for junk in (None, 0, -5, "", "abc"):
        assert normalize_ts_ns(junk) is None
    assert ns_to_datetime(normalize_ts_ns(ms)).year == 2026  # never 1970


def test_disabled_returns_immediately_no_redis():
    svc = MarketCaptureService(settings=_settings(enabled=False), redis_client=_Boom())
    asyncio.run(svc.run())  # must not touch redis / DB
    assert svc._trades == [] and svc._quotes == []


def test_ingest_trade_tick_ms_timestamp_buckets_to_2026():
    svc = _svc()
    svc._ingest(json.dumps({
        "event_type": "trade_tick",
        "produced_at": "2026-06-22T15:16:21.078253Z",
        "payload": {"symbol": "ehgo", "price": "3.44", "size": 100,
                    "timestamp_ns": 1782135713372, "exchange": "10",
                    "conditions": [12, 37], "cumulative_volume": 27057153},
    }))
    assert len(svc._trades) == 1
    row = svc._trades[0]
    assert row["symbol"] == "EHGO"  # upcased
    assert row["event_ts"].year == 2026  # NOT 1970 — normalization applied
    assert row["price"] == Decimal("3.44")
    assert row["size"] == 100
    assert row["exchange"] == "10"
    assert row["conditions"] == "12,37"
    assert row["provider"] == "massive"


def test_ingest_quote_tick_uses_produced_at():
    svc = _svc()
    svc._ingest(json.dumps({
        "event_type": "quote_tick",
        "produced_at": "2026-06-22T15:16:21.078253Z",
        "payload": {"symbol": "SKYQ", "bid_price": "1.78", "ask_price": "1.79",
                    "bid_size": 600, "ask_size": 1100},
    }))
    assert len(svc._quotes) == 1
    q = svc._quotes[0]
    assert q["symbol"] == "SKYQ"
    assert q["event_ts"].year == 2026
    assert q["bid_price"] == Decimal("1.78") and q["ask_price"] == Decimal("1.79")
    assert q["bid_size"] == 600 and q["ask_size"] == 1100


def test_ingest_drops_unparseable_and_unknown_types():
    svc = _svc()
    svc._ingest("not json")
    svc._ingest(json.dumps({"event_type": "book_tick", "payload": {"symbol": "X"}}))  # future type, no table yet
    svc._ingest(json.dumps({"event_type": "trade_tick", "payload": {"symbol": ""}}))  # no symbol
    assert svc._trades == [] and svc._quotes == []


def test_flush_writes_and_clears_buffer():
    svc = _svc()
    written = {}

    def fake_write(trades, quotes):
        written["trades"] = list(trades)
        written["quotes"] = list(quotes)

    svc._write = fake_write
    svc._trades = [{"symbol": "A"}]
    svc._quotes = [{"symbol": "B"}]
    asyncio.run(svc._flush())
    assert written["trades"] == [{"symbol": "A"}]
    assert written["quotes"] == [{"symbol": "B"}]
    assert svc._trades == [] and svc._quotes == []  # buffer cleared
