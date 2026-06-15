"""Track-2 Phase-2 Slice-2 — v2 gateway quote-consumer bridge.

v2 registers its watchlist as a market-data gateway subscription CONSUMER so the
gateway streams quotes for v2's symbols → the OMS quote cache covers them (the
GUARANTEE the in-practice overlap doesn't give). Gated OFF by default → INERT.

The publish method uses only self.{settings, redis, _watchlist, _last_gateway_symbols},
so it's exercised against a minimal harness with a fake async redis recording xadds.
"""
from __future__ import annotations

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings

SUBS_STREAM = "mai_tai:market-data-subscriptions"


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def xadd(self, stream, fields, **kw):
        self.calls.append((stream, fields, kw))
        return b"1-1"


class _Harness:
    # `enabled` = the exit flag; `register` = the dedicated coverage flag (decoupled).
    # _sync_gateway_subscription registers when EITHER is on.
    def __init__(self, *, enabled: bool = False, register: bool = False, watchlist: set[str]) -> None:
        self.settings = Settings(
            oms_v2_exit_management_enabled=enabled,
            strategy_schwab_1m_v2_gateway_register_enabled=register,
        )
        self.redis = _FakeRedis()
        self._watchlist = set(watchlist)
        self._last_gateway_symbols = None


async def _sync(h: _Harness) -> None:
    await SchwabV2BotService._sync_gateway_subscription(h)


def _decode(call) -> MarketDataSubscriptionEvent:
    return MarketDataSubscriptionEvent.model_validate_json(call[1]["data"])


# --------------------------------------------------------------------------- (1)

@pytest.mark.asyncio
async def test_dormant_when_both_flags_off() -> None:
    h = _Harness(enabled=False, register=False, watchlist={"VSME", "CAST"})
    await _sync(h)
    assert h.redis.calls == []                 # nothing published — inert
    assert h._last_gateway_symbols is None     # state untouched


@pytest.mark.asyncio
async def test_registers_via_own_flag_with_exits_off() -> None:
    """The decouple: coverage registers on its OWN flag, exits OFF — so coverage
    can be deployed + re-probed live before exits ever arm."""
    h = _Harness(enabled=False, register=True, watchlist={"VSME", "CAST"})
    await _sync(h)
    assert len(h.redis.calls) == 1
    ev = _decode(h.redis.calls[0])
    assert ev.payload.consumer_name == "schwab-1m-v2"
    assert ev.payload.mode == "replace" and ev.payload.symbols == ["CAST", "VSME"]


# --------------------------------------------------------------------------- (2)

@pytest.mark.asyncio
async def test_publishes_consumer_subscription_when_enabled() -> None:
    h = _Harness(enabled=True, watchlist={"VSME", "CAST"})
    await _sync(h)
    assert len(h.redis.calls) == 1
    stream, fields, kw = h.redis.calls[0]
    assert stream == SUBS_STREAM
    assert kw.get("approximate") is True and kw.get("maxlen") == 250
    ev = _decode(h.redis.calls[0])
    assert ev.payload.consumer_name == "schwab-1m-v2"   # v2 SERVICE_NAME
    assert ev.payload.mode == "replace"
    assert ev.payload.symbols == ["CAST", "VSME"]       # sorted, deterministic


# --------------------------------------------------------------------------- (3)

@pytest.mark.asyncio
async def test_debounced_no_republish_when_unchanged() -> None:
    h = _Harness(enabled=True, watchlist={"VSME", "CAST"})
    await _sync(h)
    await _sync(h)  # same watchlist → must NOT republish
    assert len(h.redis.calls) == 1


# --------------------------------------------------------------------------- (4)

@pytest.mark.asyncio
async def test_republishes_on_watchlist_change() -> None:
    h = _Harness(enabled=True, watchlist={"VSME"})
    await _sync(h)
    h._watchlist = {"VSME", "CAST"}
    await _sync(h)
    assert len(h.redis.calls) == 2
    assert _decode(h.redis.calls[1]).payload.symbols == ["CAST", "VSME"]
