"""Run explicitly AFTER composing pinned G1/G2 sources; not a recorded WS replay.

PYTHONPATH=<composed>/src:<composed>/tests/unit python -m pytest <this file>
The stock A primary-doc sample is schema evidence, not a live/captured burst.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from massive.websocket.models import EquityAgg
import pytest

from project_mai_tai.market_data import massive_provider
from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.momentum_paper.warning_window import AggregateObservation
from test_market_data_gateway import FakeRedis


# Exact qualifying OHLC and endpoint subset of the primary-doc sample.
# https://massive.com/docs/websocket/stocks/aggregates-per-second
DOCUMENTED_SAMPLE = {
    "ev": "A", "sym": "SPCE", "v": 200,
    "o": 25.39, "c": 25.39, "h": 25.39, "l": 25.39,
    "s": 1610144868000, "e": 1610144869000,
}


@pytest.mark.asyncio
async def test_documented_A_provider_Redis_G2_contract_preserves_exclusive_boundary(monkeypatch):
    records = []
    provider = MassiveTradeStream("offline-test")
    provider.configure_momentum_warning(records.append, lambda _: pytest.fail("unexpected A revert"))
    provider._warning_aggregates_enabled = provider._warning_wildcard_used = True
    monkeypatch.setattr(massive_provider, "datetime", SimpleNamespace(
        now=lambda _: datetime(2026, 10, 12, 12, 51, tzinfo=UTC)
    ))
    provider._handle_messages([EquityAgg.from_dict(DOCUMENTED_SAMPLE)])
    assert len(records) == 1
    redis = FakeRedis()
    publisher = MarketDataPublisher(redis, "contract", "market-data-gateway")
    await publisher.publish_momentum_warning_aggregate(
        records[0], sequence=1, dropped_total=0, gateway_instance_id="offline-contract", generation=1,
    )
    stream, payload, _ = redis.entries[0]
    assert stream == "contract:momentum-warning-bars"
    observed = AggregateObservation.from_payload(payload)
    assert observed.start_ms == payload["start_ms"] == DOCUMENTED_SAMPLE["s"]
    assert observed.end_ms == payload["end_ms"] == DOCUMENTED_SAMPLE["e"]
    assert observed.end_ms == observed.start_ms + 1000
    assert observed.low == Decimal("25.39")
    assert not any(key in payload for key in ("sip_low_at", "sip_timestamp", "timestamp_ns"))
    assert not hasattr(observed, "sip_low_at")


@pytest.mark.parametrize("span_ms", [-1, 0, 1, 500, 998, 999, 1001, 2000])
def test_unsupported_partial_or_inclusive_shape_never_reaches_Redis_or_G2(span_ms):
    message = EquityAgg.from_dict({**DOCUMENTED_SAMPLE, "e": DOCUMENTED_SAMPLE["s"] + span_ms})
    assert MassiveTradeStream._normalize_warning_aggregate(message, "SPCE") is None
