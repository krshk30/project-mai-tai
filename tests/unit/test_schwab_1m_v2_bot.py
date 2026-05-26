"""Unit tests for the isolated `schwab_1m_v2` bot.

Covers the warmup-resilience / data-flow-watchdog work:
- REST cold-start warmup window widens past a multi-day market closure
- empty-payload streak tracking (the "REST is dry" signal)
- market-session classification
- the data-flow watchdog health matrix (RTH stall vs expected off-hours
  dryness), gated by quote-liveness so holidays don't false-fire.
"""
from __future__ import annotations

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
    WATCHDOG_STARTUP_GRACE_SECS,
    SchwabV2BotService,
)
from project_mai_tai.settings import Settings


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
