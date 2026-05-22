"""Service entrypoint for the isolated `schwab_1m_v2` bot.

Sixth service. Runs as its own systemd unit. Subscribes to the existing
`mai_tai:strategy-state` Redis stream to pick up the scanner's confirmed
symbol set, polls Schwab REST for 1m bars + quotes, evaluates the strategy
(placeholder), persists completed bars to `strategy_bar_history`, publishes
its own state to `mai_tai:strategy-state-isolated` so the dashboard renders
the bot like any other, and emits intents to `mai_tai:strategy-intents` for
OMS to consume.

NO imports from `services/strategy_engine_app.py`, `services/strategy_engine.py`,
`market_data/schwab_streamer.py`, `strategy_core/schwab_native_30s.py`, etc.

Idle (no intents, no REST traffic) when:
- v2 enable flag is off (default), OR
- the Schwab token store is empty / unreadable

This lets the service ship + boot before the operator wires credentials
or flips the enable flag.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    stream_name,
)
from project_mai_tai.market_data.schwab_v2_rest_client import (
    ChartBar,
    Quote,
    SchwabV2RestClient,
)
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SERVICE_NAME,
    STRATEGY_CODE,
    SchwabV2IntentEmitter,
    SchwabV2Strategy,
)

logger = logging.getLogger(__name__)

INTERVAL_SECS = 60
STATE_PUBLISH_INTERVAL_SECONDS = 5
POSITION_POLL_INTERVAL_SECONDS = 5
# Max bar age (seconds) for DB-persistence. Older bars are warmup feeds
# that prior service instances already persisted; redoing them on every
# restart would block the bar loop for ~10s per symbol on cold-start.
PERSIST_BAR_AGE_LIMIT_SECONDS = 300
# Bar age (seconds) at which a REST-fed bar signals "REST warmup has
# caught up to live for this symbol." The REST warmup batch returns
# bars oldest-first; the tail of the batch is within ~5 min of wall
# clock and crossing that threshold marks the symbol as ready for
# streamer subscription (W2). 300s matches PERSIST_BAR_AGE_LIMIT_SECONDS
# so we only mark warmed once the same bar would qualify for DB persist.
REST_WARMUP_FRESH_THRESHOLD_SECS = 300.0
INFLIGHT_INTENT_STATUSES_TERMINAL = ("filled", "rejected", "cancelled")
EASTERN_TZ = ZoneInfo("America/New_York")


def _format_eastern(dt: datetime) -> str:
    """Format a datetime as `"YYYY-MM-DD HH:MM:SS AM/PM ET"`, matching the
    existing strategy-engine's `_datetime_str` so the dashboard's max()-based
    derivation of `latest_bot_tick_at` produces a value that's consistent in
    sort order and display with the other bots.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


class SchwabV2BotService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis: Redis | None = None
        self.strategy = SchwabV2Strategy(self.settings)
        self.rest_client: SchwabV2RestClient | None = None
        self.streamer: SchwabV2Streamer | None = None
        self.intent_emitter: SchwabV2IntentEmitter | None = None
        self.session_factory: sessionmaker[Session] | None = session_factory
        self._stop_event = asyncio.Event()
        self._strategy_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state"
        )
        self._isolated_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state-isolated"
        )
        self._strategy_state_last_id = "$"
        self._watchlist: set[str] = set()
        self._bar_counts: dict[str, int] = {}
        self._last_tick_at: dict[str, str] = {}
        self._last_bar_at: dict[str, str] = {}
        # Set of symbols whose REST warmup batch has caught up to within
        # REST_WARMUP_FRESH_THRESHOLD_SECS of wall clock. Streamer
        # subscriptions are gated on this set (W2: streamer doesn't
        # subscribe to a symbol until REST has fed its history, so
        # streamer can't drop live bars onto an empty deque ahead of the
        # historical context).
        self._rest_warmup_done: set[str] = set()
        # C3 routing counters — exposed via heartbeat for observability.
        # `rest_bars_gated` increments on REST bars suppressed because
        # streamer is healthy and already has the bucket. `rest_bars_gap_fill`
        # increments on REST bars that pass through while streamer is
        # connected (genuine gap fills where streamer missed a bucket).
        self._rest_bars_gated: int = 0
        self._rest_bars_gap_fill: int = 0
        self._data_health: dict[str, object] = {
            "status": "starting",
            "halted_symbols": [],
            "warning_symbols": [],
        }

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "strategy_schwab_1m_v2_enabled", False))

    @property
    def streamer_enabled(self) -> bool:
        """Streamer subsumes the REST bar-poll path for live bars. REST keeps
        running concurrently for cold-start warmup + reconnect gap-fill —
        both feed `_handle_bar`, which is idempotent at strategy + persist
        layers via the strategy's same-bucket update semantics and the
        UPSERT in `_persist_bar`.
        """
        return self.enabled and bool(
            getattr(self.settings, "strategy_schwab_1m_v2_streamer_enabled", False)
        )

    async def run(self) -> None:
        logging.basicConfig(
            level=self.settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        )
        logger.info("schwab_1m_v2 bot starting (enabled=%s)", self.enabled)

        if not self.enabled:
            logger.warning(
                "schwab_1m_v2 disabled: set MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=true "
                "to activate. Service will heartbeat as degraded and idle."
            )

        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        if self.session_factory is None:
            try:
                self.session_factory = build_session_factory(self.settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schwab_1m_v2 session_factory unavailable, bar persistence "
                    "disabled: %s",
                    exc,
                )
        self.intent_emitter = SchwabV2IntentEmitter(
            self.settings,
            self.redis,
            broker_account_name=self.settings.strategy_schwab_1m_v2_account_name,
        )
        self.rest_client = SchwabV2RestClient(
            self.settings,
            on_chart_bar=self._handle_bar_from_rest,
            on_quote=self._handle_quote,
        )
        self.streamer = SchwabV2Streamer(
            self.settings,
            on_chart_bar=self._handle_bar_from_streamer,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows event loops don't support add_signal_handler;
                # the SIGTERM path on Linux is the production case.
                pass

        await self._publish_heartbeat("starting")
        self._data_health["status"] = "healthy" if self.enabled else "degraded"

        tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._state_publish_loop()),
        ]
        if self.enabled:
            tasks.append(asyncio.create_task(self.rest_client.run()))
            tasks.append(asyncio.create_task(self._scanner_consumer_loop()))
            tasks.append(asyncio.create_task(self._position_poll_loop()))
            if self.streamer_enabled:
                tasks.append(asyncio.create_task(self.streamer.run()))
                logger.info(
                    "[V2-WS-INIT] schwab_v2 streamer enabled, REST polling "
                    "continues for cold-start warmup + reconnect gap-fill"
                )

        try:
            await self._stop_event.wait()
        finally:
            await self._publish_heartbeat("stopping")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.streamer is not None:
                await self.streamer.stop()
            if self.rest_client is not None:
                await self.rest_client.stop()
            if self.redis is not None:
                await self.redis.aclose()

    async def _publish_heartbeat(self, status: str) -> None:
        if self.redis is None:
            return
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=SERVICE_NAME,
                status=status,  # type: ignore[arg-type]
                details={
                    "enabled": str(self.enabled).lower(),
                    "strategy_code": STRATEGY_CODE,
                    "rest_configured": str(
                        bool(self.rest_client and self.rest_client.configured)
                    ).lower(),
                    "streamer_enabled": str(self.streamer_enabled).lower(),
                    "streamer_connected": str(
                        bool(self.streamer and self.streamer.connected)
                    ).lower(),
                    "watchlist_size": str(len(self._watchlist)),
                    "warmed_size": str(len(self._rest_warmup_done)),
                    "bars_processed": str(sum(self._bar_counts.values())),
                    "rest_bars_gated_total": str(self._rest_bars_gated),
                    "rest_bars_gap_fill_total": str(self._rest_bars_gap_fill),
                },
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(5, int(self.settings.service_heartbeat_interval_seconds))
        while not self._stop_event.is_set():
            try:
                status = "healthy" if self.enabled else "degraded"
                await self._publish_heartbeat(status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _state_publish_loop(self) -> None:
        """Publish StrategyBotStatePayload to strategy-state-isolated stream
        so the dashboard renders the v2 bot like any other.
        """
        while not self._stop_event.is_set():
            try:
                await self._publish_bot_state()
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 bot-state publish failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=STATE_PUBLISH_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    async def _publish_bot_state(self) -> None:
        if self.redis is None:
            return
        payload = StrategyBotStatePayload(
            strategy_code=STRATEGY_CODE,
            account_name=self.settings.strategy_schwab_1m_v2_account_name,
            watchlist=sorted(self._watchlist),
            prewarm_symbols=[],
            data_health=dict(self._data_health),
            retention_states=[],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=0.0,
            closed_today=[],
            recent_decisions=[],
            indicator_snapshots=[],
            bar_counts=dict(self._bar_counts),
            last_tick_at=dict(self._last_tick_at),
        )
        event = IsolatedBotStateEvent(source_service=SERVICE_NAME, payload=payload)
        await self.redis.xadd(
            self._isolated_state_stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
            approximate=True,
        )

    async def _position_poll_loop(self) -> None:
        """Poll virtual_positions + in-flight trade_intents for v2's broker
        account every 5s; feed results into the strategy's per-symbol state.

        The strategy's update_position() detects the True→False transition
        (OMS closed our position) and arms the cooldown, so we never
        re-enter on the same bar an exit fired on.

        In-flight intents (status NOT IN filled/rejected/cancelled) also
        count as "in position" — covers the gap between intent emission
        and virtual_positions row creation, preventing duplicate opens.
        """
        while not self._stop_event.is_set():
            try:
                positions = await asyncio.to_thread(self._fetch_open_positions)
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 position poll failed: %s", exc)
                positions = None
            if positions is not None:
                tracked = set(self._watchlist) | set(
                    self.strategy._symbol_states.keys()
                )
                for symbol in tracked:
                    qty = positions.get(symbol, 0)
                    self.strategy.update_position(symbol, qty)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=POSITION_POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    def _fetch_open_positions(self) -> dict[str, int]:
        """SQL: virtual_positions(qty>0) ∪ in-flight trade_intents(open)
        for the v2 broker account, keyed by symbol. Quantity is the max
        across sources (a conservative "do we own this" signal).
        """
        if self.session_factory is None:
            return {}
        account_name = self.settings.strategy_schwab_1m_v2_account_name
        positions: dict[str, int] = {}
        try:
            with self.session_factory() as session:
                broker = session.scalar(
                    select(BrokerAccount).where(BrokerAccount.name == account_name)
                )
                if broker is None:
                    return positions
                # Virtual positions = mai-tai's authoritative view of what
                # we own (synchronized by OMS on fills).
                for vp in session.scalars(
                    select(VirtualPosition).where(
                        VirtualPosition.broker_account_id == broker.id,
                        VirtualPosition.quantity > 0,
                    )
                ).all():
                    symbol = str(vp.symbol or "").upper()
                    if symbol:
                        positions[symbol] = max(
                            positions.get(symbol, 0), int(vp.quantity)
                        )
                # In-flight open intents — block re-entry until OMS resolves
                # the prior intent (filled / rejected / cancelled).
                strategy = session.scalar(
                    select(Strategy).where(Strategy.code == "schwab_1m_v2")
                )
                if strategy is not None:
                    for ti in session.scalars(
                        select(TradeIntent).where(
                            TradeIntent.strategy_id == strategy.id,
                            TradeIntent.intent_type == "open",
                            TradeIntent.status.notin_(
                                INFLIGHT_INTENT_STATUSES_TERMINAL
                            ),
                        )
                    ).all():
                        symbol = str(ti.symbol or "").upper()
                        if symbol:
                            qty = int(ti.quantity or 0) or 1
                            positions[symbol] = max(positions.get(symbol, 0), qty)
        except Exception:
            logger.exception("schwab_1m_v2 _fetch_open_positions failed")
        return positions

    async def _scanner_consumer_loop(self) -> None:
        """Seed from the latest existing strategy-state snapshot, then tail
        for new ones. The seed step is critical on cold-start because
        strategy-engine publishes its snapshot only on bar / intent events,
        which can be minutes apart in pre-market; without the seed, the v2
        bot's watchlist stays empty until the next downstream event fires.
        """
        assert self.redis is not None
        assert self.rest_client is not None
        max_watchlist = max(
            1, int(self.settings.strategy_schwab_1m_v2_max_watchlist_size)
        )

        # Step 1: seed from the latest snapshot already in the stream.
        try:
            seed = await self.redis.xrevrange(
                self._strategy_state_stream, count=1
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("schwab_1m_v2 scanner seed xrevrange failed: %s", exc)
            seed = []
        for entry_id, data in seed:
            self._strategy_state_last_id = entry_id
            self._apply_strategy_state_event(data, max_watchlist=max_watchlist)

        # Step 2: tail for new snapshots.
        while not self._stop_event.is_set():
            try:
                response = await self.redis.xread(
                    streams={self._strategy_state_stream: self._strategy_state_last_id},
                    count=10,
                    block=5_000,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 scanner xread failed: %s", exc)
                await asyncio.sleep(2.0)
                continue
            if not response:
                continue
            for _stream_key, entries in response:
                for entry_id, data in entries:
                    self._strategy_state_last_id = entry_id
                    self._apply_strategy_state_event(data, max_watchlist=max_watchlist)

    def _apply_strategy_state_event(
        self, data: object, *, max_watchlist: int
    ) -> None:
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, str):
            return
        try:
            event = StrategyStateSnapshotEvent.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            return
        symbols = self._extract_confirmed_symbols(event)
        if not symbols:
            return
        selected = set(sorted(symbols)[:max_watchlist])
        if selected == self._watchlist:
            return
        self._watchlist = selected
        # W2: drop warmup state for symbols that left the watchlist.
        # If they re-join later, REST needs to refetch the batch and
        # mark them warmed again before streamer is told about them.
        self._rest_warmup_done &= selected
        if self.rest_client is not None:
            self.rest_client.set_desired_symbols(selected)
        if self.streamer is not None:
            # Streamer only subscribes to symbols REST has confirmed
            # warmed. Newly-added symbols will be added to the streamer
            # subscription set incrementally as REST batches complete
            # (see `_handle_bar_from_rest`).
            self.streamer.set_desired_symbols(selected & self._rest_warmup_done)
        logger.info(
            "schwab_1m_v2 watchlist updated count=%d sample=%s warmed=%d",
            len(selected),
            ",".join(sorted(selected)[:5]),
            len(self._rest_warmup_done),
        )

    @staticmethod
    def _extract_confirmed_symbols(event: StrategyStateSnapshotEvent) -> set[str]:
        payload = event.payload
        candidates: list[dict | str] = []
        candidates.extend(payload.all_confirmed)
        candidates.extend(payload.top_confirmed)
        symbols: set[str] = set()
        for item in candidates:
            if isinstance(item, dict):
                sym = str(item.get("symbol", "")).strip().upper()
                if sym:
                    symbols.add(sym)
            elif isinstance(item, str):
                cleaned = item.strip().upper()
                if cleaned:
                    symbols.add(cleaned)
        for sym in payload.watchlist:
            cleaned = str(sym).strip().upper()
            if cleaned:
                symbols.add(cleaned)
        return symbols

    async def _handle_bar_from_rest(self, symbol: str, bar: ChartBar) -> None:
        """REST callback. C3+W2 routing:

        - If REST has caught up to live (bar age < REST_WARMUP_FRESH_THRESHOLD_SECS)
          mark this symbol's warmup as done. New warmups extend the
          streamer's subscription set (W2: streamer doesn't see a symbol
          until REST has fed its history).
        - If the streamer is connected AND has already delivered a bar
          at this `bar.timestamp_ms` or later, skip the strategy feed
          (C3: streamer is signal source of truth when healthy; REST
          is warmup + gap fill only).
        - Otherwise forward to `_handle_bar` (REST is the only live
          feed, or this is a genuine gap fill bar that the streamer
          missed during a disconnect window).
        """
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        bar_age_secs = (now_ms - bar.timestamp_ms) / 1000.0
        if (
            bar_age_secs <= REST_WARMUP_FRESH_THRESHOLD_SECS
            and symbol not in self._rest_warmup_done
        ):
            self._rest_warmup_done.add(symbol)
            self._extend_streamer_subscriptions_to_warmed()
            logger.info(
                "[V2-REST-WARMED] schwab_v2 REST warmup complete for %s "
                "(streamer can now subscribe; warmed=%d/%d)",
                symbol,
                len(self._rest_warmup_done),
                len(self._watchlist),
            )

        if self._should_skip_rest_strategy_feed(symbol, bar):
            self._rest_bars_gated += 1
            return

        # When streamer is connected but didn't pre-empt this bar, count
        # it as gap-fill (something streamer didn't deliver — disconnect,
        # missed bucket, or symbol not yet streamer-subscribed).
        if self.streamer is not None and self.streamer.connected:
            self._rest_bars_gap_fill += 1

        await self._handle_bar(symbol, bar)

    async def _handle_bar_from_streamer(self, symbol: str, bar: ChartBar) -> None:
        """Streamer callback. Streamer is always trusted; C3 keeps REST
        out of its way."""
        await self._handle_bar(symbol, bar)

    def _should_skip_rest_strategy_feed(self, symbol: str, bar: ChartBar) -> bool:
        """C3 gating: when streamer is connected and has already
        delivered a bar at the same timestamp (or later) for this
        symbol, REST's same-bucket fetch is redundant. Returning True
        suppresses the strategy feed; the bar is still consumed from
        the REST loop (idempotent on REST's internal cursor).

        When streamer is disconnected OR has not yet delivered any bar
        for this symbol (e.g. just subscribed, waiting for the next
        minute), REST is the only feed and must pass through.
        """
        if self.streamer is None or not self.streamer.connected:
            return False
        streamer_last_ts = self.streamer.last_bar_ts_ms(symbol)
        if streamer_last_ts <= 0:
            return False
        return bar.timestamp_ms <= streamer_last_ts

    def _extend_streamer_subscriptions_to_warmed(self) -> None:
        """W2: after a REST warmup completes, refresh the streamer's
        desired-symbol set to include all warmed symbols intersected
        with the current watchlist."""
        if self.streamer is None:
            return
        warmed = self._watchlist & self._rest_warmup_done
        self.streamer.set_desired_symbols(warmed)

    async def _handle_bar(self, symbol: str, bar: ChartBar) -> None:
        now_et = _format_eastern(datetime.now(UTC))
        self._last_tick_at[symbol] = now_et
        self._last_bar_at[symbol] = now_et
        self._bar_counts[symbol] = self._bar_counts.get(symbol, 0) + 1

        # Only DB-persist bars within the freshness window. The cold-start
        # warmup batch (up to ~500 historical bars per symbol) was already
        # persisted by a prior service instance; re-writing them serializes
        # ~5k SQL roundtrips across all symbols and stalls the bar loop.
        # In-memory indicator state still consumes EVERY bar via strategy.on_bar.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        bar_age_secs = (now_ms - bar.timestamp_ms) / 1000.0
        if bar_age_secs <= PERSIST_BAR_AGE_LIMIT_SECONDS:
            await asyncio.to_thread(self._persist_bar, symbol, bar)

        try:
            draft = self.strategy.on_bar(symbol, bar)
        except Exception:
            logger.exception("schwab_1m_v2 on_bar failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    async def _handle_quote(self, symbol: str, quote: Quote) -> None:
        self._last_tick_at[symbol] = _format_eastern(datetime.now(UTC))
        try:
            draft = self.strategy.on_quote(symbol, quote)
        except Exception:
            logger.exception("schwab_1m_v2 on_quote failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    def _persist_bar(self, symbol: str, bar: ChartBar) -> None:
        """Upsert (strategy_code, symbol, interval_secs, bar_time) into
        strategy_bar_history. Mirrors the shape the existing strategy-engine
        writes so the dashboard's decision-tape query treats v2 bars
        identically. decision_status stays '' until the strategy emits
        signals.
        """
        if self.session_factory is None:
            return

        volume = int(bar.volume or 0)
        # No trade_count from Schwab Price History; synthesize so the
        # vol=0+tc=0 placeholder filter behaves correctly downstream.
        trade_count = 1 if volume > 0 else 0
        if volume == 0 and trade_count == 0:
            return

        bar_time = datetime.fromtimestamp(bar.timestamp_ms / 1000.0, UTC)

        try:
            with self.session_factory() as session:
                record = session.scalar(
                    select(StrategyBarHistory).where(
                        StrategyBarHistory.strategy_code == STRATEGY_CODE,
                        StrategyBarHistory.symbol == symbol,
                        StrategyBarHistory.interval_secs == INTERVAL_SECS,
                        StrategyBarHistory.bar_time == bar_time,
                    )
                )
                if record is None:
                    record = StrategyBarHistory(
                        strategy_code=STRATEGY_CODE,
                        symbol=symbol,
                        interval_secs=INTERVAL_SECS,
                        bar_time=bar_time,
                        open_price=Decimal(str(bar.open)),
                        high_price=Decimal(str(bar.high)),
                        low_price=Decimal(str(bar.low)),
                        close_price=Decimal(str(bar.close)),
                        volume=volume,
                        trade_count=trade_count,
                    )
                    session.add(record)
                else:
                    record.open_price = Decimal(str(bar.open))
                    record.high_price = Decimal(str(bar.high))
                    record.low_price = Decimal(str(bar.low))
                    record.close_price = Decimal(str(bar.close))
                    record.volume = volume
                    record.trade_count = trade_count
                session.commit()
        except Exception:
            logger.exception(
                "schwab_1m_v2 failed to persist bar history for %s @ %s",
                symbol,
                bar_time,
            )

    async def _maybe_emit(self, draft) -> None:  # type: ignore[no-untyped-def]
        if draft is None:
            return
        if self.intent_emitter is None:
            logger.warning("schwab_1m_v2 intent dropped — emitter not initialized")
            return
        try:
            await self.intent_emitter.emit(draft)
        except Exception:
            logger.exception("schwab_1m_v2 emit failed")


async def main() -> None:
    service = SchwabV2BotService()
    await service.run()


def run() -> None:
    asyncio.run(main())


# Re-exports for tests / introspection
__all__ = ["SchwabV2BotService", "SERVICE_NAME", "STRATEGY_CODE", "main", "run"]
