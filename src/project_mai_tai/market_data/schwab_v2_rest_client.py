"""Dedicated Schwab REST client for the isolated `schwab_1m_v2` bot.

This module shares NO code with `schwab_streamer.py`, `SchwabBrokerAdapter`,
or any other existing Schwab integration. It polls Schwab's Price History
and Quotes REST endpoints directly for the v2 bot's watchlist.

Token handling: the v2 bot reads the existing Schwab access token from
`settings.schwab_token_store_path` on each poll. It does NOT refresh tokens
itself — the existing services already handle the refresh cycle, and we
piggyback on whatever they write. If our REST call returns 401, we log and
back off; the next read picks up the refreshed token.

Cadence (configurable):
- bars: round-robin through watchlist, one symbol per `bar_poll_interval_seconds`
- quotes: batched, all watchlist symbols, every `quote_poll_interval_seconds`

Schwab REST quota is ~120 RPM/account; tune watchlist size + cadence
accordingly.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from project_mai_tai.market_data.schwab_v2_loop_health import (
    LoopHealthTracker,
    run_resilient_loop,
    sleep_or_stop,
)
from project_mai_tai.settings import Settings

logger = logging.getLogger(__name__)


def anchored_session_poll_open(now_ms: int) -> bool:
    """LINESRC1: only the anchored source lane polls from 07:00 through 15:59 ET."""
    eastern = datetime.fromtimestamp(now_ms / 1000, UTC).astimezone(ZoneInfo("America/New_York"))
    return 7 * 60 <= eastern.hour * 60 + eastern.minute < 16 * 60


ChartBarCallback = Callable[[str, "ChartBar"], Awaitable[None]]
QuoteCallback = Callable[[str, "Quote"], Awaitable[None]]


@dataclass
class ChartBar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp_ms: int


@dataclass
class Quote:
    symbol: str
    bid_price: float
    ask_price: float
    last_price: float
    quote_time_ms: int
    cumulative_volume: int | None = None
    trade_time_ms: int = 0


class SchwabV2RestClient:
    """Minimal REST-poll client for the v2 bot.

    Idle (no-op) when the Schwab token store is unset, so the service can
    boot without credentials.
    """

    PRICE_HISTORY_PATH = "/marketdata/v1/pricehistory"
    QUOTES_PATH = "/marketdata/v1/quotes"

    def __init__(
        self,
        settings: Settings,
        *,
        on_chart_bar: ChartBarCallback,
        on_quote: QuoteCallback,
        loop_health: LoopHealthTracker | None = None,
    ) -> None:
        self.settings = settings
        self._on_chart_bar = on_chart_bar
        self._on_quote = on_quote
        # SPOF Workstream A (v2): shared loop-health tracker (the bot passes its
        # own so bar/quote-loop failures surface in the bot heartbeat). Falls back
        # to a private one so the client is self-contained in standalone use/tests.
        self._loop_health = loop_health or LoopHealthTracker(
            persistent_failure_threshold=int(
                getattr(settings, "strategy_schwab_1m_v2_loop_persistent_failure_threshold", 3)
            ),
            logger=logger,
        )
        self._loop_backoff_secs = max(
            0.0,
            float(getattr(settings, "strategy_schwab_1m_v2_loop_error_backoff_seconds", 1.0)),
        )
        self._desired_symbols: set[str] = set()
        self._symbols_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._last_bar_timestamp_ms: dict[str, int] = {}
        # Per-symbol count of consecutive polls whose raw Schwab payload had
        # ZERO candles (the "REST source is dry" signal — distinct from
        # "had candles but nothing new since the cursor"). Reset to 0 the
        # moment a payload comes back with any candles. Surfaced via the
        # service heartbeat so prolonged emptiness is observable instead of
        # silent. See `max_consecutive_empty`.
        self._consecutive_empty: dict[str, int] = {}
        self._session_request = None
        self._on_session_history = None
        self._on_session_failure = None
        self._line_source_states: dict[str, tuple[str, str]] = {}

    @property
    def configured(self) -> bool:
        return bool((self.settings.schwab_token_store_path or "").strip())

    def set_desired_symbols(self, symbols: set[str]) -> None:
        normalized = {s.strip().upper() for s in symbols if s.strip()}
        # set_desired is called from the engine loop without await — assign
        # atomically (no async lock needed for set replacement on a single
        # event loop).
        self._desired_symbols = normalized

    def consecutive_empty_polls(self, symbol: str) -> int:
        """Consecutive empty-payload polls for `symbol` (0 if last poll
        returned candles)."""
        return self._consecutive_empty.get(symbol, 0)

    def max_consecutive_empty(self) -> int:
        """Largest consecutive empty-payload streak across all symbols —
        a single scalar the heartbeat can surface as the 'REST is dry'
        signal."""
        return max(self._consecutive_empty.values(), default=0)

    async def run(self) -> None:
        if not self.configured:
            logger.warning(
                "schwab_v2_rest_client idle: schwab_token_store_path is empty. "
                "Set MAI_TAI_SCHWAB_TOKEN_STORE_PATH and complete OAuth via "
                "/auth/schwab/start before the v2 bot can poll."
            )
            await self._stop_event.wait()
            return

        await asyncio.gather(
            self._bar_loop(),
            self._quote_loop(),
        )

    async def stop(self) -> None:
        self._stop_event.set()

    async def _bar_loop(self) -> None:
        # SPOF Workstream A (v2): the per-task backstop. A pass = one round-robin
        # over the watchlist. The per-symbol fetch keeps its own guard (one bad
        # symbol doesn't abort the pass); the backstop catches anything else
        # (incl. the E1 `_on_chart_bar` callback, previously unguarded at the
        # loop level), so the bar loop can never silently die.
        interval = max(0.5, float(self.settings.strategy_schwab_1m_v2_bar_poll_interval_seconds))
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="bar_loop",
            iteration=lambda: self._bar_loop_pass(interval),
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
        )

    async def _bar_loop_pass(self, interval: float) -> None:
        symbols = sorted(self._desired_symbols)
        if not symbols:
            await asyncio.sleep(interval)
            return
        if self._session_request is not None:
            await self._anchored_bar_loop_pass(symbols, interval)
            return
        cycle = itertools.cycle(symbols)
        for _ in range(len(symbols)):
            if self._stop_event.is_set():
                return
            symbol = next(cycle)
            since = self._last_bar_timestamp_ms.get(symbol, 0)
            try:
                bars = await asyncio.to_thread(
                    self._fetch_recent_closed_bars, symbol, since
                )
            except Exception as exc:  # noqa: BLE001 — per-symbol fetch guard
                logger.warning("schwab_v2 bar poll failed for %s: %s", symbol, exc)
                await asyncio.sleep(interval)
                continue
            if bars:
                # First poll per symbol returns up to ~500 candles (24h of
                # 1-min bars). Subsequent polls return only the 1-2 bars
                # that closed since the previous poll. Feeding all of them
                # warms up the strategy's indicator state instantly; the
                # strategy's freshness guard ensures only bars within
                # ~3 min of wall clock can fire signals.
                if since == 0 and len(bars) > 5:
                    logger.info(
                        "schwab_v2 warmup feed for %s: %d bars from %s..%s",
                        symbol,
                        len(bars),
                        bars[0].timestamp_ms,
                        bars[-1].timestamp_ms,
                    )
                for bar in bars:
                    if bar.timestamp_ms > self._last_bar_timestamp_ms.get(symbol, 0):
                        self._last_bar_timestamp_ms[symbol] = bar.timestamp_ms
                        await self._on_chart_bar(symbol, bar)
            await asyncio.sleep(interval)

    async def _anchored_bar_loop_pass(self, symbols: list[str], interval: float) -> None:
        # Reuse the normal bar request budget. At most four requests are in
        # flight; the configured interval is a cycle cadence in this lane.
        started = time.monotonic()
        semaphore = asyncio.Semaphore(4)
        requests = 0

        async def poll(symbol):
            nonlocal requests
            context = None
            try:
                async with semaphore:
                    if self._stop_event.is_set():
                        return
                    context = self._session_request(symbol)
                    if context is None:
                        return
                    epoch, anchor, current = context
                    if not anchored_session_poll_open(current + 60_000):
                        return
                    if not anchored_session_poll_open(current):
                        return
                    requests += 1
                    bars, proof = await asyncio.to_thread(
                        self.fetch_session_history, symbol, anchor, current,
                    )
                if proof is None:
                    # No current close is not source corruption or a failed epoch.
                    # The service retains its old proof but fences new entries.
                    self._on_session_history(symbol, epoch, bars, None)
                    self._set_line_source_state(symbol, ("waiting", "current_closed_candle_pending"))
                    return
                if not self._on_session_history(symbol, epoch, bars, proof):
                    return
                self._set_line_source_state(symbol, ("ready", "current_closed_candle_proven"))
                # Persistence never occupies a source-fetch semaphore slot.
                await self._on_chart_bar(symbol, bars[-1])
                self._last_bar_timestamp_ms[symbol] = bars[-1].timestamp_ms
            except Exception as exc:  # noqa: BLE001 - fail actual source errors closed
                if context is not None:
                    self._on_session_failure(symbol, context[0])
                self._set_line_source_state(symbol, ("error", f"{type(exc).__name__}: {exc}"))

        await asyncio.gather(*(poll(symbol) for symbol in symbols))
        elapsed = time.monotonic() - started
        # Reserve 30 RPM for quotes/other REST consumers, even on larger lists.
        cadence = max(0.5, interval, len(symbols) * 60 / 90)
        if requests:
            logger.info("[V2-LINE-SOURCE-CYCLE] symbols=%d elapsed_s=%.3f cadence_s=%.3f concurrency=4",
                        len(symbols), elapsed, cadence)
        await sleep_or_stop(self._stop_event, max(0, cadence - elapsed))

    def _set_line_source_state(self, symbol: str, state: tuple[str, str]) -> None:
        previous = self._line_source_states.get(symbol)
        if previous is not None and previous[0] == state[0]:
            return
        self._line_source_states[symbol] = state
        if state[0] != "ready" or previous is not None:
            logger.warning("[V2-LINE-SOURCE-STATE] sym=%s state=%s reason=%s", symbol, *state)

    async def _quote_loop(self) -> None:
        interval = max(0.5, float(self.settings.strategy_schwab_1m_v2_quote_poll_interval_seconds))
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="quote_loop",
            iteration=lambda: self._quote_loop_pass(interval),
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
        )

    async def _quote_loop_pass(self, interval: float) -> None:
        symbols = sorted(self._desired_symbols)
        if not symbols:
            await asyncio.sleep(interval)
            return
        try:
            quotes = await asyncio.to_thread(self._fetch_quotes, symbols)
        except Exception as exc:  # noqa: BLE001 — fetch guard
            logger.warning("schwab_v2 quote poll failed: %s", exc)
            await asyncio.sleep(interval)
            return
        for quote in quotes:
            await self._on_quote(quote.symbol, quote)
        await asyncio.sleep(interval)

    def _read_access_token(self) -> str | None:
        path = (self.settings.schwab_token_store_path or "").strip()
        if not path:
            return None
        try:
            document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("schwab_v2 token store unreadable: %s", exc)
            return None
        token = str(document.get("access_token", "")).strip()
        return token or None

    def _authorized_get(self, url: str) -> dict[str, object]:
        token = self._read_access_token()
        if not token:
            raise RuntimeError("schwab access token unavailable")
        request = UrlRequest(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.settings.schwab_request_timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"schwab REST {exc.code}: {detail or exc}") from exc
        except URLError as exc:
            raise RuntimeError(f"schwab REST transport error: {exc}") from exc
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise RuntimeError("schwab REST returned non-object payload")
        return payload

    def _fetch_recent_closed_bars(
        self, symbol: str, since_ts_ms: int
    ) -> list[ChartBar]:
        """Return all closed candles strictly newer than `since_ts_ms`,
        sorted ascending. "Closed" means timestamp <= (now - 60s) — Schwab's
        last candle in the array can be the in-flight current minute.

        First call per symbol (`since=0`) is the cold-start warmup: it
        requests `strategy_schwab_1m_v2_warmup_lookback_days` back so the
        indicator-seed batch reaches the last completed trading session
        even across a multi-day closure. A fixed 24h window returns an
        EMPTY array after a weekend+holiday gap (e.g. Fri->Tue Memorial
        Day), silently starving warmup. Subsequent calls (`since>0`) use a
        24h window — "today" is always within 24h and the since-filter
        trims to the 1-2 newly-closed bars.

        Schwab pricehistory gotcha (verified 2026-05-22): `periodType=day&
        period=1` returns the last fully-closed trading session, NOT
        "today so far." Using explicit startDate/endDate avoids this.
        """
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if since_ts_ms <= 0:
            lookback_days = max(
                1, int(self.settings.strategy_schwab_1m_v2_warmup_lookback_days)
            )
            start_ms = now_ms - lookback_days * 24 * 60 * 60 * 1000
        else:
            start_ms = now_ms - 24 * 60 * 60 * 1000
        params = urlencode(
            {
                "symbol": symbol,
                "periodType": "day",
                "frequencyType": "minute",
                "frequency": 1,
                "startDate": start_ms,
                "endDate": now_ms,
                "needExtendedHoursData": "true",
            }
        )
        url = f"{self.settings.schwab_base_url.rstrip('/')}{self.PRICE_HISTORY_PATH}?{params}"
        payload = self._authorized_get(url)
        candles = payload.get("candles")
        if not isinstance(candles, list) or not candles:
            # Schwab returned a 200 with no candles (dry source — e.g.
            # pricehistory does not serve same-day pre/after-hours minutes,
            # or the window spans only non-trading days). Track the streak
            # so the service can surface prolonged emptiness.
            self._consecutive_empty[symbol] = (
                self._consecutive_empty.get(symbol, 0) + 1
            )
            return []
        self._consecutive_empty.pop(symbol, None)
        cutoff_ms = now_ms - 60_000
        bars: list[ChartBar] = []
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            ts_raw = candle.get("datetime", 0) or 0
            try:
                ts = int(ts_raw)
            except (TypeError, ValueError):
                continue
            if ts > cutoff_ms:
                continue
            if ts <= since_ts_ms:
                continue
            try:
                bars.append(
                    ChartBar(
                        symbol=symbol,
                        open=float(candle.get("open", 0.0) or 0.0),
                        high=float(candle.get("high", 0.0) or 0.0),
                        low=float(candle.get("low", 0.0) or 0.0),
                        close=float(candle.get("close", 0.0) or 0.0),
                        volume=int(float(candle.get("volume", 0) or 0)),
                        timestamp_ms=ts,
                    )
                )
            except (TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b.timestamp_ms)
        return bars

    def fetch_session_history(self, symbol: str, anchor_ms: int, current_bar_ms: int):
        """Return anchored bars and proof, or bars/None while the current close is absent.

        This request has no delivery cursor or seed cap. Completeness is from
        the anchored provider response, not the number of candles returned.
        """
        from project_mai_tai.strategy_core.session_line_restore import (
            SessionCoverage, SessionLineRestoration, history_fingerprint,
        )

        if not anchored_session_poll_open(current_bar_ms + 60_000):
            return [], None
        if not anchored_session_poll_open(current_bar_ms):
            return [], None
        end_ms = current_bar_ms + 60_000
        if not anchor_ms <= current_bar_ms < anchor_ms + 16 * 3_600_000:
            raise ValueError("invalid restoration session window")
        params = urlencode({
            "symbol": symbol, "periodType": "day", "frequencyType": "minute",
            "frequency": 1, "startDate": anchor_ms, "endDate": end_ms - 1,
            "needExtendedHoursData": "true",
        })
        payload = self._authorized_get(
            f"{self.settings.schwab_base_url.rstrip('/')}{self.PRICE_HISTORY_PATH}?{params}"
        )
        candles = payload.get("candles")
        if (str(payload.get("symbol", "")).upper() != symbol.upper()
                or (payload.get("empty") is not False and payload.get("empty") is not True)
                or not isinstance(candles, list) or len(candles) > 960
                or any(payload.get(key) for key in ("next", "nextToken", "nextPage", "truncated"))):
            raise ValueError("session response completeness unproven")
        if not candles:
            # A valid empty response has no bars or completeness proof yet.
            return [], None
        if payload.get("empty") is True:
            raise ValueError("nonempty session response marked empty")
        validator = SessionLineRestoration(symbol, anchor_ms, 0)
        bars = []
        ids = set()
        for candle in candles:
            if not isinstance(candle, dict):
                raise ValueError("malformed session candle")
            bar = ChartBar(
                symbol.upper(), float(candle["open"]), float(candle["high"]),
                float(candle["low"]), float(candle["close"]),
                int(candle["volume"]), int(candle["datetime"]),
            )
            if not anchor_ms <= bar.timestamp_ms <= current_bar_ms or bar.timestamp_ms in ids:
                raise ValueError("foreign or duplicate session candle")
            validator.observe(bar)
            ids.add(bar.timestamp_ms)
            bars.append(bar)
        bars.sort(key=lambda bar: bar.timestamp_ms)
        if bars[-1].timestamp_ms != current_bar_ms:
            return bars, None
        return bars, SessionCoverage(
            "schwab_rest_full_session", anchor_ms, end_ms,
            tuple(bar.timestamp_ms for bar in bars), True, history_fingerprint(bars),
            prefix_complete=True,
        )

    def _fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        params = urlencode(
            {
                "symbols": ",".join(symbols),
                "fields": "quote",
            }
        )
        url = f"{self.settings.schwab_base_url.rstrip('/')}{self.QUOTES_PATH}?{params}"
        payload = self._authorized_get(url)
        results: list[Quote] = []
        for symbol, record in payload.items():
            if not isinstance(record, dict):
                continue
            quote = record.get("quote")
            if not isinstance(quote, dict):
                continue
            # tradeTime is HALT1 observability only. A malformed optional timestamp must
            # never discard the bid/ask update that drives the live strategy.
            try:
                trade_time_ms = int(
                    quote.get("tradeTime")
                    or quote.get("tradeTimeInLong")
                    or quote.get("trade_time")
                    or 0
                )
            except (TypeError, ValueError, OverflowError):
                trade_time_ms = 0
            try:
                results.append(
                    Quote(
                        symbol=str(symbol).upper(),
                        bid_price=float(quote.get("bidPrice", 0.0) or 0.0),
                        ask_price=float(quote.get("askPrice", 0.0) or 0.0),
                        last_price=float(quote.get("lastPrice", 0.0) or 0.0),
                        quote_time_ms=int(quote.get("quoteTime", 0) or 0),
                        cumulative_volume=int(quote.get("totalVolume", 0) or 0),
                        trade_time_ms=trade_time_ms,
                    )
                )
            except (TypeError, ValueError):
                continue
        return results
