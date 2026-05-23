"""Unit tests for the isolated `schwab_1m_v2` bot.

Merged coverage from PR #225 (warmup window + data-flow watchdog) and
PR #224 (subscribe-early streamer + buffer/replay).

#225 — warmup-resilience / data-flow watchdog:
- REST cold-start warmup window widens past a multi-day market closure
- empty-payload streak tracking (the "REST is dry" signal)
- market-session classification (incl. US market holidays)
- data-flow watchdog health matrix (RTH stall vs expected off-hours
  dryness), gated by quote-liveness so holidays don't false-fire.

#224 — subscribe-early / evaluate-late streamer wiring:
- streamer subscribes to the full watchlist immediately; streamer bars
  arriving before REST warmup completes for a symbol are buffered and
  replayed in timestamp order when warmup finishes
- strategy `on_bar` carries a defense-in-depth out-of-order drop.

All tests instantiate the service/strategy directly (no Redis, no
session_factory) and exercise pure/bar-routing methods — no run() loop,
REST polling, or live streamer connection.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import datetime

from project_mai_tai.market_data.schwab_v2_rest_client import (
    ChartBar,
    Quote,
    SchwabV2RestClient,
)
from project_mai_tai.services.schwab_1m_v2_bot import (
    EASTERN_TZ,
    REST_WARMUP_FRESH_THRESHOLD_SECS,
    STREAMER_PENDING_BARS_MAX_PER_SYMBOL,
    WATCHDOG_STARTUP_GRACE_SECS,
    SchwabV2BotService,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


# ===========================================================================
# PR #225 — warmup window + data-flow watchdog
# ===========================================================================


async def _noop_bar(symbol: str, bar: ChartBar) -> None:  # pragma: no cover
    return None


async def _noop_quote(symbol: str, quote: Quote) -> None:  # pragma: no cover
    return None


def _rest_client(**settings_kwargs) -> SchwabV2RestClient:
    return SchwabV2RestClient(
        Settings(**settings_kwargs),
        on_chart_bar=_noop_bar,
        on_quote=_noop_quote,
    )


def _window_span_days(client: SchwabV2RestClient, since: int) -> float:
    """Run a fetch with `_authorized_get` stubbed and return the requested
    (endDate - startDate) span in days. Clock-independent: both bounds use
    the same now_ms inside the method."""
    captured: dict[str, str] = {}

    def fake_get(url: str) -> dict[str, object]:
        captured["url"] = url
        return {"candles": []}

    client._authorized_get = fake_get  # type: ignore[method-assign]
    client._fetch_recent_closed_bars("AAPL", since)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    start = int(qs["startDate"][0])
    end = int(qs["endDate"][0])
    return (end - start) / (24 * 60 * 60 * 1000)


# --- Change #1: warmup lookback widening -----------------------------------


def test_cold_start_warmup_uses_configured_lookback_window() -> None:
    client = _rest_client(strategy_schwab_1m_v2_warmup_lookback_days=7)
    # since==0 is the cold-start warmup poll -> must reach back the full
    # configured lookback so it survives a Fri->Tue Memorial-Day gap.
    assert _window_span_days(client, since=0) == 7


def test_warmup_lookback_is_configurable() -> None:
    client = _rest_client(strategy_schwab_1m_v2_warmup_lookback_days=10)
    assert _window_span_days(client, since=0) == 10


def test_incremental_poll_uses_24h_window() -> None:
    client = _rest_client(strategy_schwab_1m_v2_warmup_lookback_days=7)
    # since>0 -> incremental; "today" is always within 24h.
    assert _window_span_days(client, since=1) == 1


# --- Change #2: empty-payload streak (the "REST is dry" signal) ------------


def test_empty_payload_increments_streak_and_resets_on_data() -> None:
    client = _rest_client()
    client._authorized_get = lambda url: {"candles": []}  # type: ignore[method-assign]

    for _ in range(3):
        assert client._fetch_recent_closed_bars("AAPL", 0) == []
    assert client.consecutive_empty_polls("AAPL") == 3
    assert client.max_consecutive_empty() == 3

    # A payload with candles resets the streak, even if the candle is one
    # we'd filter out — the streak tracks "raw payload had candles".
    client._authorized_get = lambda url: {  # type: ignore[method-assign]
        "candles": [
            {"datetime": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10}
        ]
    }
    client._fetch_recent_closed_bars("AAPL", 0)
    assert client.consecutive_empty_polls("AAPL") == 0
    assert client.max_consecutive_empty() == 0


# --- market-session helper -------------------------------------------------


def _service(**settings_kwargs) -> SchwabV2BotService:
    return SchwabV2BotService(Settings(**settings_kwargs))


def test_market_session_boundaries() -> None:
    svc = _service()
    # 2026-05-26 is a Tuesday.
    def et(h: int, m: int) -> datetime:
        return datetime(2026, 5, 26, h, m, tzinfo=EASTERN_TZ)

    assert svc._market_session(et(2, 0)) == "closed"
    assert svc._market_session(et(4, 0)) == "premarket"
    assert svc._market_session(et(9, 29)) == "premarket"
    assert svc._market_session(et(9, 30)) == "regular"
    assert svc._market_session(et(15, 59)) == "regular"
    assert svc._market_session(et(16, 0)) == "afterhours"
    assert svc._market_session(et(20, 0)) == "closed"
    # Saturday 2026-05-23 -> closed regardless of clock.
    assert svc._market_session(datetime(2026, 5, 23, 10, 0, tzinfo=EASTERN_TZ)) == "closed"


# --- Change #3: data-flow watchdog health matrix ---------------------------


_REGULAR_MS = int(datetime(2026, 5, 26, 10, 0, tzinfo=EASTERN_TZ).timestamp() * 1000)
_PREMARKET_MS = int(datetime(2026, 5, 26, 6, 0, tzinfo=EASTERN_TZ).timestamp() * 1000)


def _enabled_service() -> SchwabV2BotService:
    return _service(strategy_schwab_1m_v2_enabled=True)


def test_watchdog_disabled_is_degraded() -> None:
    svc = _service()  # enabled defaults False
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "degraded"
    assert detail["data_flow"] == "disabled"


def test_watchdog_empty_watchlist_is_idle_healthy() -> None:
    svc = _enabled_service()
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "healthy"
    assert detail["data_flow"] == "idle_no_watchlist"


def test_watchdog_bars_flowing_is_healthy() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    svc._last_bar_processed_at_ms = _REGULAR_MS - 10_000
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "healthy"
    assert detail["data_flow"] == "flowing"


def test_watchdog_warming_up_grace_suppresses_stall() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    svc._last_bar_processed_at_ms = 0  # never processed a bar
    svc._started_at_ms = _REGULAR_MS - int((WATCHDOG_STARTUP_GRACE_SECS - 30) * 1000)
    svc._last_quote_at_ms = {"AAPL": _REGULAR_MS - 5_000}
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "healthy"
    assert detail["data_flow"] == "warming_up"


def test_watchdog_rth_stall_with_live_quotes_is_degraded_rth() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    svc._last_bar_processed_at_ms = 0
    svc._started_at_ms = _REGULAR_MS - 300_000  # past grace
    svc._last_quote_at_ms = {"AAPL": _REGULAR_MS - 5_000}  # quotes live
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "degraded"
    assert detail["data_flow"] == "stalled_rth"
    assert detail["quotes_live"] == "true"


def test_watchdog_premarket_stall_is_expected_offhours_dry() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    svc._last_bar_processed_at_ms = 0
    svc._started_at_ms = _PREMARKET_MS - 300_000
    svc._last_quote_at_ms = {"AAPL": _PREMARKET_MS - 5_000}  # quotes live pre-market
    status, detail = svc._evaluate_data_flow(_PREMARKET_MS)
    # Quotes flow pre-market but pricehistory is dry by design -> NOT an
    # RTH fault; degraded+expected, surfaced at INFO not WARN.
    assert status == "degraded"
    assert detail["data_flow"] == "stalled_offhours_rest_dry"


def test_holiday_weekday_is_closed_and_not_stalled_rth() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    # 2026 Memorial Day = Mon 2026-05-25 — a weekday holiday (not caught by
    # the weekend check), so this exercises the holiday set specifically.
    holiday_10am = datetime(2026, 5, 25, 10, 0, tzinfo=EASTERN_TZ)
    assert svc._market_session(holiday_10am) == "closed"

    now_ms = int(holiday_10am.timestamp() * 1000)
    svc._last_bar_processed_at_ms = 0  # no bars
    svc._started_at_ms = now_ms - 300_000  # past grace
    svc._last_quote_at_ms = {"AAPL": now_ms - 5_000}  # force quotes "live"
    status, detail = svc._evaluate_data_flow(now_ms)
    # Even with live quotes, a holiday must NOT read as an RTH pipeline fault.
    assert detail["market_session"] == "closed"
    assert detail["data_flow"] != "stalled_rth"


def test_watchdog_stale_quotes_means_market_quiet_not_a_fault() -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    svc._last_bar_processed_at_ms = 0
    svc._started_at_ms = _REGULAR_MS - 300_000
    svc._last_quote_at_ms = {}  # no quotes -> market closed/holiday/thin
    status, detail = svc._evaluate_data_flow(_REGULAR_MS)
    assert status == "healthy"
    assert detail["data_flow"] == "idle_market_quiet"


def test_rth_stall_logs_warning_offhours_does_not(caplog) -> None:
    svc = _enabled_service()
    svc._watchlist = {"AAPL"}
    # RTH stall -> WARN
    with caplog.at_level(logging.WARNING):
        svc._log_data_flow_transition(
            {
                "data_flow": "stalled_rth",
                "secs_since_last_bar": "240",
                "rest_empty_streak_max": "9",
                "market_session": "regular",
            }
        )
    assert any("V2-DATA-STALL" in r.message for r in caplog.records)

    # Off-hours dryness -> no WARN (expected condition).
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        svc._log_data_flow_transition(
            {
                "data_flow": "stalled_offhours_rest_dry",
                "secs_since_last_bar": "240",
                "market_session": "premarket",
            }
        )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ===========================================================================
# PR #224 — subscribe-early streamer + buffer/replay
# ===========================================================================


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
