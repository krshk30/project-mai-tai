"""Dedicated Schwab WebSocket streamer for the isolated `schwab_1m_v2` bot.

Shares NO code with `market_data/schwab_streamer.py`. Built to escape the
regression chain that's been hitting the shared streamer file. Subscribes
to CHART_EQUITY only — the v2 strategy is bar-close based; LEVELONE quotes
and TIMESALE trades are deliberately out of scope.

OAuth: reads the same access token as `schwab_v2_rest_client.py` (from
`settings.schwab_token_store_path`). Schwab's streamer is generally
limited to one concurrent WS session per OAuth user, so opening this
session may collide with the existing `schwab_streamer.py` session held by
the strategy-engine process. The streamer is gated behind a separate
enable flag (default off) so the PR ships dormant; flip the flag only
during an evening test window with eyes on the existing schwab_1m log.

Why CHART_EQUITY is enough: Schwab pushes each closed 1-minute bar as a
complete OHLCV snapshot at minute close (~T+0 to T+1s). The bar is final
the moment we receive it — no in-flight bucket building, no 60s finality
wait, no round-robin queue. That drops the v2 persist-lag floor from
~85s p50 to <2s p50.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

try:  # `websockets` is already a dependency via the existing streamer.
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover — defensive only
    websockets = None  # type: ignore[assignment]
    ConnectionClosed = Exception  # type: ignore[misc,assignment]

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.settings import Settings

logger = logging.getLogger(__name__)

ChartBarCallback = Callable[[str, ChartBar], Awaitable[None]]
DisconnectCallback = Callable[[], Awaitable[None]]


@dataclass
class SchwabTick:
    """A captured LEVELONE_EQUITIES tick (trade or quote). Capture-only — never
    consumed by the strategy. A single LEVELONE content record can yield a trade
    tick, a quote tick, or both.
    """

    kind: str            # "trade" | "quote"
    service: str
    symbol: str
    event_ts_ms: int
    raw: dict[str, object]
    raw_hash: str
    # trade fields
    price: float | None = None
    size: int | None = None
    cumulative_volume: int | None = None
    # quote fields
    bid_price: float | None = None
    ask_price: float | None = None
    last_price: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    last_size: int | None = None


TickCallback = Callable[[SchwabTick], Awaitable[None]]


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class _StreamerCreds:
    socket_url: str
    customer_id: str
    correl_id: str
    channel: str
    function_id: str


class SchwabV2Streamer:
    """WebSocket-based 1m bar feed for the v2 bot.

    Idle (no-op, awaiting stop) when:
    - `websockets` package is unavailable (import failed), OR
    - the Schwab token store is empty / unreadable, OR
    - the streamer enable flag is off (default).

    This lets the service ship + boot before the operator wires
    credentials or flips the flag.
    """

    USER_PREF_PATH = "/trader/v1/userPreference"
    CHART_EQUITY_SERVICE = "CHART_EQUITY"
    # Field codes per Schwab CHART_EQUITY schema:
    #   0=key (symbol), 1=sequence, 2=open, 3=high, 4=low, 5=close,
    #   6=volume, 7=chart_time_ms, 8=chart_day
    CHART_EQUITY_FIELDS = "0,1,2,3,4,5,6,7,8"
    # LEVELONE_EQUITIES (tick capture only — gated behind tick-capture flag).
    # Field codes (verified against the existing schwab_streamer.py + live capture):
    #   0=key, 1=bid, 2=ask, 3=last, 4=bid_size, 5=ask_size,
    #   8=cumulative_volume, 9=last_size, 35=trade_time_ms
    LEVELONE_EQUITIES_SERVICE = "LEVELONE_EQUITIES"
    LEVELONE_EQUITIES_FIELDS = "0,1,2,3,4,5,8,9,35"
    # TIMESALE_EQUITY (true time-&-sales — trade-by-trade; tick capture only, gated
    # behind a SEPARATE flag, ADDITIVE to LEVELONE — does not remove or alter it).
    # Field codes (Schwab TIMESALE_EQUITY schema):
    #   0=key (symbol), 1=trade_time_ms, 2=last_price, 3=last_size, 4=last_sequence
    TIMESALE_EQUITY_SERVICE = "TIMESALE_EQUITY"
    TIMESALE_EQUITY_FIELDS = "0,1,2,3,4"

    def __init__(
        self,
        settings: Settings,
        *,
        on_chart_bar: ChartBarCallback,
        on_disconnect: DisconnectCallback | None = None,
        on_tick: TickCallback | None = None,
    ) -> None:
        self.settings = settings
        self._on_chart_bar = on_chart_bar
        self._on_disconnect = on_disconnect
        self._on_tick = on_tick
        self._desired_symbols: set[str] = set()
        self._requested_symbols: set[str] = set()
        self._stop_event = asyncio.Event()
        self._sync_event = asyncio.Event()
        self._last_bar_ts_ms: dict[str, int] = {}
        self._ws: object | None = None
        self._creds: _StreamerCreds | None = None
        self._request_id_counter = itertools.count(1)
        self._connected = False
        self._connect_failures = 0
        self._last_bar_received_monotonic: float = 0.0

    @property
    def configured(self) -> bool:
        if websockets is None:
            return False
        if not bool(getattr(self.settings, "strategy_schwab_1m_v2_streamer_enabled", False)):
            return False
        return bool((self.settings.schwab_token_store_path or "").strip())

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def _tick_capture(self) -> bool:
        """LEVELONE tick capture is active only when a writer callback was wired
        AND the tick-capture flag is on. Default off -> no LEVELONE SUBS is sent,
        the LEVELONE branch is unreachable, CHART_EQUITY behavior is identical."""
        return self._on_tick is not None and bool(
            getattr(self.settings, "strategy_schwab_1m_v2_tick_capture_enabled", False)
        )

    @property
    def _timesale_capture(self) -> bool:
        """TIMESALE (true trade-by-trade) capture is active only when a writer callback
        was wired AND the timesale-capture flag is on. SEPARATE from LEVELONE tick
        capture (additive). Default off -> no TIMESALE SUBS is sent, the TIMESALE branch
        is unreachable, and CHART_EQUITY + LEVELONE behavior is identical."""
        return self._on_tick is not None and bool(
            getattr(self.settings, "strategy_schwab_1m_v2_timesale_capture_enabled", False)
        )

    def set_desired_symbols(self, symbols: set[str]) -> None:
        normalized = {s.strip().upper() for s in symbols if s.strip()}
        if normalized == self._desired_symbols:
            return
        self._desired_symbols = normalized
        self._sync_event.set()

    async def run(self) -> None:
        if not self.configured:
            if websockets is None:
                logger.warning(
                    "schwab_v2_streamer idle: 'websockets' package unavailable."
                )
            else:
                logger.info(
                    "schwab_v2_streamer idle: enabled=%s token_path=%r. "
                    "Set MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true "
                    "and ensure schwab_token_store_path is set to activate.",
                    getattr(self.settings, "strategy_schwab_1m_v2_streamer_enabled", False),
                    self.settings.schwab_token_store_path,
                )
            await self._stop_event.wait()
            return

        base_delay = max(
            0.5,
            float(self.settings.strategy_schwab_1m_v2_streamer_reconnect_base_secs),
        )
        max_delay = max(
            base_delay,
            float(self.settings.strategy_schwab_1m_v2_streamer_reconnect_max_secs),
        )

        while not self._stop_event.is_set():
            ws = None
            try:
                self._creds = await asyncio.to_thread(self._fetch_streamer_creds)
                ws = await websockets.connect(
                    self._creds.socket_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=30,
                )
                self._ws = ws
                await self._login(ws, self._creds)
                self._connected = True
                self._connect_failures = 0
                logger.info(
                    "[V2-WS-LOGIN-OK] schwab_v2 streamer connected "
                    "(symbols_desired=%d)",
                    len(self._desired_symbols),
                )
                # Force a fresh SUBS on every (re)connect — Schwab's streamer
                # has no server-side subscription memory across sessions.
                self._requested_symbols = set()
                self._sync_event.set()
                await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connect_failures += 1
                logger.warning(
                    "[V2-WS-DISCONNECT] schwab_v2 streamer failure #%d: %s",
                    self._connect_failures,
                    exc,
                )
            finally:
                was_connected = self._connected
                self._connected = False
                self._ws = None
                self._requested_symbols.clear()
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if was_connected and self._on_disconnect is not None:
                    try:
                        await self._on_disconnect()
                    except Exception:
                        logger.exception(
                            "schwab_v2_streamer on_disconnect callback raised"
                        )
            if self._stop_event.is_set():
                break
            backoff = min(
                max_delay,
                base_delay * (2 ** min(self._connect_failures, 6)),
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()
        self._sync_event.set()

    def last_bar_ts_ms(self, symbol: str) -> int:
        return self._last_bar_ts_ms.get(symbol, 0)

    # --------------------------------------------------------------- receive

    async def _receive_loop(self, ws: object) -> None:
        # Tight recv timeout so we can apply pending sub-deltas without
        # waiting for an inbound message.
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)  # type: ignore[attr-defined]
            except asyncio.TimeoutError:
                if self._sync_event.is_set():
                    self._sync_event.clear()
                    await self._apply_subscription_delta(ws)
                continue
            except ConnectionClosed:
                raise
            await self._handle_message(raw)
            if self._sync_event.is_set():
                self._sync_event.clear()
                await self._apply_subscription_delta(ws)

    async def _handle_message(self, raw: str | bytes) -> None:
        payload = self._decode(raw)
        if not payload:
            return
        # Admin / subscription responses — log non-zero codes but don't fail.
        for resp in payload.get("response", []):
            if not isinstance(resp, dict):
                continue
            content = resp.get("content")
            if isinstance(content, list):
                content = content[0] if content else {}
            if not isinstance(content, dict):
                continue
            code = str(content.get("code", "")).strip()
            if code and code != "0":
                logger.warning(
                    "[V2-WS-RESP-ERR] service=%s command=%s code=%s msg=%s",
                    str(resp.get("service", "")).upper(),
                    str(resp.get("command", "")).upper(),
                    code,
                    str(content.get("msg") or content.get("message") or "?"),
                )
        # Data records. CHART_EQUITY drives the bar feed (unchanged). LEVELONE
        # (only when tick capture is on) is TEED to on_tick for durable capture;
        # it never touches the bar feed, _last_bar_ts_ms, or strategy state.
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            service = str(item.get("service", "")).upper()
            if service == self.CHART_EQUITY_SERVICE:
                for content in item.get("content", []):
                    if not isinstance(content, dict):
                        continue
                    bar = self._extract_chart_bar(content)
                    if bar is None:
                        continue
                    # Dedupe: skip same-or-older buckets. CHART_EQUITY's
                    # contract is that each emit is a FINAL snapshot for the
                    # closed minute, so re-emits of the same bucket are
                    # redundant — letting them through would flip
                    # `state.bars[-1]` under the strategy's update-in-place
                    # path without re-running cross detection, which is
                    # wasted work and a source of cross-feed seam noise.
                    # Tightened from `<` to `<=` per W3 in the code review;
                    # see Day 2 entry in `docs/session-handoff-schwab-1m-v2.md`.
                    prev = self._last_bar_ts_ms.get(bar.symbol, 0)
                    if bar.timestamp_ms <= prev:
                        continue
                    self._last_bar_ts_ms[bar.symbol] = bar.timestamp_ms
                    self._last_bar_received_monotonic = asyncio.get_running_loop().time()
                    try:
                        await self._on_chart_bar(bar.symbol, bar)
                    except Exception:
                        logger.exception(
                            "schwab_v2_streamer on_chart_bar raised for %s",
                            bar.symbol,
                        )
            elif (
                service == self.LEVELONE_EQUITIES_SERVICE
                and self._tick_capture
                and self._on_tick is not None
            ):
                item_ts_ms = _int_or_none(item.get("timestamp"))
                for content in item.get("content", []):
                    if not isinstance(content, dict):
                        continue
                    for tick in self._extract_level_one_ticks(content, item_ts_ms):
                        try:
                            await self._on_tick(tick)
                        except Exception:
                            logger.exception(
                                "schwab_v2_streamer on_tick raised for %s",
                                tick.symbol,
                            )
            elif (
                service == self.TIMESALE_EQUITY_SERVICE
                and self._timesale_capture
                and self._on_tick is not None
            ):
                item_ts_ms = _int_or_none(item.get("timestamp"))
                for content in item.get("content", []):
                    if not isinstance(content, dict):
                        continue
                    tick = self._extract_timesale_tick(content, item_ts_ms)
                    if tick is None:
                        continue
                    try:
                        await self._on_tick(tick)
                    except Exception:
                        logger.exception(
                            "schwab_v2_streamer on_tick (timesale) raised for %s",
                            tick.symbol,
                        )

    # -------------------------------------------------------- subscription

    async def _apply_subscription_delta(self, ws: object) -> None:
        desired = set(self._desired_symbols)
        to_remove = self._requested_symbols - desired
        to_add = desired - self._requested_symbols
        if to_remove:
            await self._send_subscription(ws, command="UNSUBS", symbols=sorted(to_remove))
        if to_add:
            command = "ADD" if self._requested_symbols else "SUBS"
            await self._send_subscription(ws, command=command, symbols=sorted(to_add))
        self._requested_symbols = desired

    def _build_service_request(
        self, *, service: str, fields: str, command: str, symbols: list[str]
    ) -> dict[str, object]:
        assert self._creds is not None
        req: dict[str, object] = {
            "service": service,
            "requestid": str(next(self._request_id_counter)),
            "command": command,
            "SchwabClientCustomerId": self._creds.customer_id,
            "SchwabClientCorrelId": self._creds.correl_id,
            "parameters": {"keys": ",".join(symbols)},
        }
        if command != "UNSUBS":
            req["parameters"]["fields"] = fields  # type: ignore[index]
        return req

    async def _send_subscription(
        self, ws: object, *, command: str, symbols: list[str]
    ) -> None:
        if not symbols or self._creds is None:
            return
        # CHART_EQUITY always; LEVELONE_EQUITIES too (same session, same symbols)
        # only when tick capture is active. Both ride one frame; distinct
        # requestids keep them independent. CHART_EQUITY framing is unchanged
        # from the pre-tick-capture path.
        requests = [
            self._build_service_request(
                service=self.CHART_EQUITY_SERVICE, fields=self.CHART_EQUITY_FIELDS,
                command=command, symbols=symbols,
            )
        ]
        if self._tick_capture:
            requests.append(
                self._build_service_request(
                    service=self.LEVELONE_EQUITIES_SERVICE, fields=self.LEVELONE_EQUITIES_FIELDS,
                    command=command, symbols=symbols,
                )
            )
        if self._timesale_capture:
            requests.append(
                self._build_service_request(
                    service=self.TIMESALE_EQUITY_SERVICE, fields=self.TIMESALE_EQUITY_FIELDS,
                    command=command, symbols=symbols,
                )
            )
        await ws.send(json.dumps({"requests": requests}))  # type: ignore[attr-defined]
        logger.info(
            "[V2-WS-SUB] cmd=%s count=%d services=%s sample=%s",
            command,
            len(symbols),
            "+".join(str(r["service"]) for r in requests),
            ",".join(symbols[:5]),
        )

    # ------------------------------------------------------------- protocol

    async def _login(self, ws: object, creds: _StreamerCreds) -> None:
        token = await asyncio.to_thread(self._read_access_token)
        if not token:
            raise RuntimeError("schwab_v2_streamer login: access token unavailable")
        request = {
            "requests": [
                {
                    "service": "ADMIN",
                    "requestid": str(next(self._request_id_counter)),
                    "command": "LOGIN",
                    "SchwabClientCustomerId": creds.customer_id,
                    "SchwabClientCorrelId": creds.correl_id,
                    "parameters": {
                        "Authorization": token,
                        "SchwabClientChannel": creds.channel,
                        "SchwabClientFunctionId": creds.function_id,
                    },
                }
            ]
        }
        await ws.send(json.dumps(request))  # type: ignore[attr-defined]
        timeout = float(self.settings.schwab_request_timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = max(0.1, deadline - loop.time())
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)  # type: ignore[attr-defined]
            payload = self._decode(raw)
            if not payload:
                continue
            for resp in payload.get("response", []):
                if not isinstance(resp, dict):
                    continue
                if str(resp.get("service", "")).upper() != "ADMIN":
                    continue
                if str(resp.get("command", "")).upper() != "LOGIN":
                    continue
                content = resp.get("content")
                if isinstance(content, list):
                    content = content[0] if content else {}
                if not isinstance(content, dict):
                    continue
                code = str(content.get("code", "")).strip()
                if code == "0":
                    return
                msg = str(content.get("msg") or content.get("message") or "unknown")
                raise RuntimeError(
                    f"schwab_v2_streamer login failed: code={code} msg={msg}"
                )
        raise RuntimeError("schwab_v2_streamer login timed out")

    def _fetch_streamer_creds(self) -> _StreamerCreds:
        token = self._read_access_token()
        if not token:
            raise RuntimeError("schwab_v2_streamer: access token unavailable")
        url = f"{self.settings.schwab_base_url.rstrip('/')}{self.USER_PREF_PATH}"
        req = UrlRequest(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=self.settings.schwab_request_timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"schwab_v2_streamer userPreference HTTP {exc.code}: {detail or exc}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"schwab_v2_streamer userPreference transport error: {exc}"
            ) from exc
        document = json.loads(body)
        if isinstance(document, list):
            document = document[0] if document else {}
        if not isinstance(document, dict):
            raise RuntimeError("schwab_v2_streamer userPreference: invalid payload")
        info = document.get("streamerInfo")
        if isinstance(info, list):
            info = info[0] if info else None
        if not isinstance(info, dict):
            raise RuntimeError("schwab_v2_streamer userPreference missing streamerInfo")
        socket_url = str(info.get("streamerSocketUrl", "")).strip()
        if socket_url and not socket_url.startswith(("ws://", "wss://")):
            socket_url = f"wss://{socket_url}"
        creds = _StreamerCreds(
            socket_url=socket_url,
            customer_id=str(info.get("schwabClientCustomerId", "")).strip(),
            correl_id=str(info.get("schwabClientCorrelId", "")).strip(),
            channel=str(info.get("schwabClientChannel", "")).strip(),
            function_id=str(info.get("schwabClientFunctionId", "")).strip(),
        )
        if not all(
            (
                creds.socket_url,
                creds.customer_id,
                creds.correl_id,
                creds.channel,
                creds.function_id,
            )
        ):
            raise RuntimeError(
                "schwab_v2_streamer userPreference streamerInfo missing fields"
            )
        return creds

    def _read_access_token(self) -> str | None:
        path = (self.settings.schwab_token_store_path or "").strip()
        if not path:
            return None
        try:
            document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("schwab_v2_streamer token store unreadable: %s", exc)
            return None
        token = str(document.get("access_token", "")).strip()
        return token or None

    # ----------------------------------------------------------- bar extract

    @staticmethod
    def _extract_chart_bar(content: dict[str, object]) -> ChartBar | None:
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None
        try:
            open_p = float(content.get("2", 0.0) or 0.0)
            high_p = float(content.get("3", 0.0) or 0.0)
            low_p = float(content.get("4", 0.0) or 0.0)
            close_p = float(content.get("5", 0.0) or 0.0)
            volume = int(float(content.get("6", 0) or 0))
            ts_ms = int(float(content.get("7", 0) or 0))
        except (TypeError, ValueError):
            return None
        if close_p <= 0 or ts_ms <= 0:
            return None
        return ChartBar(
            symbol=symbol,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=max(0, volume),
            timestamp_ms=ts_ms,
        )

    @staticmethod
    def _extract_level_one_ticks(
        content: dict[str, object], item_ts_ms: int | None
    ) -> list[SchwabTick]:
        """Parse one LEVELONE_EQUITIES content record into 0–2 ticks (a trade
        and/or a quote). Fields: 0=key, 1=bid, 2=ask, 3=last, 4=bid_size,
        5=ask_size, 8=cumulative_volume, 9=last_size, 35=trade_time_ms.
        Capture-only; no dedupe here (the DB unique constraint dedups exact
        re-sends; distinct field updates are intentionally all kept)."""
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return []
        raw_hash = hashlib.sha1(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        bid = _float_or_none(content.get("1"))
        ask = _float_or_none(content.get("2"))
        last = _float_or_none(content.get("3"))
        bid_size = _int_or_none(content.get("4"))
        ask_size = _int_or_none(content.get("5"))
        cum_vol = _int_or_none(content.get("8"))
        last_size = _int_or_none(content.get("9"))
        trade_time_ms = _int_or_none(content.get("35"))
        ticks: list[SchwabTick] = []
        if last is not None and last > 0:
            event_ts = trade_time_ms or item_ts_ms
            if event_ts is not None:
                ticks.append(
                    SchwabTick(
                        kind="trade", service="LEVELONE_EQUITIES", symbol=symbol,
                        event_ts_ms=int(event_ts), raw=content, raw_hash=raw_hash,
                        price=last, size=last_size, cumulative_volume=cum_vol,
                    )
                )
        if (bid is not None and bid > 0) or (ask is not None and ask > 0):
            if item_ts_ms is not None:
                ticks.append(
                    SchwabTick(
                        kind="quote", service="LEVELONE_EQUITIES", symbol=symbol,
                        event_ts_ms=int(item_ts_ms), raw=content, raw_hash=raw_hash,
                        bid_price=bid, ask_price=ask, last_price=last,
                        bid_size=bid_size, ask_size=ask_size, last_size=last_size,
                        cumulative_volume=cum_vol,
                    )
                )
        return ticks

    @staticmethod
    def _extract_timesale_tick(
        content: dict[str, object], item_ts_ms: int | None
    ) -> "SchwabTick | None":
        """Parse one TIMESALE_EQUITY content record into a single TRADE tick (true
        time-&-sales). Fields: 0=key, 1=trade_time_ms, 2=last_price, 3=last_size,
        4=sequence. Capture-only; the DB unique constraint dedups exact re-sends."""
        symbol = str(content.get("key") or content.get("0") or "").upper()
        if not symbol:
            return None
        price = _float_or_none(content.get("2"))
        if price is None or price <= 0:
            return None
        size = _int_or_none(content.get("3"))
        event_ts = _int_or_none(content.get("1")) or item_ts_ms
        if event_ts is None:
            return None
        raw_hash = hashlib.sha1(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return SchwabTick(
            kind="trade", service="TIMESALE_EQUITY", symbol=symbol,
            event_ts_ms=int(event_ts), raw=content, raw_hash=raw_hash,
            price=price, size=size,
        )

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, object]:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}


__all__ = [
    "SchwabV2Streamer",
    "ChartBarCallback",
    "DisconnectCallback",
    "TickCallback",
    "SchwabTick",
]
