"""Tests for schwab_1m_v2 LEVELONE tick capture (observer-only).

Focus: the LEVELONE field extraction, the default-OFF flag gating (capture off ==
identical pre-feature behavior), the guarantee that LEVELONE never touches the
CHART_EQUITY bar feed, and the writer's bounded buffering.
"""
from __future__ import annotations

import json

import pytest

from project_mai_tai.market_data.schwab_v2_streamer import (
    SchwabTick,
    SchwabV2Streamer,
    _StreamerCreds,
)
from project_mai_tai.market_data.schwab_v2_tick_writer import SchwabV2TickWriter
from project_mai_tai.settings import Settings


async def _noop_bar(symbol, bar):  # noqa: ANN001
    return None


def _streamer(tick_capture, *, on_tick=None, on_bar=None):
    settings = Settings(strategy_schwab_1m_v2_tick_capture_enabled=tick_capture)
    return SchwabV2Streamer(
        settings, on_chart_bar=on_bar or _noop_bar, on_tick=on_tick
    )


def test_extract_levelone_trade_and_quote():
    content = {"key": "GLXG", "1": 1.20, "2": 1.22, "3": 1.21, "4": 100, "5": 200,
               "8": 50000, "9": 300, "35": 1781175600000}
    ticks = SchwabV2Streamer._extract_level_one_ticks(content, item_ts_ms=1781175600500)
    assert {t.kind for t in ticks} == {"trade", "quote"}
    trade = next(t for t in ticks if t.kind == "trade")
    assert trade.price == 1.21 and trade.size == 300 and trade.cumulative_volume == 50000
    assert trade.event_ts_ms == 1781175600000  # field 35 wins for trades
    quote = next(t for t in ticks if t.kind == "quote")
    assert quote.bid_price == 1.20 and quote.ask_price == 1.22
    assert quote.bid_size == 100 and quote.ask_size == 200
    assert quote.event_ts_ms == 1781175600500  # item timestamp for quotes
    assert trade.raw_hash == quote.raw_hash  # same source record


def test_extract_levelone_quote_only_and_missing_symbol():
    q = SchwabV2Streamer._extract_level_one_ticks({"key": "X", "1": 2.0, "2": 2.1}, item_ts_ms=123)
    assert len(q) == 1 and q[0].kind == "quote"
    # No last + no bid/ask -> nothing; no symbol -> nothing.
    assert SchwabV2Streamer._extract_level_one_ticks({"key": "X", "8": 5}, item_ts_ms=123) == []
    assert SchwabV2Streamer._extract_level_one_ticks({"1": 2.0}, item_ts_ms=123) == []


def test_tick_capture_property_gating():
    async def ot(t):  # noqa: ANN001
        return None
    assert _streamer(False, on_tick=ot)._tick_capture is False   # flag off
    assert _streamer(True, on_tick=ot)._tick_capture is True
    assert _streamer(True, on_tick=None)._tick_capture is False   # no callback wired


@pytest.mark.asyncio
async def test_send_subscription_adds_levelone_only_when_capture_on():
    sends = []

    class FakeWs:
        async def send(self, m):  # noqa: ANN001
            sends.append(json.loads(m))

    async def ot(t):  # noqa: ANN001
        return None

    off = _streamer(False, on_tick=ot)
    off._creds = _StreamerCreds("wss://x", "cid", "corr", "ch", "fn")
    await off._send_subscription(FakeWs(), command="SUBS", symbols=["GLXG"])
    assert [r["service"] for r in sends[-1]["requests"]] == ["CHART_EQUITY"]

    on = _streamer(True, on_tick=ot)
    on._creds = _StreamerCreds("wss://x", "cid", "corr", "ch", "fn")
    await on._send_subscription(FakeWs(), command="SUBS", symbols=["GLXG"])
    svcs = [r["service"] for r in sends[-1]["requests"]]
    assert svcs == ["CHART_EQUITY", "LEVELONE_EQUITIES"]
    # distinct requestids per service in the same frame
    rids = [r["requestid"] for r in sends[-1]["requests"]]
    assert len(set(rids)) == 2


@pytest.mark.asyncio
async def test_levelone_tees_to_on_tick_without_touching_bar_feed():
    bars, ticks = [], []

    async def ob(symbol, bar):  # noqa: ANN001
        bars.append(bar)

    async def ot(t):  # noqa: ANN001
        ticks.append(t)

    s = _streamer(True, on_tick=ot, on_bar=ob)
    chart = {"data": [{"service": "CHART_EQUITY", "content": [
        {"key": "GLXG", "2": 1.0, "3": 1.1, "4": 0.9, "5": 1.05, "6": 1000, "7": 1781175600000}]}]}
    await s._handle_message(json.dumps(chart))
    assert len(bars) == 1 and bars[0].symbol == "GLXG"

    lvl = {"data": [{"service": "LEVELONE_EQUITIES", "timestamp": 1781175600500, "content": [
        {"key": "GLXG", "3": 1.07, "9": 50, "8": 1234, "35": 1781175600400}]}]}
    await s._handle_message(json.dumps(lvl))
    assert any(t.kind == "trade" and t.price == 1.07 for t in ticks)
    assert len(bars) == 1  # LEVELONE did NOT add/alter a bar


@pytest.mark.asyncio
async def test_levelone_ignored_when_capture_off():
    ticks = []

    async def ot(t):  # noqa: ANN001
        ticks.append(t)

    s = _streamer(False, on_tick=ot)  # flag off => LEVELONE branch unreachable
    lvl = {"data": [{"service": "LEVELONE_EQUITIES", "timestamp": 1, "content": [
        {"key": "G", "3": 1.0, "35": 1}]}]}
    await s._handle_message(json.dumps(lvl))
    assert ticks == []


@pytest.mark.asyncio
async def test_tick_writer_buffers_and_drops_on_overflow():
    # max_buffer is clamped to >= batch (a buffer can't be smaller than a flush
    # batch), so keep batch small to exercise the overflow drop.
    settings = Settings(
        strategy_schwab_1m_v2_tick_max_buffer=3,
        strategy_schwab_1m_v2_tick_flush_batch_size=2,
    )
    w = SchwabV2TickWriter(settings, session_factory=None)

    def mk(i):
        return SchwabTick(kind="trade", service="LEVELONE_EQUITIES", symbol="G",
                          event_ts_ms=i, raw={}, raw_hash=str(i), price=1.0)

    for i in range(5):
        await w.on_tick(mk(i))
    st = w.stats()
    assert st["buffered"] == 3 and st["dropped"] == 2
