"""Unit tests for the schwab_1m_v2 bot service's subscribe-early /
evaluate-late wiring.

The streamer subscribes to the full watchlist immediately on
scanner-state arrival; streamer bars received before REST warmup
completes for a symbol are buffered and replayed in timestamp order
when warmup finishes. The strategy's `on_bar` also carries a
defense-in-depth out-of-order drop.

Tests instantiate `SchwabV2BotService` directly with no Redis / no
session_factory and exercise the bar-routing methods. They do NOT
exercise the run() loop, REST polling, or streamer connection — those
are owned by their respective unit tests / integration smoke.
"""
from __future__ import annotations

import asyncio

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import (
    REST_WARMUP_FRESH_THRESHOLD_SECS,
    STREAMER_PENDING_BARS_MAX_PER_SYMBOL,
    SchwabV2BotService,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


def _settings() -> Settings:
    return Settings(
        strategy_schwab_1m_v2_enabled=True,
        scanner_feed_retention_enabled=False,
    )


def _bot() -> SchwabV2BotService:
    return SchwabV2BotService(settings=_settings(), session_factory=None)


def _bar(ts_ms: int, *, close: float = 1.0, volume: int = 100) -> ChartBar:
    return ChartBar(
        symbol="AAA",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        timestamp_ms=ts_ms,
    )


def _now_ms_at_age(age_secs: float) -> int:
    """Compute a `bar.timestamp_ms` whose age (computed inside
    `_handle_bar_from_rest`) is approximately `age_secs`.
    """
    import datetime as _dt

    now_ms = int(_dt.datetime.now(_dt.UTC).timestamp() * 1000)
    return now_ms - int(age_secs * 1000)


def test_streamer_bar_before_warmup_is_buffered_not_fed_to_strategy() -> None:
    bot = _bot()
    bar = _bar(ts_ms=_now_ms_at_age(30.0))

    asyncio.run(bot._handle_bar_from_streamer("AAA", bar))

    assert "AAA" in bot._streamer_pending
    assert bot._streamer_pending["AAA"] == [bar]
    assert len(bot.strategy.watchlist_state("AAA").bars) == 0


def test_streamer_bar_after_warmup_is_fed_directly_no_buffer() -> None:
    bot = _bot()
    bot._rest_warmup_done.add("AAA")

    bar = _bar(ts_ms=_now_ms_at_age(30.0))
    asyncio.run(bot._handle_bar_from_streamer("AAA", bar))

    assert "AAA" not in bot._streamer_pending
    state = bot.strategy.watchlist_state("AAA")
    assert len(state.bars) == 1
    assert state.bars[-1].timestamp_ms == bar.timestamp_ms


def test_warmup_completion_drains_buffer_in_timestamp_order() -> None:
    bot = _bot()

    # Streamer pushes three bars BEFORE warmup, intentionally
    # out-of-order to prove drain sorts them.
    t_base = _now_ms_at_age(60.0)
    early = _bar(ts_ms=t_base + 120_000)
    middle = _bar(ts_ms=t_base + 60_000)
    late = _bar(ts_ms=t_base + 180_000)
    asyncio.run(bot._handle_bar_from_streamer("AAA", early))
    asyncio.run(bot._handle_bar_from_streamer("AAA", middle))
    asyncio.run(bot._handle_bar_from_streamer("AAA", late))
    assert len(bot._streamer_pending["AAA"]) == 3

    # REST delivers the warmup-completing bar. Its timestamp is older
    # than all three pending bars, so all three should drain in
    # ascending timestamp order on top of it.
    rest_bar = _bar(ts_ms=t_base)
    asyncio.run(bot._handle_bar_from_rest("AAA", rest_bar))

    assert "AAA" in bot._rest_warmup_done
    assert "AAA" not in bot._streamer_pending
    state = bot.strategy.watchlist_state("AAA")
    timestamps = [b.timestamp_ms for b in state.bars]
    assert timestamps == [
        t_base,
        t_base + 60_000,
        t_base + 120_000,
        t_base + 180_000,
    ]


def test_drain_skips_buffered_bars_older_or_equal_to_latest_deque_bar() -> None:
    bot = _bot()

    # Buffer one bar that's OLDER than the upcoming REST bar — it
    # should be dropped on drain, not appended out-of-order.
    t_base = _now_ms_at_age(60.0)
    stale = _bar(ts_ms=t_base - 60_000)
    same = _bar(ts_ms=t_base)
    fresh = _bar(ts_ms=t_base + 60_000)
    asyncio.run(bot._handle_bar_from_streamer("AAA", stale))
    asyncio.run(bot._handle_bar_from_streamer("AAA", same))
    asyncio.run(bot._handle_bar_from_streamer("AAA", fresh))

    rest_bar = _bar(ts_ms=t_base)
    asyncio.run(bot._handle_bar_from_rest("AAA", rest_bar))

    state = bot.strategy.watchlist_state("AAA")
    timestamps = [b.timestamp_ms for b in state.bars]
    # `stale` (t_base-60s) and `same` (t_base) are <= rest_bar
    # timestamp; only `fresh` survives the drain.
    assert timestamps == [t_base, t_base + 60_000]


def test_buffer_cap_drops_oldest_when_full() -> None:
    bot = _bot()

    overflow = STREAMER_PENDING_BARS_MAX_PER_SYMBOL + 1
    t_base = _now_ms_at_age(3600.0)
    for i in range(overflow):
        bar = _bar(ts_ms=t_base + i * 60_000)
        asyncio.run(bot._handle_bar_from_streamer("AAA", bar))

    pending = bot._streamer_pending["AAA"]
    assert len(pending) == STREAMER_PENDING_BARS_MAX_PER_SYMBOL
    # Oldest (i=0) dropped; the kept window starts at i=1.
    assert pending[0].timestamp_ms == t_base + 60_000
    assert pending[-1].timestamp_ms == t_base + overflow * 60_000 - 60_000


def test_watchlist_transition_drops_pending_for_removed_symbols() -> None:
    bot = _bot()

    bot._streamer_pending["AAA"] = [_bar(ts_ms=_now_ms_at_age(30.0))]
    bot._streamer_pending["BBB"] = [_bar(ts_ms=_now_ms_at_age(30.0))]
    bot._streamer_pending["CCC"] = [_bar(ts_ms=_now_ms_at_age(30.0))]
    bot._rest_warmup_done.update({"AAA", "BBB", "CCC"})

    # Build a minimal StrategyStateSnapshotEvent that drops CCC.
    from project_mai_tai.events import (
        StrategyStateSnapshotEvent,
        StrategyStateSnapshotPayload,
    )

    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            watchlist=["AAA", "BBB"],
        ),
    )
    data = {"data": event.model_dump_json()}
    bot._apply_strategy_state_event(data, max_watchlist=25)

    assert bot._watchlist == {"AAA", "BBB"}
    assert "CCC" not in bot._streamer_pending
    assert "CCC" not in bot._rest_warmup_done
    assert "AAA" in bot._streamer_pending
    assert "BBB" in bot._streamer_pending


def test_warmup_completion_only_fires_on_fresh_bar_not_old_one() -> None:
    bot = _bot()

    # REST delivers an OLD bar (older than freshness threshold). It
    # should be fed to the strategy but NOT mark the symbol warmed,
    # and NOT drain the buffer.
    pending_bar = _bar(ts_ms=_now_ms_at_age(30.0))
    asyncio.run(bot._handle_bar_from_streamer("AAA", pending_bar))

    old_rest_bar = _bar(
        ts_ms=_now_ms_at_age(REST_WARMUP_FRESH_THRESHOLD_SECS + 60.0)
    )
    asyncio.run(bot._handle_bar_from_rest("AAA", old_rest_bar))

    assert "AAA" not in bot._rest_warmup_done
    assert bot._streamer_pending.get("AAA") == [pending_bar]
    # Strategy received the REST bar (warmup feed), and only it.
    state = bot.strategy.watchlist_state("AAA")
    assert [b.timestamp_ms for b in state.bars] == [old_rest_bar.timestamp_ms]


def test_strategy_on_bar_drops_out_of_order_bar_defense_in_depth() -> None:
    strategy = SchwabV2Strategy(_settings())

    t_base = _now_ms_at_age(60.0)
    strategy.on_bar("AAA", _bar(ts_ms=t_base))
    state = strategy.watchlist_state("AAA")
    assert len(state.bars) == 1

    # Out-of-order bar: should be silently dropped (return None) and
    # NOT appended.
    result = strategy.on_bar("AAA", _bar(ts_ms=t_base - 60_000))
    assert result is None
    assert len(state.bars) == 1
    assert state.bars[-1].timestamp_ms == t_base
