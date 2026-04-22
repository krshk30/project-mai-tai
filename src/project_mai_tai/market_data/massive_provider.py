from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from datetime import date, timedelta

from project_mai_tai.market_data.models import (
    HistoricalBarRecord,
    LiveBarRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)

logger = logging.getLogger(__name__)


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MassiveSnapshotProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def fetch_all_snapshots(self) -> list[SnapshotRecord]:
        client = self._get_rest_client()
        snapshots = client.get_snapshot_all("stocks", include_otc=False)
        return [self._normalize_snapshot(snapshot) for snapshot in snapshots if getattr(snapshot, "ticker", None)]

    def get_grouped_daily_multi(self, days: int = 20) -> dict[str, list[float]]:
        client = self._get_rest_client()
        volume_by_ticker: dict[str, list[float]] = {}
        check_date = date.today() - timedelta(days=1)
        fetched_days = 0
        max_lookback = days + 15

        for _ in range(max_lookback):
            if fetched_days >= days:
                break
            if check_date.weekday() >= 5:
                check_date -= timedelta(days=1)
                continue

            aggs = client.get_grouped_daily_aggs(
                date=check_date.strftime("%Y-%m-%d"),
                adjusted=True,
                include_otc=False,
            )
            if aggs:
                fetched_days += 1
                for agg in aggs:
                    ticker = getattr(agg, "ticker", None)
                    volume = getattr(agg, "volume", None)
                    if not ticker or not volume or volume <= 0:
                        continue
                    volume_by_ticker.setdefault(ticker, []).append(float(volume))

            check_date -= timedelta(days=1)
            time.sleep(0.1)

        return volume_by_ticker

    def get_ticker_details_batch(
        self,
        tickers: list[str],
        batch_size: int = 10,
        delay_between_batches: float = 0.2,
    ) -> dict[str, int]:
        client = self._get_rest_client()
        result: dict[str, int] = {}

        for index in range(0, len(tickers), batch_size):
            batch = tickers[index : index + batch_size]
            for ticker in batch:
                try:
                    details = client.get_ticker_details(ticker)
                except Exception:
                    logger.exception("Failed to fetch Massive ticker details for %s", ticker)
                    continue

                shares = getattr(details, "share_class_shares_outstanding", None)
                if shares is None:
                    shares = getattr(details, "weighted_shares_outstanding", None)
                if shares is None:
                    continue
                result[ticker] = int(shares)

            if index + batch_size < len(tickers):
                time.sleep(delay_between_batches)

        return result

    def fetch_historical_bars(
        self,
        symbol: str,
        *,
        interval_secs: int,
        lookback_calendar_days: int,
        limit: int,
    ) -> list[HistoricalBarRecord]:
        client = self._get_rest_client()
        multiplier, timespan = self._resolve_agg_interval(interval_secs)
        from_date = date.today() - timedelta(days=max(1, lookback_calendar_days))
        to_date = date.today()
        aggs = client.list_aggs(
            symbol,
            multiplier,
            timespan,
            from_=from_date.strftime("%Y-%m-%d"),
            to=to_date.strftime("%Y-%m-%d"),
            limit=limit,
        )
        bars: list[HistoricalBarRecord] = []
        for agg in aggs:
            close = _to_float(getattr(agg, "close", None))
            timestamp_raw = _to_int(getattr(agg, "timestamp", None))
            if close is None or timestamp_raw is None:
                continue
            timestamp = timestamp_raw / 1000 if timestamp_raw > 1_000_000_000_000 else float(timestamp_raw)
            bars.append(
                HistoricalBarRecord(
                    open=_to_float(getattr(agg, "open", None)) or close,
                    high=_to_float(getattr(agg, "high", None)) or close,
                    low=_to_float(getattr(agg, "low", None)) or close,
                    close=close,
                    volume=_to_int(getattr(agg, "volume", None)) or 0,
                    timestamp=timestamp,
                    trade_count=_to_int(getattr(agg, "transactions", None))
                    or _to_int(getattr(agg, "trade_count", None))
                    or 1,
                )
            )
        return bars

    def _get_rest_client(self):
        if self._client is None:
            try:
                from massive import RESTClient
            except ImportError as exc:
                raise RuntimeError(
                    "The 'massive' package is required for live market-data polling."
                ) from exc
            self._client = RESTClient(api_key=self.api_key)
        return self._client

    def _resolve_agg_interval(self, interval_secs: int) -> tuple[int, str]:
        if interval_secs < 60:
            return interval_secs, "second"
        if interval_secs % 60 != 0:
            raise ValueError(f"Unsupported interval for historical bars: {interval_secs}s")
        return interval_secs // 60, "minute"

    def _normalize_snapshot(self, snapshot) -> SnapshotRecord:
        day = getattr(snapshot, "day", None)
        minute = getattr(snapshot, "min", None) or getattr(snapshot, "minute", None)
        prev_day = getattr(snapshot, "prev_day", None)
        last_trade = getattr(snapshot, "last_trade", None)
        last_quote = getattr(snapshot, "last_quote", None)

        return SnapshotRecord(
            symbol=str(getattr(snapshot, "ticker")),
            previous_close=_to_float(getattr(prev_day, "close", None)),
            day_close=_to_float(getattr(day, "close", None)),
            day_volume=_to_int(getattr(day, "volume", None)),
            day_high=_to_float(getattr(day, "high", None)),
            day_vwap=_to_float(getattr(day, "vwap", None)),
            minute_close=_to_float(getattr(minute, "close", None)),
            minute_accumulated_volume=_to_int(getattr(minute, "accumulated_volume", None)),
            minute_high=_to_float(getattr(minute, "high", None)),
            minute_vwap=_to_float(getattr(minute, "vwap", None)),
            last_trade_price=_to_float(getattr(last_trade, "price", None)),
            last_trade_timestamp_ns=_to_int(
                getattr(last_trade, "sip_timestamp", None) or getattr(last_trade, "timestamp", None)
            ),
            bid_price=_to_float(getattr(last_quote, "bid_price", None)),
            ask_price=_to_float(getattr(last_quote, "ask_price", None)),
            bid_size=_to_int(getattr(last_quote, "bid_size", None)),
            ask_size=_to_int(getattr(last_quote, "ask_size", None)),
            todays_change_percent=_to_float(getattr(snapshot, "todays_change_percent", None)),
            updated_ns=_to_int(getattr(snapshot, "updated", None) or getattr(snapshot, "updated_ns", None)),
        )


class MassiveTradeStream:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._ws = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._connected = False
        self._subscriptions: set[str] = set()
        self._on_trade: Callable[[TradeTickRecord], None] | None = None
        self._on_quote: Callable[[QuoteTickRecord], None] | None = None
        self._on_agg: Callable[[LiveBarRecord], None] | None = None

    async def start(
        self,
        on_trade: Callable[[TradeTickRecord], None],
        on_quote: Callable[[QuoteTickRecord], None] | None = None,
        on_agg: Callable[[LiveBarRecord], None] | None = None,
    ) -> None:
        self._on_trade = on_trade
        self._on_quote = on_quote
        self._on_agg = on_agg
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                logger.exception("Failed to close Massive websocket cleanly")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False

    async def sync_subscriptions(self, symbols: Iterable[str]) -> None:
        desired = {symbol.upper() for symbol in symbols if symbol}
        to_remove = self._subscriptions - desired
        to_add = desired - self._subscriptions
        self._subscriptions = desired

        if self._ws is None or not self._connected:
            return

        if to_remove:
            self._ws.unsubscribe(*[f"T.{symbol}" for symbol in sorted(to_remove)])
            self._ws.unsubscribe(*[f"Q.{symbol}" for symbol in sorted(to_remove)])
            if self._on_agg is not None:
                self._ws.unsubscribe(*[f"A.{symbol}" for symbol in sorted(to_remove)])
        if to_add:
            self._ws.subscribe(*[f"T.{symbol}" for symbol in sorted(to_add)])
            self._ws.subscribe(*[f"Q.{symbol}" for symbol in sorted(to_add)])
            if self._on_agg is not None:
                self._ws.subscribe(*[f"A.{symbol}" for symbol in sorted(to_add)])

    async def _run_loop(self) -> None:
        while self._running:
            try:
                ws = self._build_client()
                self._ws = ws
                if self._subscriptions:
                    ws.subscribe(*[f"T.{symbol}" for symbol in sorted(self._subscriptions)])
                    ws.subscribe(*[f"Q.{symbol}" for symbol in sorted(self._subscriptions)])
                    if self._on_agg is not None:
                        ws.subscribe(*[f"A.{symbol}" for symbol in sorted(self._subscriptions)])
                self._connected = True
                await asyncio.to_thread(ws.run, self._handle_messages)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                if self._running:
                    logger.exception("Massive websocket error; reconnecting in 5 seconds")
                    await asyncio.sleep(5)

    def _build_client(self):
        try:
            from massive import WebSocketClient
        except ImportError as exc:
            raise RuntimeError(
                "The 'massive' package is required for live market-data streaming."
            ) from exc

        return WebSocketClient(api_key=self.api_key, subscriptions=[])

    def _handle_messages(self, messages) -> None:
        for message in messages:
            try:
                event_type = getattr(message, "event_type", None) or getattr(message, "ev", None)
                symbol = getattr(message, "symbol", None)
                if not symbol or symbol not in self._subscriptions:
                    continue

                if event_type == "T" and self._on_trade is not None:
                    price = _to_float(getattr(message, "price", None))
                    if price is None or price <= 0:
                        continue
                    self._on_trade(
                        TradeTickRecord(
                            symbol=symbol,
                            price=price,
                            size=int(getattr(message, "size", 0) or 0),
                            timestamp_ns=_to_int(
                                getattr(message, "sip_timestamp", None) or getattr(message, "timestamp", None)
                            ),
                            exchange=str(getattr(message, "exchange", "")) or None,
                        )
                    )
                elif event_type == "Q" and self._on_quote is not None:
                    bid = _to_float(getattr(message, "bid_price", None))
                    ask = _to_float(getattr(message, "ask_price", None))
                    if bid is None or ask is None:
                        continue
                    self._on_quote(
                        QuoteTickRecord(
                            symbol=symbol,
                            bid_price=bid,
                            ask_price=ask,
                            bid_size=_to_int(getattr(message, "bid_size", None)),
                            ask_size=_to_int(getattr(message, "ask_size", None)),
                        )
                    )
                elif event_type == "A" and self._on_agg is not None:
                    open_price = _to_float(getattr(message, "open", None))
                    high_price = _to_float(getattr(message, "high", None))
                    low_price = _to_float(getattr(message, "low", None))
                    close_price = _to_float(getattr(message, "close", None))
                    start_timestamp = _to_int(getattr(message, "start_timestamp", None))
                    if (
                        open_price is None
                        or high_price is None
                        or low_price is None
                        or close_price is None
                        or start_timestamp is None
                    ):
                        continue
                    timestamp = (
                        start_timestamp / 1000 if start_timestamp > 1_000_000_000_000 else float(start_timestamp)
                    )
                    self._on_agg(
                        LiveBarRecord(
                            symbol=symbol,
                            interval_secs=1,
                            open=open_price,
                            high=high_price,
                            low=low_price,
                            close=close_price,
                            volume=_to_int(getattr(message, "volume", None)) or 0,
                            timestamp=timestamp,
                            trade_count=1,
                        )
                    )
            except Exception:
                logger.exception("Failed to normalize Massive stream message")
