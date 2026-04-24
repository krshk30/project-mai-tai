from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

import websockets

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
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
        self._desired_symbols: set[str] = set()
        self._subscribed_symbols: set[str] = set()
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
    ) -> None:
        self._on_trade = on_trade
        self._on_quote = on_quote
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
                await ws.close()
            except Exception:
                logger.debug("error closing Schwab streamer websocket", exc_info=True)
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._credentials = None
        self._subscribed_symbols.clear()
        self._last_error = None

    async def sync_subscriptions(self, symbols: Sequence[str]) -> None:
        self._desired_symbols = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        await self._apply_subscription_delta()

    async def force_resubscribe(self) -> None:
        await self._apply_subscription_delta(force_resubscribe=True)

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
            )
            await self._login(websocket, credentials)
            login_succeeded = True
            await self._send_subscription_command(
                websocket,
                credentials,
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
                quotes, trades = self._extract_records(payload)
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
            try:
                self._credentials = await self._fetch_streamer_credentials()
                websocket = await websockets.connect(
                    self._normalize_socket_url(self._credentials.socket_url),
                    ping_interval=20,
                    ping_timeout=20,
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
                await self._apply_subscription_delta(force_resubscribe=True)

                while not self._stop_event.is_set():
                    raw_message = await websocket.recv()
                    await self._handle_message(raw_message)
            except asyncio.CancelledError:
                raise
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
                if websocket is not None:
                    try:
                        await websocket.close()
                    except Exception:
                        logger.debug("error closing Schwab streamer websocket", exc_info=True)

            if not self._stop_event.is_set():
                await asyncio.sleep(self.reconnect_delay_seconds)

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
        if force_resubscribe:
            to_remove = set(self._subscribed_symbols)
            to_add = desired
        else:
            to_remove = self._subscribed_symbols - desired
            to_add = desired - self._subscribed_symbols

        if to_remove:
            await self._send_subscription_command(
                websocket,
                credentials,
                command="UNSUBS",
                symbols=sorted(to_remove),
            )
        if to_add:
            await self._send_subscription_command(
                websocket,
                credentials,
                command="ADD",
                symbols=sorted(to_add),
            )
        self._subscribed_symbols = desired

    async def _send_subscription_command(
        self,
        websocket: object,
        credentials: SchwabStreamerCredentials,
        *,
        command: str,
        symbols: Sequence[str],
    ) -> None:
        if not symbols:
            return

        request = self._build_subscription_request(
            credentials=credentials,
            command=command,
            symbols=symbols,
        )
        async with self._send_lock:
            await websocket.send(json.dumps(request))

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
                logger.warning(
                    "Schwab streamer response error | service=%s command=%s code=%s message=%s",
                    service,
                    command,
                    code,
                    message,
                )
        quotes, trades = self._extract_records(payload)
        if self._on_quote is not None:
            for quote in quotes:
                self._on_quote(quote)
        if self._on_trade is not None:
            for trade in trades:
                self._on_trade(trade)

    @classmethod
    def _extract_records(
        cls,
        payload: dict[str, object],
    ) -> tuple[list[QuoteTickRecord], list[TradeTickRecord]]:
        quotes: list[QuoteTickRecord] = []
        trades: list[TradeTickRecord] = []
        for item in payload.get("data", []):
            if item.get("service") != "LEVELONE_EQUITIES":
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                quote = cls._extract_quote_record(content)
                if quote is not None:
                    quotes.append(quote)

                trade = cls._extract_trade_record(content)
                if trade is not None:
                    trades.append(trade)
        return quotes, trades

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
        command: str,
        symbols: Sequence[str],
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "requests": [
                {
                    "service": "LEVELONE_EQUITIES",
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
            request["requests"][0]["parameters"]["fields"] = self.LEVELONE_EQUITIES_FIELDS
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
