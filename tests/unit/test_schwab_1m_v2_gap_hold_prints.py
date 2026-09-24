from datetime import UTC, datetime

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.market_data.schwab_v2_streamer import SchwabTick
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
async def test_gap_hold_print_clock_ignores_quote_only_updates(monkeypatch) -> None:
    bot = SchwabV2BotService(Settings(strategy_schwab_1m_v2_gap_hold_enabled=True))
    bot.tick_writer = None
    monkeypatch.setattr(bot.strategy, "on_quote", lambda _symbol, _quote: None)
    stamp_ms = int(datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp() * 1000)

    await bot._handle_quote(
        "BENF",
        Quote("BENF", 2.4, 2.5, 2.45, stamp_ms, trade_time_ms=0),
    )
    await bot._handle_stream_tick(
        SchwabTick("quote", "LEVELONE_EQUITIES", "BENF", stamp_ms, {}, "quote")
    )
    assert "BENF" not in bot._gap_last_print_at_ms

    await bot._handle_stream_tick(
        SchwabTick("trade", "LEVELONE_EQUITIES", "BENF", stamp_ms + 1, {}, "trade")
    )
    assert bot._gap_last_print_at_ms["BENF"] == stamp_ms + 1

    await bot._handle_quote(
        "BENF",
        Quote("BENF", 2.4, 2.5, 2.45, stamp_ms + 2, trade_time_ms=stamp_ms + 2),
    )
    assert bot._gap_last_print_at_ms["BENF"] == stamp_ms + 2
