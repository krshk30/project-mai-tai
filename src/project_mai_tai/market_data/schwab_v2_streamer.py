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

    def __init__(
        self,
        settings: Settings,
        *,
        on_chart_bar: ChartBarCallback,
        on_disconnect: DisconnectCallback | None = None,
    ) -> None:
        self.settings = settings
        self._on_chart_bar = on_chart_bar
        self._on_disconnect = on_disconnect
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
        # --- DIAGNOSTIC (2026-06-09 streamer no-bars investigation) — env-gated,
        # NOT a fix. Pins the arriving-vs-dropped fork by logging raw frames.
        # Revert by setting strategy_schwab_1m_v2_streamer_diag_enabled=false.
        self._diag_enabled = bool(
            getattr(settings, "strategy_schwab_1m_v2_streamer_diag_enabled", False)
        )
        self._diag_frame_counts: dict[str, int] = {}
        self._diag_chart_sample_logged = False
        self._diag_last_tally_log = 0.0

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
        if self._diag_enabled:
            self._diag_observe(payload)
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
        # Data records — CHART_EQUITY only.
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("service", "")).upper() != self.CHART_EQUITY_SERVICE:
                continue
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

    def _diag_observe(self, payload: dict[str, object]) -> None:
        """DIAGNOSTIC (env-gated, NOT a fix). Pins the arriving-vs-dropped fork:
        tallies frame composition (response/data/notify), logs each data frame's
        services, and one-shot-dumps the first CHART_EQUITY data frame's RAW
        content so the real field keys can be compared to the 0/2/3/4/5/6/7 parse.
        """
        present = sorted(k for k in ("response", "data", "notify") if payload.get(k))
        key = ",".join(present) or "other"
        self._diag_frame_counts[key] = self._diag_frame_counts.get(key, 0) + 1
        data = payload.get("data") or []
        if isinstance(data, list) and data:
            services = sorted(
                {str(it.get("service", "")).upper() for it in data if isinstance(it, dict)}
            )
            logger.info("[V2-WS-DIAG] data frame: services=%s items=%d", services, len(data))
            if not self._diag_chart_sample_logged:
                for it in data:
                    if isinstance(it, dict) and str(it.get("service", "")).upper() == self.CHART_EQUITY_SERVICE:
                        logger.info(
                            "[V2-WS-DIAG-CHART-RAW] item_keys=%s content=%s",
                            sorted(str(k) for k in it.keys()),
                            json.dumps(it.get("content"))[:800],
                        )
                        self._diag_chart_sample_logged = True
                        break
        now = asyncio.get_running_loop().time()
        if now - self._diag_last_tally_log >= 30.0:
            self._diag_last_tally_log = now
            logger.info(
                "[V2-WS-DIAG-TALLY] frame_composition=%s chart_sample_logged=%s",
                self._diag_frame_counts,
                self._diag_chart_sample_logged,
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

    async def _send_subscription(
        self, ws: object, *, command: str, symbols: list[str]
    ) -> None:
        if not symbols or self._creds is None:
            return
        request: dict[str, object] = {
            "requests": [
                {
                    "service": self.CHART_EQUITY_SERVICE,
                    "requestid": str(next(self._request_id_counter)),
                    "command": command,
                    "SchwabClientCustomerId": self._creds.customer_id,
                    "SchwabClientCorrelId": self._creds.correl_id,
                    "parameters": {
                        "keys": ",".join(symbols),
                    },
                }
            ]
        }
        if command != "UNSUBS":
            request["requests"][0]["parameters"]["fields"] = self.CHART_EQUITY_FIELDS  # type: ignore[index]
        await ws.send(json.dumps(request))  # type: ignore[attr-defined]
        logger.info(
            "[V2-WS-SUB] cmd=%s count=%d sample=%s",
            command,
            len(symbols),
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


__all__ = ["SchwabV2Streamer", "ChartBarCallback", "DisconnectCallback"]
