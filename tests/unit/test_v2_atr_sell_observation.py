from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    ATRSellObservation,
    SchwabV2IntentEmitter,
)


class _RecordingRedis:
    def __init__(self) -> None:
        self.fields: dict[str, str] | None = None

    async def xadd(self, _stream, fields, **_kwargs):
        self.fields = fields
        return b"1-1"


class _PendingStrategy:
    def __init__(self, observation: ATRSellObservation) -> None:
        self.observations = {observation.decision_id: observation}

    def pending_atr_sell_observations(self) -> list[ATRSellObservation]:
        return list(self.observations.values())

    def acknowledge_atr_sell_observation(self, decision_id: str) -> None:
        self.observations.pop(decision_id, None)


class _FlakyEmitter:
    def __init__(self) -> None:
        self.attempts = 0

    async def emit_atr_sell_observation(self, _observation: ATRSellObservation) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary Redis failure")


def _observation() -> ATRSellObservation:
    bar_time_ms = int((datetime.now(UTC) - timedelta(seconds=60)).timestamp() * 1000)
    return ATRSellObservation(
        symbol="YMAT",
        bar_time_ms=bar_time_ms,
        bar_close=4.12,
        atr_trail=4.18,
        decision_id=f"atr-sell:YMAT:{bar_time_ms}",
    )


@pytest.mark.asyncio
async def test_observation_payload_has_no_account_or_quantity() -> None:
    redis = _RecordingRedis()
    emitter = SchwabV2IntentEmitter(Settings(), redis, "live:schwab_1m_v2")

    await emitter.emit_atr_sell_observation(_observation())

    assert redis.fields is not None
    payload = json.loads(redis.fields["data"])
    assert payload["event_type"] == "v2_atr_sell_observation"
    assert payload["symbol"] == "YMAT"
    assert "broker_account_name" not in payload
    assert "quantity" not in payload


@pytest.mark.asyncio
async def test_failed_delivery_retries_until_it_is_acknowledged() -> None:
    observation = _observation()
    strategy = _PendingStrategy(observation)
    emitter = _FlakyEmitter()
    service = SchwabV2BotService.__new__(SchwabV2BotService)
    service.strategy = strategy
    service.intent_emitter = emitter

    await service._drain_atr_sell_observations()
    assert emitter.attempts == 1
    assert strategy.pending_atr_sell_observations() == [observation]

    await service._drain_atr_sell_observations()
    assert emitter.attempts == 2
    assert strategy.pending_atr_sell_observations() == []


@pytest.mark.asyncio
async def test_failed_delivery_is_discarded_at_the_freshness_boundary() -> None:
    observation = _observation()
    expired = ATRSellObservation(
        symbol=observation.symbol,
        bar_time_ms=int((datetime.now(UTC) - timedelta(seconds=181)).timestamp() * 1000),
        bar_close=observation.bar_close,
        atr_trail=observation.atr_trail,
        decision_id="atr-sell:YMAT:expired",
    )
    strategy = _PendingStrategy(expired)
    emitter = _FlakyEmitter()
    service = SchwabV2BotService.__new__(SchwabV2BotService)
    service.strategy = strategy
    service.intent_emitter = emitter

    await service._drain_atr_sell_observations()

    assert emitter.attempts == 0
    assert strategy.pending_atr_sell_observations() == []


@pytest.mark.asyncio
async def test_paper_exit_consumes_only_the_replacement_observation_event() -> None:
    seen: list[tuple[str, datetime]] = []

    class _Runtime:
        def on_atr_sell(self, *, symbol: str, observed_at: datetime):
            seen.append((symbol, observed_at))
            return []

    service = StrategyEngineService.__new__(StrategyEngineService)
    service.paper_exit_runtime = _Runtime()
    service._persist_paper_decisions = lambda _decisions: None
    bar_time_ms = int(datetime(2026, 9, 9, 14, 30, tzinfo=UTC).timestamp() * 1000)

    await service._handle_stream_message(
        "test",
        {
            "data": json.dumps(
                {
                    "event_type": "v2_atr_sell_observation",
                    "symbol": "YMAT",
                    "bar_time_ms": bar_time_ms,
                }
            )
        },
    )
    await service._handle_stream_message(
        "test",
        {"data": json.dumps({"event_type": "v2_cw_flip", "symbol": "YMAT"})},
    )

    assert seen == [("YMAT", datetime(2026, 9, 9, 14, 30, tzinfo=UTC))]
