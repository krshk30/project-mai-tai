from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass

import websockets

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.market_data.models import LiveBarRecord, QuoteTickRecord, TradeTickRecord
from project_mai_tai.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchwabStreamerCredentials:
    socket_url: str
    customer_id: str
    correl_id: str
    channel: str
    function_id: str


@dataclass(frozen=True)
class SchwabStreamerProbeResult:
    ok: bool
    connected: bool
    login_succeeded: bool
    credentials: dict[str, str]
    symbols: list[str]
    trade_count: int
    quote_count: int
    sampled_trades: list[dict[str, object]]
    sampled_quotes: list[dict[str, object]]
    raw_messages_seen: int
    duration_seconds: float
    error: str | None = None


class SchwabStreamerClient:
    LEVELONE_EQUITIES_FIELDS = "0,1,2,3,4,5,8,9,35"
    CHART_EQUITY_FIELDS = "0,1,2,3,4,5,6,7,8"
    TIMESALE_EQUITY_FIELDS = "0,1,2,3,4"

    def __init__(
        self,
        settings: Settings,
        *,
        auth_adapter: SchwabBrokerAdapter | None = None,
        reconnect_delay_seconds: float = 5.0,
        login_timeout_seconds: float = 10.0,
    ) -> None:
        self.settings = settings
        self.auth_adapter = auth_adapter or SchwabBrokerAdapter(settings)
        self.reconnect_delay_seconds = max(1.0, float(reconnect_delay_seconds))
        self.login_timeout_seconds = max(1.0, float(login_timeout_seconds))

        self._on_trade: Callable[[TradeTickRecord], None] | None = None
        self._on_quote: Callable[[QuoteTickRecord], None] | None = None
        self._on_bar: Callable[[LiveBarRecord], None] | None = None
        self._desired_symbols: set[str] = set()
        self._desired_chart_symbols: set[str] = set()
        self._desired_timesale_symbols: set[str] = set()
        self._subscribed_symbols: set[str] = set()
        self._subscribed_chart_symbols: set[str] = set()
        self._subscribed_timesale_symbols: set[str] = set()
        self._timesale_service_available = True
        self._request_id = 1
        self._credentials: SchwabStreamerCredentials | None = None
        self._ws: object | None = None
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._connection_failures = 0
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connection_failures(self) -> int:
        return self._connection_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def start(
        self,
        *,
        on_trade: Callable[[TradeTickRecord], None],
        on_quote: Callable[[QuoteTickRecord], None],
        on_bar: Callable[[LiveBarRecord], None] | None = None,
    ) -> None:
        self._on_trade = on_trade
        self._on_quote = on_quote
        self._on_bar = on_bar
        self._stop_event.clear()
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        self._ws = None
        self._connected = False
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except (TimeoutError, Exception):
                logger.debug("error or timeout closing Schwab streamer websocket", exc_info=True)
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(self._task, return_exceptions=True)),
                    timeout=3.0,
                )
            except TimeoutError:
                self._task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(self._task, return_exceptions=True),
                        timeout=1.0,
                    )
                except (TimeoutError, Exception):
                    logger.debug("Schwab streamer task did not exit cleanly", exc_info=True)
            self._task = None
        self._credentials = None
        self._subscribed_symbols.clear()
        self._subscribed_chart_symbols.clear()
        self._subscribed_timesale_symbols.clear()
        self._timesale_service_available = True
        self._last_error = None

    async def sync_subscriptions(
        self,
        symbols: Sequence[str],
        *,
        chart_symbols: Sequence[str] | None = None,
        timesale_symbols: Sequence[str] | None = None,
    ) -> None:
        self._desired_symbols = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        self._desired_chart_symbols = (
            {str(symbol).upper() for symbol in chart_symbols if str(symbol).strip()}
            if chart_symbols is not None
            else set()
        )
        self._desired_timesale_symbols = (
            {str(symbol).upper() for symbol in timesale_symbols if str(symbol).strip()}
            if timesale_symbols is not None
            else set()
        )
        try:
            await self._apply_subscription_delta()
        except websockets.exceptions.ConnectionClosed:
            self._handle_subscription_sync_connection_closed()

    async def force_resubscribe(self) -> None:
        try:
            await self._apply_subscription_delta(force_resubscribe=True)
        except websockets.exceptions.ConnectionClosed:
            self._handle_subscription_sync_connection_closed()

    async def probe(
        self,
        *,
        symbols: Sequence[str],
        duration_seconds: float = 10.0,
        sample_limit: int = 5,
    ) -> SchwabStreamerProbeResult:
        normalized_symbols = [str(symbol).upper() for symbol in symbols if str(symbol).strip()]
        if not normalized_symbols:
            raise ValueError("probe requires at least one symbol")

        credentials = await self._fetch_streamer_credentials()
        sampled_quotes: list[dict[str, object]] = []
        sampled_trades: list[dict[str, object]] = []
        quote_count = 0
        trade_count = 0
        raw_messages_seen = 0
        login_succeeded = False

        started_at = asyncio.get_running_loop().time()
        websocket = None
        try:
            websocket = await websockets.connect(
                self._normalize_socket_url(credentials.socket_url),
                ping_interval=20,
                ping_timeout=20,
                open_timeout=30,
            )
            await self._login(websocket, credentials)
            login_succeeded = True
            await self._send_subscription_command(
                websocket,
                credentials,
                service="LEVELONE_EQUITIES",
                command="ADD",
                symbols=normalized_symbols,
            )

            deadline = asyncio.get_running_loop().time() + max(1.0, float(duration_seconds))
            while asyncio.get_running_loop().time() < deadline:
                timeout = max(0.1, deadline - asyncio.get_running_loop().time())
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                except TimeoutError:
                    continue
                raw_messages_seen += 1
                payload = self._decode_message(raw_message)
                quotes, trades, _bars = self._extract_records(payload)
                quote_count += len(quotes)
                trade_count += len(trades)
                for quote in quotes:
                    if len(sampled_quotes) < sample_limit:
                        sampled_quotes.append(asdict(quote))
                for trade in trades:
                    if len(sampled_trades) < sample_limit:
                        sampled_trades.append(asdict(trade))

            return SchwabStreamerProbeResult(
                ok=login_succeeded and raw_messages_seen > 0,
                connected=True,
                login_succeeded=login_succeeded,
                credentials={
                    "socket_url": credentials.socket_url,
                    "customer_id": credentials.customer_id,
                    "correl_id": credentials.correl_id,
                    "channel": credentials.channel,
                    "function_id": credentials.function_id,
                },
                symbols=normalized_symbols,
                trade_count=trade_count,
                quote_count=quote_count,
                sampled_trades=sampled_trades,
                sampled_quotes=sampled_quotes,
                raw_messages_seen=raw_messages_seen,
                duration_seconds=asyncio.get_running_loop().time() - started_at,
            )
        except Exception as exc:
            return SchwabStreamerProbeResult(
                ok=False,
                connected=websocket is not None,
                login_succeeded=login_succeeded,
                credentials={
                    "socket_url": credentials.socket_url,
                    "customer_id": credentials.customer_id,
                    "correl_id": credentials.correl_id,
                    "channel": credentials.channel,
                    "function_id": credentials.function_id,
                },
                symbols=normalized_symbols,
                trade_count=trade_count,
                quote_count=quote_count,
                sampled_trades=sampled_trades,
                sampled_quotes=sampled_quotes,
                raw_messages_seen=raw_messages_seen,
                duration_seconds=asyncio.get_running_loop().time() - started_at,
                error=str(exc),
            )
        finally:
            if websocket is not None:
                try:
                    await self._send_subscription_command(
                        websocket,
                        credentials,
                        service="LEVELONE_EQUITIES",
                        command="UNSUBS",
                        symbols=normalized_symbols,
                    )
                except Exception:
                    logger.debug("error unsubscribing during Schwab streamer probe", exc_info=True)
                try:
                    await websocket.close()
                except Exception:
                    logger.debug("error closing Schwab streamer probe websocket", exc_info=True)

    async def _connection_loop(self) -> None:
        while not self._stop_event.is_set():
            websocket = None
            reconnect_delay = self.reconnect_delay_seconds
            try:
                self._credentials = await self._fetch_streamer_credentials()
                websocket = await websockets.connect(
                    self._normalize_socket_url(self._credentials.socket_url),
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=30,
                )
                self._ws = websocket
                await self._login(websocket, self._credentials)
                if self._connection_failures > 0:
                    logger.info(
                        "Schwab streamer connected after %s consecutive failure(s)",
                        self._connection_failures,
                    )
                else:
                    logger.info("Schwab streamer connected")
                self._connected = True
                self._connection_failures = 0
                self._last_error = None
                self._timesale_service_available = True
                await self._apply_subscription_delta(force_resubscribe=True)

                while not self._stop_event.is_set():
                    raw_message = await websocket.recv()
                    await self._handle_message(raw_message)
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosedOK:
                self._connected = False
                self._last_error = ""
                reconnect_delay = 0.5
                if not self._stop_event.is_set():
                    logger.info("Schwab streamer socket closed cleanly; reconnecting")
            except Exception as exc:
                self._connected = False
                self._connection_failures += 1
                self._last_error = traceback.format_exc(limit=1).strip().splitlines()[-1]
                if not self._stop_event.is_set():
                    if self._connection_failures <= 5:
                        logger.warning(
                            "Schwab streamer connection loop failed (attempt %s)",
                            self._connection_failures,
                            exc_info=True,
                        )
                    else:
                        logger.exception(
                            "Schwab streamer connection loop failed (attempt %s)",
                            self._connection_failures,
                        )
            finally:
                self._ws = None
                self._connected = False
                self._subscribed_symbols.clear()
                self._subscribed_chart_symbols.clear()
                self._subscribed_timesale_symbols.clear()
                if websocket is not None:
                    try:
                        await websocket.close()
                    except Exception:
                        logger.debug("error closing Schwab streamer websocket", exc_info=True)

            if not self._stop_event.is_set():
                await asyncio.sleep(reconnect_delay)

    async def _fetch_streamer_credentials(self) -> SchwabStreamerCredentials:
        status_code, _headers, payload = await self.auth_adapter._authorized_request_json(
            "GET",
            "/trader/v1/userPreference",
        )
        if status_code >= 400:
            raise RuntimeError(f"failed fetching Schwab userPreference: {payload}")

        document = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(document, dict):
            raise RuntimeError("Schwab userPreference returned invalid payload")

        streamer_info = document.get("streamerInfo")
        if isinstance(streamer_info, list):
            streamer_info = streamer_info[0] if streamer_info else None
        if not isinstance(streamer_info, dict):
            raise RuntimeError("Schwab userPreference missing streamerInfo")

        socket_url = str(streamer_info.get("streamerSocketUrl", "")).strip()
        customer_id = str(streamer_info.get("schwabClientCustomerId", "")).strip()
        correl_id = str(streamer_info.get("schwabClientCorrelId", "")).strip()
        channel = str(streamer_info.get("schwabClientChannel", "")).strip()
        function_id = str(streamer_info.get("schwabClientFunctionId", "")).strip()
        if not all((socket_url, customer_id, correl_id, channel, function_id)):
            raise RuntimeError("Schwab userPreference missing streamer credentials")

        return SchwabStreamerCredentials(
            socket_url=socket_url,
            customer_id=customer_id,
            correl_id=correl_id,
            channel=channel,
            function_id=function_id,
        )

    async def _login(self, websocket: object, credentials: SchwabStreamerCredentials) -> None:
        access_token = await self.auth_adapter._get_access_token()
        request = self._build_login_request(credentials=credentials, access_token=access_token)
        await websocket.send(json.dumps(request))
        deadline = asyncio.get_running_loop().time() + self.login_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            raw_message = await asyncio.wait_for(
                websocket.recv(),
                timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
            )
            payload = self._decode_message(raw_message)
            for response in payload.get("response", []):
                if response.get("service") != "ADMIN" or response.get("command") != "LOGIN":
                    continue
                content = response.get("content", {})
                code = str(content.get("code", "")).strip()
                if code == "0":
                    return
                message = str(content.get("msg") or content.get("message") or "unknown login failure")
                raise RuntimeError(f"Schwab streamer login failed: {message}")
        raise RuntimeError("Schwab streamer login timed out")

    async def _apply_subscription_delta(self, *, force_resubscribe: bool = False) -> None:
        websocket = self._ws
        credentials = self._credentials
        if websocket is None or credentials is None:
            return

        desired = set(self._desired_symbols)
        desired_chart = set(self._desired_chart_symbols)
        desired_timesale = set(self._desired_timesale_symbols) if self._timesale_service_available else set()
        if force_resubscribe:
            to_remove = set(self._subscribed_symbols)
            to_add = desired
            chart_to_remove = set(self._subscribed_chart_symbols)
            chart_to_add = desired_chart
            timesale_to_remove = set(self._subscribed_timesale_symbols)
            timesale_to_add = desired_timesale
        else:
            to_remove = self._subscribed_symbols - desired
            to_add = desired - self._subscribed_symbols
            chart_to_remove = self._subscribed_chart_symbols - desired_chart
            chart_to_add = desired_chart - self._subscribed_chart_symbols
            timesale_to_remove = self._subscribed_timesale_symbols - desired_timesale
            timesale_to_add = desired_timesale - self._subscribed_timesale_symbols

        if to_remove:
            await self._send_subscription_command(
                websocket,
                credentials,
                service="LEVELONE_EQUITIES",
                command="UNSUBS",
                symbols=sorted(to_remove),
            )
        if to_add:
            await self._send_subscription_command(
                websocket,
                credentials,
                service="LEVELONE_EQUITIES",
                command="ADD",
                symbols=sorted(to_add),
            )
        if chart_to_remove:
            await self._send_subscription_command(
                websocket,
                credentials,
                service="CHART_EQUITY",
                command="UNSUBS",
                symbols=sorted(chart_to_remove),
            )
        if chart_to_add:
            chart_command = "ADD" if self._subscribed_chart_symbols else "SUBS"
            await self._send_subscription_command(
                websocket,
                credentials,
                service="CHART_EQUITY",
                command=chart_command,
                symbols=sorted(chart_to_add),
            )
        if timesale_to_remove:
            await self._send_subscription_command(
                websocket,
                credentials,
                service="TIMESALE_EQUITY",
                command="UNSUBS",
                symbols=sorted(timesale_to_remove),
            )
        if timesale_to_add:
            timesale_command = "ADD" if self._subscribed_timesale_symbols else "SUBS"
            await self._send_subscription_command(
                websocket,
                credentials,
                service="TIMESALE_EQUITY",
                command=timesale_command,
                symbols=sorted(timesale_to_add),
            )
        self._subscribed_symbols = desired
        self._subscribed_chart_symbols = desired_chart
        self._subscribed_timesale_symbols = desired_timesale

    async def _send_subscription_command(
        self,
        websocket: object,
        credentials: SchwabStreamerCredentials,
        *,
        service: str,
        command: str,
        symbols: Sequence[str],
    ) -> None:
        if not symbols:
            return

        request = self._build_subscription_request(
            credentials=credentials,
            service=service,
            command=command,
            symbols=symbols,
        )
        async with self._send_lock:
            await websocket.send(json.dumps(request))

    def _handle_subscription_sync_connection_closed(self) -> None:
        self._connected = False
        self._last_error = ""
        if not self._stop_event.is_set():
            logger.info("Schwab streamer subscription sync saw closed socket; waiting for reconnect")

    def _disable_timesale_service(self, *, reason: str) -> None:
        if not self._timesale_service_available and not self._subscribed_timesale_symbols:
            return
        fallback_symbols = sorted(self._subscribed_timesale_symbols or self._desired_timesale_symbols)
        self._timesale_service_available = False
        self._subscribed_timesale_symbols.clear()
        if fallback_symbols:
            logger.warning(
                "Schwab TIMESALE_EQUITY unavailable; falling back to LEVELONE_EQUITIES trades | "
                "symbols=%s reason=%s",
                ",".join(fallback_symbols),
                reason,
            )

    async def _handle_message(self, raw_message: str | bytes) -> None:
        payload = self._decode_message(raw_message)
        for response in payload.get("response", []):
            if not isinstance(response, dict):
                continue
            service = str(response.get("service", "")).strip()
            command = str(response.get("command", "")).strip()
            content = response.get("content", {})
            if isinstance(content, list):
                content = content[0] if content else {}
            if not isinstance(content, dict):
                continue
            code = str(content.get("code", "")).strip()
            if code and code != "0":
                message = str(content.get("msg") or content.get("message") or "unknown response error")
                if service.upper() == "TIMESALE_EQUITY":
                    self._disable_timesale_service(reason=message)
                logger.warning(
                    "Schwab streamer response error | service=%s command=%s code=%s message=%s",
                    service,
                    command,
                    code,
                    message,
                )
        quotes, trades, bars = self._extract_records(payload, timesale_symbols=self._subscribed_timesale_symbols)
        if self._on_quote is not None:
            for quote in quotes:
                self._on_quote(quote)
        if self._on_trade is not None:
            for trade in trades:
                self._on_trade(trade)
        if self._on_bar is not None:
            for bar in bars:
                self._on_bar(bar)

    @classmethod
    def _extract_records(
        cls,
        payload: dict[str, object],
        *,
        timesale_symbols: Collection[str] | None = None,
    ) -> tuple[list[QuoteTickRecord], list[TradeTickRecord], list[LiveBarRecord]]:
        quotes: list[QuoteTickRecord] = []
        trades: list[TradeTickRecord] = []
        bars: list[LiveBarRecord] = []
        normalized_timesale_symbols = {
            str(symbol).upper() for symbol in (timesale_symbols or ()) if str(symbol).strip()
        }
        for item in payload.get("data", []):
            service = str(item.get("service", "")).strip().upper()
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                if service == "LEVELONE_EQUITIES":
                    quote = cls._extract_quote_record(content)
                    if quote is not None:
                        quotes.append(quote)

                    symbol = str(content.get("key") or content.get("0") or "").upper()
                    if symbol in normalized_timesale_symbols:
                        continue
                    trade = cls._extract_trade_record(content)
                    if trade is not None:
                        trades.append(trade)
                    continue
                if service == "CHART_EQUITY":
                    bar = cls._extract_chart_bar_record(content)
                    if bar is not None:
                        bars.append(bar)
                    continue
                if service == "TIMESALE_EQUITY":
                    trade = cls._extract_timesale_trade_record(content)
                    if trade is not None:
                        trades.append(trade)
        return quotes, trades, bars

    def _build_login_request(
        self,
        *,
        credentials: SchwabStreamerCredentials,
        access_token: str,
    ) -> dict[str, object]:
        return {
            "requests": [
                {
                    "service": "ADMIN",
                    "requestid": str(self._next_request_id()),
                    "command": "LOGIN",
                    "SchwabClientCustomerId": credentials.customer_id,
                    "SchwabClientCorrelId": credentials.correl_id,
                    "parameters": {
                        "Authorization": access_token,
                        "SchwabClientChannel": credentials.channel,
                        "SchwabClientFunctionId": credentials.function_id,
                    },
                }
            ]
        }

    def _build_subscription_request(
        self,
        *,
        credentials: SchwabStreamerCredentials,
        service: str,
        command: str,
        symbols: Sequence[str],
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "requests": [
                {
                    "service": service,
                    "requestid": str(self._next_request_id()),
                    "command": command,
                    "SchwabClientCustomerId": credentials.customer_id,
                    "SchwabClientCorrelId": credentials.correl_id,
                    "parameters": {
                        "keys": ",".join(str(symbol).upper() for symbol in symbols if str(symbol).strip()),
                    },
                }
            ]
        }
        if command != "UNSUBS":
            if service == "LEVELONE_EQUITIES":
                fields = self.LEVELONE_EQUITIES_FIELDS
            elif service == "CHART_EQUITY":
                fields = self.CHART_EQUITY_FIELDS
            else:
                fields = self.TIMESALE_EQUITY_FIELDS
            request["requests"][0]["parameters"]["fields"] = fields
        return request

    @classmethod
    def _extract_quote_record(cls, content: dict[str, object]) -> QuoteTickRecord | None:
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None

        bid_price = cls._float_or_none(content.get("1"))
        ask_price = cls._float_or_none(content.get("2"))
        if bid_price is None or ask_price is None or bid_price <= 0 or ask_price <= 0:
            return None

        return QuoteTickRecord(
            symbol=symbol,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=cls._int_or_none(content.get("4")),
            ask_size=cls._int_or_none(content.get("5")),
        )

    @classmethod
    def _extract_trade_record(cls, content: dict[str, object]) -> TradeTickRecord | None:
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None

        last_price = cls._float_or_none(content.get("3"))
        if last_price is None or last_price <= 0:
            return None

        cumulative_volume = cls._int_or_none(content.get("8"))
        trade_time_ms = cls._int_or_none(content.get("35"))
        # Live Schwab capture showed field 9 carries the last-share size.
        last_size = cls._int_or_none(content.get("9"))

        return TradeTickRecord(
            symbol=symbol,
            price=last_price,
            size=max(1, last_size or 0),
            timestamp_ns=(trade_time_ms * 1_000_000) if trade_time_ms is not None else None,
            cumulative_volume=cumulative_volume,
        )

    @classmethod
    def _extract_timesale_trade_record(cls, content: dict[str, object]) -> TradeTickRecord | None:
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None

        last_price = cls._float_or_none(content.get("2"))
        if last_price is None or last_price <= 0:
            return None

        trade_time_ms = cls._int_or_none(content.get("1"))
        last_size = cls._int_or_none(content.get("3"))

        return TradeTickRecord(
            symbol=symbol,
            price=last_price,
            size=max(1, last_size or 0),
            timestamp_ns=(trade_time_ms * 1_000_000) if trade_time_ms is not None else None,
        )

    @classmethod
    def _extract_chart_bar_record(cls, content: dict[str, object]) -> LiveBarRecord | None:
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None

        open_price = cls._float_or_none(content.get("2"))
        high_price = cls._float_or_none(content.get("3"))
        low_price = cls._float_or_none(content.get("4"))
        close_price = cls._float_or_none(content.get("5"))
        volume = cls._int_or_none(content.get("6"))
        chart_time_ms = cls._int_or_none(content.get("7"))
        if (
            open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
            or chart_time_ms is None
            or close_price <= 0
        ):
            return None

        return LiveBarRecord(
            symbol=symbol,
            interval_secs=60,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=max(0, volume or 0),
            timestamp=chart_time_ms / 1000.0,
            trade_count=1,
        )

    @staticmethod
    def _decode_message(raw_message: str | bytes | dict[str, object]) -> dict[str, object]:
        if isinstance(raw_message, dict):
            return raw_message
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        payload = json.loads(raw_message)
        if not isinstance(payload, dict):
            return {}
        return payload

    @staticmethod
    def _normalize_socket_url(socket_url: str) -> str:
        if socket_url.startswith("ws://") or socket_url.startswith("wss://"):
            return socket_url
        return f"wss://{socket_url}"

    def _next_request_id(self) -> int:
        request_id = self._request_id
        self._request_id += 1
        return request_id

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
