from __future__ import annotations

from project_mai_tai.symbol_case import raw_symbol_is_uppercase

import asyncio
import json
import logging
import re
import socket
import time
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import (
    BusyLoadingError,
    ConnectionError as RedisConnectionError,
    RedisError,
    TimeoutError as RedisTimeoutError,
)

from project_mai_tai.events import MarketDataSubscriptionEvent, stream_name
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider, MassiveTradeStream
from project_mai_tai.market_data.models import (
    HistoricalBarsRecord,
    LiveBarRecord,
    MomentumWarningAggregateRecord,
    QuoteTickRecord,
    SnapshotRecord,
    TradeTickRecord,
)
from project_mai_tai.market_data.protocols import SnapshotProvider, TradeStreamProvider
from project_mai_tai.market_data.publisher import MarketDataPublisher, WarningBatchWriteError
from project_mai_tai.market_data.reference_cache import ReferenceDataCache
from project_mai_tai.market_data.stage_diagnostics import StageDiagnostics
from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy
from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealthReader, WarningWatchdog
from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ, is_fillable_et_session

logger = logging.getLogger(__name__)

SERVICE_NAME = "market-data-gateway"
WARMUP_INTERVALS = (30, 60)
WARNING_CONSUMER = "momentum-warning"
# Mirror the reconciler's measured Redis-reload margin: five total attempts
# spanning 7.5 seconds. Persistent failures still escape and let systemd make
# the loss of the heartbeat task visible.
HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)


def _warning_monitor_diagnostic(exc: Exception, stages: dict | None = None) -> str:
    # Never emit URLs/credential fields or multiline exception text into live logs.
    try:
        message = str(exc)[:2048]
    except Exception:
        message = "[message unavailable]"
    message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "[REDACTED_URL]", message)
    message = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[REDACTED_AUTH]", message)
    message = re.sub(
        r"(?i)\b[\w-]*(?:authorization|password|passwd|secret|token|api[_-]?key|chat[_-]?id)"
        r"[\w-]*[\"']?\s*[:=]\s*(?:[\"'][^\"']*[\"']|[^\s,;]+)",
        "[REDACTED_CREDENTIAL]", message,
    )
    message = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]+", "[REDACTED_TOKEN]", message)
    result = {"exception_class": type(exc).__name__[:80], "message": message[:512]}
    if stages is not None:
        result["stages"] = stages
    return json.dumps(result, ensure_ascii=True)


def _is_transient_redis_reload_error(exc: RedisError) -> bool:
    """Recognize only restart-shaped Redis failures, never all Redis errors."""

    return (
        isinstance(exc, (BusyLoadingError, RedisTimeoutError))
        or type(exc) is RedisConnectionError
    )


class MarketDataGatewayService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        trade_stream: TradeStreamProvider | None = None,
        reference_cache: ReferenceDataCache | None = None,
        warning_redis_client: Redis | None = None,
        warning_health_reader: WarningHealthReader | None = None,
    ):
        self.settings = settings or get_settings()
        self._momentum_validation_window = MomentumValidationWindow.from_settings(self.settings)
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.publisher = MarketDataPublisher(
            self.redis,
            self.settings.redis_stream_prefix,
            SERVICE_NAME,
            snapshot_batch_stream_maxlen=self.settings.redis_snapshot_batch_stream_maxlen,
            market_data_stream_maxlen=self.settings.redis_market_data_stream_maxlen,
            heartbeat_stream_maxlen=self.settings.redis_heartbeat_stream_maxlen,
            momentum_warning_stream_maxlen=self.settings.market_data_momentum_warning_stream_maxlen,
        )
        self.snapshot_provider = snapshot_provider or self._build_snapshot_provider()
        self.trade_stream = trade_stream or self._build_trade_stream()
        self.reference_cache = reference_cache or ReferenceDataCache(
            self.snapshot_provider,
            cache_path=self.settings.market_data_reference_cache_path,
            max_age_hours=self.settings.market_data_reference_cache_max_age_hours,
            min_price=self.settings.market_data_scan_min_price,
            max_price=self.settings.market_data_scan_max_price,
            lookback_days=self.settings.market_data_reference_lookback_days,
        )
        self.logger = logging.getLogger(SERVICE_NAME)
        self.instance_name = socket.gethostname()
        self._trade_queue: asyncio.Queue[TradeTickRecord] = asyncio.Queue()
        self._quote_queue: asyncio.Queue[QuoteTickRecord] = asyncio.Queue()
        self._bar_queue: asyncio.Queue[LiveBarRecord] = asyncio.Queue()
        self._live_wakeup = asyncio.Event()
        self._warning_queue: asyncio.Queue[tuple[int, MomentumWarningAggregateRecord]] = asyncio.Queue(
            maxsize=self.settings.market_data_momentum_warning_queue_maxsize
        )
        self._warning_seq = 0
        self._warning_drop_count = 0
        self._warning_queue_drop_count = 0
        self._warning_max_publish_lag_ms = 0.0
        self._warning_active = False
        self._warning_revert_reason: str | None = None
        self._warning_inflight: MomentumWarningAggregateRecord | None = None
        self._warning_publish_task: asyncio.Task | None = None
        self._warning_write_task: asyncio.Task | None = None
        self._warning_generation = 0
        self._warning_instance_id = str(uuid4())
        self._warning_published = 0
        self._warning_ack_unknown = 0
        self._warning_write_waits = deque(maxlen=256)
        self._warning_rate_bins: deque[tuple[int, int]] = deque(maxlen=10)
        self._warning_reverted_utc_ns: int | None = None
        self._warning_requested_utc_ns: int | None = None
        self._warning_first_a_utc_ns: int | None = None
        self._warning_window_day = None
        self._warning_log_gap_counts: dict[str, int] = {}
        self._warning_trade_coverage: dict[str, dict] = {}
        self._warning_watchdog = WarningWatchdog()
        self._warning_pause_policy = WarningPausePolicy()
        self._warning_pause_reporter = None
        self._warning_health_task = None
        self._stage_diagnostics = StageDiagnostics()
        self.publisher.stage_diagnostics = self._stage_diagnostics
        if isinstance(self.snapshot_provider, MassiveSnapshotProvider):
            self.snapshot_provider.stage_diagnostics = self._stage_diagnostics
        self._warning_health_reader = warning_health_reader or WarningHealthReader(
            self.settings.market_data_momentum_warning_health_path,
            self.settings.market_data_momentum_warning_log_directory,
        )
        if isinstance(self._warning_health_reader, WarningHealthReader):
            self._warning_health_reader.stage_diagnostics = self._stage_diagnostics
            self._warning_health_reader.retain_cancelled_harm = True
        self._warning_redis = None
        self._warning_publisher = None
        if self.settings.market_data_momentum_warning_enabled:
            self.trade_stream.configure_momentum_warning(
                self._enqueue_warning, self._latch_warning_revert, self._close_warning_window,
                log_only=True,
                window=self._momentum_validation_window,
            )
            # A has its own Redis pool/task; stalled A writes never serialize T/Q writes.
            self._warning_redis = warning_redis_client or Redis.from_url(
                self.settings.redis_url, decode_responses=True,
                max_connections=2, socket_timeout=1, socket_connect_timeout=1,
            )
            self._warning_publisher = MarketDataPublisher(
                self._warning_redis, self.settings.redis_stream_prefix, SERVICE_NAME,
                momentum_warning_stream_maxlen=self.settings.market_data_momentum_warning_stream_maxlen,
            )
        self._live_aggregate_stream_enabled = (
            self.settings.market_data_live_aggregate_stream_enabled
            or self.settings.strategy_macd_30s_live_aggregate_bars_enabled
            or self.settings.strategy_polygon_30s_runtime_uses_live_aggregate_bars
        )
        self._desired_symbols_by_consumer: dict[str, set[str]] = {
            "static": set(self.settings.market_data_static_symbol_list),
        }
        self._active_symbols: set[str] = set(self.settings.market_data_static_symbol_list)
        # Observability for the DFNS/LGHL incident: how many symbols in the last snapshot batch had
        # NO reference entry. five_pillars silently drops those, so a stale cache is otherwise
        # invisible. Surfaced on the heartbeat so it is monitorable instead of undiagnosable.
        self._snapshots_without_reference = 0
        self._last_snapshot_symbol_count = 0
        self._subscription_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"): "$",
        }
        self._subscription_state_key = stream_name(
            self.settings.redis_stream_prefix, "market-data-subscription-owners"
        )

    async def run(self) -> None:
        if self.settings.market_data_momentum_warning_enabled:
            bind = getattr(self._warning_health_reader, "bind_gateway_start", None)
            if bind is not None:
                bind(self._warning_instance_id, time.time_ns())
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        await self._ensure_reference_data()
        await self._publish_heartbeat(
            "starting",
            {"reference_tickers": str(self.reference_cache.ticker_count())},
        )
        await self._restore_subscription_state()

        loop = asyncio.get_running_loop()
        await self.trade_stream.start(
            on_trade=lambda record: loop.call_soon_threadsafe(self._enqueue_trade, record),
            on_quote=lambda record: loop.call_soon_threadsafe(self._enqueue_live, self._quote_queue, record),
            on_agg=(
                (lambda record: loop.call_soon_threadsafe(self._enqueue_live, self._bar_queue, record))
                if self._live_aggregate_stream_enabled
                else None
            ),
        )
        await self._sync_provider_owners(self._desired_symbols_by_consumer)

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(stop_event),
            name="market-data-heartbeat",
        )
        tasks = [
            asyncio.create_task(self._snapshot_loop(stop_event)),
            asyncio.create_task(self._subscription_loop(stop_event)),
            asyncio.create_task(self._stream_publish_loop(stop_event)),
            heartbeat_task,
            asyncio.create_task(self._reference_refresh_loop(stop_event)),
        ]
        if self.settings.market_data_momentum_warning_enabled:
            self._warning_publish_task = asyncio.create_task(self._warning_publish_loop(stop_event))
            tasks.extend([
                self._warning_publish_task,
                asyncio.create_task(self._warning_monitor_loop(stop_event)),
                asyncio.create_task(self._warning_metrics_loop(stop_event)),
            ])
        regular_symbols = self._regular_symbols(self._desired_symbols_by_consumer)
        if regular_symbols and self.settings.market_data_warmup_enabled:
            tasks.append(
                asyncio.create_task(
                    self._publish_historical_warmup(regular_symbols),
                )
            )

        try:
            await self._wait_for_stop_or_heartbeat_failure(stop_event, heartbeat_task)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._warning_health_task is not None:
                self._warning_health_task.cancel()
                await asyncio.gather(self._warning_health_task, return_exceptions=True)
            if self._warning_pause_reporter is not None:
                if not await self._warning_pause_reporter.close():
                    self.logger.warning("[MOMLATCH1-INCIDENT] shutdown_write_pending=1")
            await self.trade_stream.stop()
            if self._warning_redis is not None:
                await self._warning_redis.aclose()
            await self._publish_heartbeat(
                "stopping",
                {"active_symbols": str(len(self._active_symbols))},
            )
            await self.redis.aclose()

    async def _wait_for_stop_or_heartbeat_failure(
        self,
        stop_event: asyncio.Event,
        heartbeat_task: asyncio.Task[None],
    ) -> None:
        """Return on shutdown; otherwise make heartbeat-task death process-fatal.

        A completed heartbeat task can never be treated as a healthy live
        process. systemd's restart counter is the durable external signal.
        """

        stop_waiter = asyncio.create_task(stop_event.wait(), name="market-data-stop-waiter")
        try:
            done, _pending = await asyncio.wait(
                {stop_waiter, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task not in done:
                return
            if heartbeat_task.cancelled():
                self.logger.critical(
                    "[MARKET-DATA-HEARTBEAT-TASK-DIED] outcome=process_exit "
                    "reason=unexpected_cancel"
                )
                raise RuntimeError("market-data heartbeat task cancelled unexpectedly")
            failure = heartbeat_task.exception()
            if failure is not None:
                self.logger.critical(
                    "[MARKET-DATA-HEARTBEAT-TASK-DIED] outcome=process_exit "
                    "reason=exception error=%s",
                    type(failure).__name__,
                )
                raise failure
            if not stop_event.is_set():
                self.logger.critical(
                    "[MARKET-DATA-HEARTBEAT-TASK-DIED] outcome=process_exit "
                    "reason=unexpected_return"
                )
                raise RuntimeError("market-data heartbeat task returned before shutdown")
        finally:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    async def _publish_heartbeat(self, status: str, details: dict[str, str]) -> None:
        max_attempts = len(HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS) + 1
        transient_failures = 0
        for attempt in range(1, max_attempts + 1):
            try:
                await self.publisher.publish_heartbeat(
                    instance_name=self.instance_name,
                    status=status,
                    details=details,
                )
            except RedisError as exc:
                if not _is_transient_redis_reload_error(exc):
                    self.logger.error(
                        "[MARKET-DATA-HEARTBEAT-FAILED] attempt=%s/%s "
                        "outcome=non_transient_propagated error=%s",
                        attempt,
                        max_attempts,
                        type(exc).__name__,
                    )
                    raise
                transient_failures += 1
                if attempt == max_attempts:
                    self.logger.error(
                        "[MARKET-DATA-HEARTBEAT-FAILED] attempt=%s/%s "
                        "transient_failures=%s outcome=transient_exhausted error=%s",
                        attempt,
                        max_attempts,
                        transient_failures,
                        type(exc).__name__,
                    )
                    raise
                delay_seconds = HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS[attempt - 1]
                self.logger.warning(
                    "[MARKET-DATA-HEARTBEAT-RETRY] attempt=%s/%s "
                    "transient_failures=%s outcome=retrying delay_seconds=%.1f error=%s",
                    attempt,
                    max_attempts,
                    transient_failures,
                    delay_seconds,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay_seconds)
                continue
            if transient_failures:
                self.logger.info(
                    "[MARKET-DATA-HEARTBEAT-RECOVERED] attempt=%s/%s "
                    "transient_failures=%s outcome=published",
                    attempt,
                    max_attempts,
                    transient_failures,
                )
            return

    @property
    def _timings(self):
        diagnostics = getattr(self, "_stage_diagnostics", None)
        if diagnostics is None:
            diagnostics = self._stage_diagnostics = StageDiagnostics()
        return diagnostics

    @property
    def _pauses(self):
        policy = getattr(self, "_warning_pause_policy", None)
        if policy is None:
            policy = self._warning_pause_policy = WarningPausePolicy()
        return policy

    async def publish_snapshot_batch_once(self, snapshots: Iterable[SnapshotRecord]) -> int:
        snapshot_list = list(snapshots)
        reference_payloads = await self._timings.thread(
            "snapshot_references", self.reference_cache.as_payloads, (snapshot.symbol for snapshot in snapshot_list),
        )
        # Count symbols the scanner will silently drop for want of a reference entry (see __init__).
        # `as_payloads` yields ReferenceDataPayload objects in production; accept plain mappings too
        # so a swapped-in cache (tests, fixtures) can't turn an observability counter into a crash.
        known = {
            payload["symbol"] if isinstance(payload, Mapping) else payload.symbol
            for payload in reference_payloads
        }
        self._last_snapshot_symbol_count = len(snapshot_list)
        self._snapshots_without_reference = sum(
            1 for snapshot in snapshot_list if snapshot.symbol not in known
        )
        await self.publisher.publish_snapshot_batch(snapshot_list, reference_payloads)
        return len(snapshot_list)

    async def apply_subscription_event(
        self, event: MarketDataSubscriptionEvent, *, message_id: str | None = None
    ) -> set[str]:
        symbols = {symbol.upper() for symbol in event.payload.symbols if raw_symbol_is_uppercase(symbol)}
        consumer = event.payload.consumer_name
        mode = event.payload.mode

        current = self._desired_symbols_by_consumer.get(consumer, set())
        if mode == "replace":
            updated = symbols
        elif mode == "add":
            updated = current | symbols
        else:
            updated = current - symbols

        if consumer == WARNING_CONSUMER:
            updated = self._validated_warning_symbols(updated)

        candidate_owners = {**self._desired_symbols_by_consumer, consumer: updated}
        for owned in candidate_owners.values():
            MassiveTradeStream._validate_explicit_symbols(owned)
        stream = stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions")
        checkpoint_id = message_id or self._subscription_offsets.get(stream, "0-0")
        if checkpoint_id == "$":
            checkpoint_id = "0-0"
        checkpoint = {
            name: json.dumps(sorted(owned))
            for name, owned in candidate_owners.items()
            if name != "static"
        }
        checkpoint.update(_migration_complete="1", _last_applied_id=checkpoint_id)
        next_symbols = set().union(*candidate_owners.values())
        added_symbols = next_symbols - self._active_symbols
        removed_symbols = self._active_symbols - next_symbols
        await self.redis.hset(self._subscription_state_key, mapping=checkpoint)
        provider_union_changed = self._regular_symbols(candidate_owners) != self._regular_symbols(
            self._desired_symbols_by_consumer
        ) or (
            self.settings.market_data_momentum_warning_enabled
            and candidate_owners.get(WARNING_CONSUMER, set()) != self._desired_symbols_by_consumer.get(WARNING_CONSUMER, set())
        )
        if provider_union_changed:
            await self._sync_provider_owners(candidate_owners)
        warmup_symbols = self._regular_symbols(candidate_owners) - self._regular_symbols(
            self._desired_symbols_by_consumer
        )
        self._desired_symbols_by_consumer = candidate_owners
        self._active_symbols = next_symbols
        self.logger.info(
            "[MARKET-DATA-SUBSCRIPTION-UNION] consumer=%s added=%s removed=%s count=%d",
            consumer, ",".join(sorted(added_symbols)) or "-",
            ",".join(sorted(removed_symbols)) or "-", len(next_symbols),
        )
        if warmup_symbols:
            self.logger.info(
                "[MARKET-DATA-WARMUP-UNION-ADD] consumer=%s symbols=%s count=%d",
                consumer, ",".join(sorted(warmup_symbols)), len(warmup_symbols),
            )
            await self._publish_historical_warmup(warmup_symbols)
        return set(self._active_symbols)

    def active_symbols(self) -> set[str]:
        return set(self._active_symbols)

    @staticmethod
    def _regular_symbols(owners: dict[str, set[str]]) -> set[str]:
        return set().union(*(symbols for owner, symbols in owners.items() if owner != WARNING_CONSUMER))

    def _validated_warning_symbols(self, symbols: set[str]) -> set[str]:
        MassiveTradeStream._validate_explicit_symbols(symbols)
        if len(symbols) > self.settings.market_data_momentum_warning_owner_cap:
            raise ValueError("momentum-warning T owner exceeds cap")
        return symbols if self.settings.market_data_momentum_warning_enabled else set()

    async def _sync_provider_owners(self, owners: dict[str, set[str]]) -> None:
        await self.trade_stream.sync_subscriptions(sorted(self._regular_symbols(owners)))
        if self.settings.market_data_momentum_warning_enabled:
            symbols = owners.get(WARNING_CONSUMER, set())
            await self.trade_stream.sync_warning_trade_symbols(sorted(symbols))
            self._warning_trade_coverage = {
                symbol: self._warning_trade_coverage.get(symbol) or {
                    "requested_utc_ns": time.time_ns(), "first_event_observed_ns": None,
                    "first_trade_sip_ns": None, "subscription_confirmed": None,
                    "status": "requested",
                } for symbol in sorted(symbols)
            }

    def _build_snapshot_provider(self) -> SnapshotProvider:
        if not self.settings.massive_api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required for market-data polling.")
        return MassiveSnapshotProvider(self.settings.massive_api_key)

    def _build_trade_stream(self) -> TradeStreamProvider:
        if not self.settings.massive_api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required for market-data streaming.")
        return MassiveTradeStream(self.settings.massive_api_key)

    async def _ensure_reference_data(self) -> None:
        loaded = await asyncio.to_thread(self.reference_cache.load_from_cache)
        if loaded:
            return
        await asyncio.to_thread(self.reference_cache.build)

    async def _reference_refresh_loop(self, stop_event: asyncio.Event) -> None:
        """Periodically re-check the reference cache (the DFNS/LGHL incident, 2026-07-27).

        `_ensure_reference_data()` was only called once at startup, so a gateway with long uptime
        served an ever-staler cache forever — ours reached 19 days. `load_from_cache()` already
        honours the max-age and returns False when stale, so simply re-invoking it on a timer is the
        whole fix: a FRESH cache costs one file read, a STALE one triggers `build()` (which already
        runs off-loop via `asyncio.to_thread`, so the snapshot loop keeps publishing throughout).
        Interval <= 0 disables the loop (restores the previous boot-only behaviour)."""
        interval = int(self.settings.market_data_reference_refresh_interval_seconds)
        if interval <= 0:
            self.logger.info("reference-data periodic refresh DISABLED (interval <= 0)")
            return
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return  # stop requested
            except TimeoutError:
                pass
            try:
                before = self.reference_cache.ticker_count()
                await self._ensure_reference_data()
                after = self.reference_cache.ticker_count()
                if after != before:
                    self.logger.info(
                        "reference data refreshed: %s -> %s tickers", before, after
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a refresh failure kill the gateway — the old cache keeps serving.
                self.logger.exception("reference data refresh failed; keeping the existing cache")

    async def _snapshot_loop(self, stop_event: asyncio.Event) -> None:
        interval = max(1, self.settings.market_data_snapshot_interval_seconds)
        while not stop_event.is_set():
            try:
                fetch_started_ns = time.monotonic_ns()
                snapshots = await self._timings.thread("snapshot_fetch", self.snapshot_provider.fetch_all_snapshots)
                publish_started_ns = time.monotonic_ns()
                count = await self.publish_snapshot_batch_once(snapshots)
                publish_completed_ns = time.monotonic_ns()
                self.logger.info(
                    "published snapshot batch with %s records publish_started_monotonic_ns=%s "
                    "publish_completed_monotonic_ns=%s publish_wall_ms=%.3f fetch_wall_ms=%.3f",
                    count, publish_started_ns, publish_completed_ns,
                    (publish_completed_ns - publish_started_ns) / 1e6,
                    (publish_started_ns - fetch_started_ns) / 1e6,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("snapshot polling failed")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _subscription_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(self._subscription_offsets, block=1000, count=50)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("subscription stream read failed")
                await asyncio.sleep(1)
                continue

            for stream, entries in messages:
                for message_id, fields in entries:
                    try:
                        data = fields.get("data")
                        if not data:
                            self._subscription_offsets[stream] = message_id
                            continue
                        event = MarketDataSubscriptionEvent.model_validate(json.loads(data))
                        symbols = await self.apply_subscription_event(event, message_id=message_id)
                        self._subscription_offsets[stream] = message_id
                        self.logger.info(
                            "market-data subscriptions updated by %s -> %s symbols",
                            event.payload.consumer_name,
                            len(symbols),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.logger.exception("failed to apply market-data subscription event")

    async def _restore_subscription_state(self) -> None:
        stream = stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions")
        try:
            latest = await self.redis.xrevrange(stream, count=1)
            saved = await self.redis.hgetall(self._subscription_state_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("failed to read market-data subscription state")
            raise

        migrated = saved.get("_migration_complete") == "1"
        checkpoint = saved.get("_last_applied_id") if migrated else None
        if migrated:
            if latest and checkpoint is None:
                raise RuntimeError("subscription owner state has no stream checkpoint")
            for consumer, encoded in saved.items():
                if consumer in {"_migration_complete", "_last_applied_id"}:
                    continue
                symbols = json.loads(encoded)
                if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
                    raise RuntimeError(f"invalid subscription state for {consumer}")
                self._desired_symbols_by_consumer[consumer] = {
                    symbol.upper() for symbol in symbols if raw_symbol_is_uppercase(symbol)
                }
                if consumer == WARNING_CONSUMER:
                    self._desired_symbols_by_consumer[consumer] = self._validated_warning_symbols(
                        self._desired_symbols_by_consumer[consumer]
                    )
        # Replay updates newer than the durable checkpoint. On first upgrade,
        # replay all retained events rather than only the last consumer's event.
        entries = await self.redis.xrange(stream, min=f"({checkpoint}" if checkpoint else "-")
        for message_id, fields in entries:
            data = fields.get("data")
            if not data:
                continue
            event = MarketDataSubscriptionEvent.model_validate(json.loads(data))
            consumer = event.payload.consumer_name
            symbols = {symbol.upper() for symbol in event.payload.symbols if raw_symbol_is_uppercase(symbol)}
            if not migrated and consumer not in self._desired_symbols_by_consumer and event.payload.mode != "replace":
                raise RuntimeError(f"retained history for {consumer} does not start with replace")
            current = self._desired_symbols_by_consumer.get(consumer, set())
            if event.payload.mode == "replace":
                updated = symbols
            elif event.payload.mode == "add":
                updated = current | symbols
            else:
                updated = current - symbols
            if consumer == WARNING_CONSUMER:
                updated = self._validated_warning_symbols(updated)
            self._desired_symbols_by_consumer[consumer] = updated
        if entries or not migrated:
            state = {
                consumer: json.dumps(sorted(symbols))
                for consumer, symbols in self._desired_symbols_by_consumer.items()
                if consumer != "static"
            }
            state["_migration_complete"] = "1"
            if entries:
                checkpoint = entries[-1][0]
            checkpoint = checkpoint or "0-0"
            state["_last_applied_id"] = checkpoint
            await self.redis.hset(self._subscription_state_key, mapping=state)
        if checkpoint:
            self._subscription_offsets[stream] = checkpoint
        if not migrated:
            self.logger.warning(
                "bootstrapped market-data subscription owners from %s retained events; "
                "verify every expected consumer before gateway deployment",
                len(entries),
            )
        self._active_symbols = set().union(*self._desired_symbols_by_consumer.values())
        self.logger.info(
            "restored market-data subscriptions from %s consumers -> %s symbols",
            len(self._desired_symbols_by_consumer),
            len(self._active_symbols),
        )

    async def _stream_publish_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self._live_wakeup.clear()
            published = False
            for _ in range(self.settings.market_data_publish_round_size):
                for queue, publish in (
                    (self._trade_queue, self._publish_trade_tick_safely),
                    (self._quote_queue, self._publish_quote_tick_safely),
                    (self._bar_queue, self._publish_live_bar_safely),
                ):
                    if not queue.empty():
                        published = True
                        await publish(queue.get_nowait())
            if published:
                await asyncio.sleep(0)
            else:
                try:
                    await asyncio.wait_for(self._live_wakeup.wait(), timeout=0.01)
                except TimeoutError:
                    pass

    def _enqueue_live(self, queue: asyncio.Queue, record) -> None:
        queue.put_nowait(record)
        self._live_wakeup.set()

    def _enqueue_trade(self, record: TradeTickRecord) -> None:
        coverage = self._warning_trade_coverage.get(record.symbol)
        if coverage is not None and coverage["first_event_observed_ns"] is None:
            coverage.update(first_event_observed_ns=time.time_ns(),
                            first_trade_sip_ns=record.timestamp_ns, status="provider_event_observed")
        self._enqueue_live(self._trade_queue, record)

    def _latch_warning_revert(self, reason: str, *, diagnostic: str | None = None) -> None:
        self._warning_observed_reason = reason
        if reason in {"disconnect", "policy_1008", "reconnect_1012"}:
            self._pauses.reconnect_event_count += 1
        self.logger.warning("[MOMSIMPLE1-OBSERVATION] reason=%s action=none%s", reason,
                            f" detail={diagnostic}" if diagnostic is not None else "")

    async def _revert_warning(self, reason: str, *, diagnostic: str | None = None) -> None:
        self._latch_warning_revert(reason, diagnostic=diagnostic)

    def _close_warning_window(self, *, reset_monitor: bool = True) -> None:
        if self._warning_revert_reason is not None:
            return
        was_active = self._warning_active or self._warning_requested_utc_ns is not None
        self._warning_active = False
        while not self._warning_queue.empty():
            self._warning_queue.get_nowait()
            self._warning_drop_count += 1
        if self._warning_write_task is not None:
            self._warning_write_task.cancel()
        self._warning_requested_utc_ns = None
        self._warning_first_a_utc_ns = None
        if was_active and self._warning_revert_reason is None and reset_monitor:
            self._warning_watchdog = WarningWatchdog()
            reset = getattr(self._warning_health_reader, "reset_window", None)
            if reset is not None:
                reset()

    def _warning_window_open(self) -> bool:
        now = datetime.now(UTC)
        end = self._momentum_validation_window.gateway_end_for(now.astimezone(EASTERN_TZ).date())
        return is_fillable_et_session(now, 4, end.hour, end_minute=end.minute)

    async def _end_warning_window(self) -> None:
        self._pauses.close_window()
        self._close_warning_window()
        try:
            await self.trade_stream.set_momentum_warning_aggregates(False)
        except Exception:
            await self._revert_warning("unsubscribe_error")

    def _enqueue_warning(self, record: MomentumWarningAggregateRecord) -> None:
        if not self._warning_active or self._warning_revert_reason is not None:
            return
        self._warning_seq += 1
        if self._warning_first_a_utc_ns is None:
            self._warning_first_a_utc_ns = record.received_utc_ns
        second = record.received_utc_ns // 1_000_000_000
        if self._warning_rate_bins and self._warning_rate_bins[-1][0] == second:
            sec, count = self._warning_rate_bins.pop()
            self._warning_rate_bins.append((sec, count + 1))
        else:
            self._warning_rate_bins.append((second, 1))
        if self._warning_queue.full():
            self._warning_queue.get_nowait()
            self._warning_drop_count += 1
            self._warning_queue_drop_count += 1
        self._warning_queue.put_nowait((self._warning_seq, record))

    def _warning_publish_lag(self) -> float:
        records = list(getattr(self, '_warning_inflight_records', ()))
        if self._warning_inflight is not None:
            records.append(self._warning_inflight)
        if not self._warning_queue.empty():
            records.append(self._warning_queue._queue[0][1])
        return max((max(0, (time.monotonic_ns() - r.received_monotonic_ns) / 1e9) for r in records), default=0)

    async def _warning_publish_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() and self._warning_revert_reason is None:
            try:
                seq, record = await asyncio.wait_for(self._warning_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            self._warning_inflight = record
            pending = [(seq, record)]
            while len(pending) < 32 and not self._warning_queue.empty():
                pending.append(self._warning_queue.get_nowait())
            self._warning_inflight_records = tuple(item[1] for item in pending)
            acknowledged = 0
            try:
                # A normal daily boundary takes precedence over stale pending rows.
                now = datetime.now(UTC)
                end = self._momentum_validation_window.gateway_end_for(now.astimezone(EASTERN_TZ).date())
                if not is_fillable_et_session(now, 4, end.hour, end_minute=end.minute):
                    self._warning_drop_count += len(pending)
                    await self._end_warning_window()
                    continue
                lag_seconds = max((time.monotonic_ns() - item.received_monotonic_ns) / 1e9
                                  for _, item in pending)
                self._warning_max_publish_lag_ms = max(self._warning_max_publish_lag_ms, lag_seconds * 1000)
                if lag_seconds > 2:
                    self._warning_drop_count += len(pending)
                    await self._revert_warning("publish_lag")
                    continue
                if len(pending) == 1:
                    write = self._warning_publisher.publish_momentum_warning_aggregate(
                        record, sequence=seq, dropped_total=self._warning_drop_count,
                        gateway_instance_id=self._warning_instance_id, generation=self._warning_generation)
                else:
                    write = self._warning_publisher.publish_momentum_warning_batch(
                        tuple(pending), dropped_total=self._warning_drop_count,
                        gateway_instance_id=self._warning_instance_id, generation=self._warning_generation)
                self._warning_write_task = asyncio.create_task(write)
                write_started_ns = time.monotonic_ns()
                await asyncio.wait_for(self._warning_write_task, timeout=1)
                self._warning_write_waits.append((len(pending), (time.monotonic_ns()-write_started_ns)/1e6))
                acknowledged = len(pending)
                self._warning_published += acknowledged
            except WarningBatchWriteError as exc:
                acknowledged = exc.acknowledged
                self._warning_published += acknowledged
                self._warning_drop_count += len(pending)-acknowledged
                self._warning_ack_unknown += exc.unknown
                await self._revert_warning('publish_error', diagnostic=f'acknowledged={acknowledged} unknown_ack={exc.unknown} batch={len(pending)}')
                continue
            except asyncio.CancelledError:
                self._warning_drop_count += len(pending)-acknowledged
                if self._warning_write_task is not None:
                    self._warning_ack_unknown += len(pending)-acknowledged
                if asyncio.current_task().cancelling() or self._warning_revert_reason is not None:
                    raise
                # Only the in-flight A write was cancelled by an A-only pause/window close.
                continue
            except Exception as exc:
                self._warning_drop_count += len(pending)-acknowledged
                if self._warning_write_task is not None:
                    self._warning_ack_unknown += len(pending)-acknowledged
                await self._revert_warning("publish_error", diagnostic=_warning_monitor_diagnostic(exc))
                continue
            finally:
                self._warning_inflight = None
                self._warning_inflight_records = ()
                self._warning_write_task = None
            await asyncio.sleep(0)

    async def _warning_monitor_once(self) -> None:
        if self._warning_revert_reason is not None:
            return
        now = datetime.now(UTC)
        end = self._momentum_validation_window.gateway_end_for(now.astimezone(EASTERN_TZ).date())
        if not self.settings.market_data_momentum_warning_enabled or not is_fillable_et_session(now, 4, end.hour, end_minute=end.minute):
            await self._end_warning_window()
            return
        try:
            day = now.astimezone(EASTERN_TZ).date()
            if day != self._warning_window_day:
                reset = getattr(self._warning_health_reader, "reset_window", None)
                if reset is not None:
                    reset()
                self._warning_watchdog = WarningWatchdog()
                self._warning_pause_policy = WarningPausePolicy()
                self._warning_window_day = day
                self._warning_log_gap_counts.clear()
            self._pauses.open_window()
            # Subscription intent is independent of measurement availability.
            was_active = self._warning_active
            subscribed = await self.trade_stream.set_momentum_warning_aggregates(True)
            self._warning_active = subscribed or was_active
            if subscribed and not was_active:
                self._warning_generation += 1
                self._warning_requested_utc_ns = time.time_ns()
                self._warning_first_a_utc_ns = None
            if self._warning_health_task is None:
                live_reader = getattr(self._warning_health_reader, "read_live", None)
                operation = (
                    live_reader(self._warning_redis, self.settings.redis_stream_prefix)
                    if live_reader is not None else asyncio.to_thread(self._warning_health_reader.read)
                )
                self._warning_health_task = asyncio.create_task(self._timings.asynchronous("health_total", operation))
            # A timed-out to_thread cannot be stopped. Reuse its ONE task until
            # completion instead of concurrently mutating source/log cursors.
            try:
                health = await asyncio.wait_for(
                    asyncio.shield(self._warning_health_task),
                    timeout=self.settings.market_data_momentum_warning_monitor_timeout_seconds,
                )
            finally:
                if self._warning_health_task.done():
                    self._warning_health_task = None
            observability = getattr(self._warning_health_reader, "log_observability", None)
            if observability is not None:
                counts = observability()["copytruncate_counts"]
                if any(count > self._warning_log_gap_counts.get(name, 0) for name, count in counts.items()):
                    self.logger.warning(
                        "[MOMWARN1-LOG-REBASELINE] unobserved_interval=UNKNOWN detail=%s",
                        json.dumps(counts, sort_keys=True),
                    )
                self._warning_log_gap_counts = counts
            reason = self._warning_watchdog.check_recoverable(health, publish_lag_seconds=self._warning_publish_lag())
            self._warning_health_sample = health
            self._warning_observed_reason = reason
            if getattr(health, "v2_cohort", None) is not None:
                metrics = self._warning_watchdog.cohort_metrics()
                key = (metrics["due_ns"], metrics["status"], metrics["late_streak"], metrics["missing_symbols"])
                if metrics["status"] == "INCOMPLETE" and self.__dict__.get("_warning_cohort_log_key") != key:
                    self.logger.info("[MOMWARN1-V2-COHORT] status=%s incomplete_sampled_minutes=%d may_later_complete=1 "
                                     "unobserved_gap_minutes=%d late_streak=%d lateness=%s missing=%s",
                                     metrics["status"], metrics["incomplete_minutes"],
                                     metrics["unobserved_gap_minutes"], metrics["late_streak"],
                                     metrics["lateness_seconds"] if metrics["lateness_seconds"] is not None else "UNKNOWN",
                                     metrics["missing_symbols"])
                    self._warning_cohort_log_key = key
            if reason:
                await self._revert_warning(reason)
            if getattr(health, "v2_startup_pending", False):
                await self._revert_warning("v2_startup_pending")
            if not getattr(self.trade_stream, "_connected", False):
                await self._revert_warning("provider_disconnected")
            if not self._warning_active:
                await self._revert_warning("monitor_unreadable", diagnostic="provider not ready; A not subscribed")
        except asyncio.CancelledError:
            # Cancellation is shutdown/control flow, never monitor unreadability.
            raise
        except Exception as exc:
            await self._revert_warning("monitor_unreadable", diagnostic=_warning_monitor_diagnostic(
                exc, self._timings.snapshot()))

    async def _warning_monitor_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() and self._warning_revert_reason is None:
            await self._warning_monitor_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.market_data_momentum_warning_monitor_interval_seconds)
            except TimeoutError:
                pass

    def warning_metrics(self, *, event_type: str = "gateway_sample") -> dict:
        if not self._warning_window_open():
            self._pauses.close_window()
        pause = self._pauses.metrics()
        if self._warning_window_open():
            pause.update(state="active" if self._warning_active else "inactive", reason=None)
        observe_errors = getattr(self._warning_health_reader, "error_observations", None)
        log_errors = observe_errors() if observe_errors is not None else {
            "error_lines_by_log": None, "traceback_lines_by_log": None, "last_error_tag": None,
        }
        pause["history"] = pause["history"][-8:]
        pause["exported_pause_history_limit"] = 8
        health_observed_ns = getattr(getattr(self, "_warning_health_sample", None), "observed_wall_ns", None)
        return {
            **log_errors,
            "watchdog_mode": "log_only",
            "watchdog_observed_reason": getattr(self, "_warning_observed_reason", None),
            "last_successful_health_observed_wall_ns": health_observed_ns,
            "health_measurement_age_seconds": (
                (time.time_ns() - health_observed_ns) / 1e9 if health_observed_ns is not None else None
            ),
            "reconnect_count_scope": "LIFETIME_CALLBACK_COUNT_NOT_600S_WINDOW",
            "oms_sync_pass_seconds": getattr(getattr(self, "_warning_health_sample", None), "oms_sync_seconds", None),
            "v2_bar_lateness_seconds": getattr(getattr(self, "_warning_health_sample", None), "v2_bar_lateness_seconds", None),
            "version": 1, "source": SERVICE_NAME, "event_type": event_type,
            "observed_ns": time.time_ns(), "gateway_instance_id": self._warning_instance_id,
            "generation": self._warning_generation, "A_received": self._warning_seq,
            "A_published": self._warning_published, "A_rate_bins": list(self._warning_rate_bins),
            "A_ack_unknown": self._warning_ack_unknown,
            "A_write_await_samples": list(self._warning_write_waits),
            "A_write_await_scope": "BOUNDED_CLIENT_AWAIT_INCLUDES_ENCODING_CONNECTION_SCHEDULING_NOT_SERVER_RTT",
            "read_stage_timings": self._timings.snapshot(),
            "pause_state": pause,
            "observed_monotonic_ns": time.monotonic_ns(),
            "queue_depth": self._warning_queue.qsize(), "queue_drops": self._warning_queue_drop_count,
            "dropped_total": self._warning_drop_count, "lag_ms": self._warning_publish_lag() * 1000,
            "max_publish_lag_ms": self._warning_max_publish_lag_ms,
            "owned_warning_symbols": sorted(self._warning_trade_coverage),
            "warning_trade_coverage": self._warning_trade_coverage,
            "A_subscription_requested_ns": self._warning_requested_utc_ns,
            "A_first_provider_event_ns": self._warning_first_a_utc_ns,
            "A_subscription_confirmed": None,
            "coverage_status": "reverted" if self._warning_revert_reason else (
                "off_by_window" if pause["state"] == "window_closed" else (
                "paused" if pause["state"] == "paused" else (
                "provider_event_observed" if self._warning_first_a_utc_ns else (
                    "requested" if self._warning_active else "inactive"
                ))
            )),
            "watchdog_revert_reason": self._warning_revert_reason,
            "watchdog_revert_ns": self._warning_reverted_utc_ns,
            "v2_cohort": self._warning_watchdog.cohort_metrics(),
        }

    async def _warning_metrics_loop(self, stop_event: asyncio.Event) -> None:
        reported_revert = False
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
                return
            except TimeoutError:
                pass
            try:
                event_type = "revert" if self._warning_revert_reason and not reported_revert else "gateway_sample"
                metrics = self.warning_metrics(event_type=event_type)
                await asyncio.wait_for(
                    self._warning_publisher.publish_momentum_warning_metric(metrics),
                    timeout=1,
                )
                reported_revert |= event_type == "revert"
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._revert_warning("metrics_publish_error")
            else:
                # Incident reporting has its own bounded, coalesced DB worker.
                # It must never turn a display/storage failure into an A trip.
                await self._report_warning_pauses(metrics)

    async def _report_warning_pauses(self, metrics: dict) -> None:
        if not metrics["pause_state"]["alert_reasons"]:
            if self._warning_pause_reporter is not None:
                await self._warning_pause_reporter.observe(metrics, now_ns=time.time_ns())
            return
        try:
            if self._warning_pause_reporter is None:
                self._warning_pause_reporter = await asyncio.to_thread(self._build_warning_pause_reporter)
            await self._warning_pause_reporter.observe(metrics, now_ns=time.time_ns())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.warning("[MOMLATCH1-INCIDENT] reporting_unavailable=1 A_unchanged=1", exc_info=True)

    def _build_warning_pause_reporter(self):
        from sqlalchemy.orm import sessionmaker
        from project_mai_tai.db.session import build_engine
        from project_mai_tai.momwarn_pause_incidents import MomwarnPauseIncidentReporter, PauseIncidentStore

        engine = build_engine(self.settings.database_url, connect_timeout_s=2,
                              statement_timeout_ms=2000, lock_timeout_ms=250, pool_timeout_s=2)
        return MomwarnPauseIncidentReporter(PauseIncidentStore(sessionmaker(bind=engine, expire_on_commit=False)))

    async def _publish_trade_tick_safely(self, trade: TradeTickRecord) -> None:
        try:
            await self.publisher.publish_trade_tick(trade)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("failed to publish trade tick for %s", trade.symbol)

    async def _publish_live_bar_safely(self, bar: LiveBarRecord) -> None:
        try:
            await self.publisher.publish_live_bar(bar)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("failed to publish live bar for %s", bar.symbol)

    async def _publish_quote_tick_safely(self, quote: QuoteTickRecord) -> None:
        try:
            await self.publisher.publish_quote_tick(quote)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("failed to publish quote tick for %s", quote.symbol)

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        interval = max(1, self.settings.service_heartbeat_interval_seconds)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                await self._publish_heartbeat(
                    "healthy",
                    {
                        "reference_tickers": str(self.reference_cache.ticker_count()),
                        "active_symbols": str(len(self._active_symbols)),
                        # DFNS/LGHL incident: a climbing value here means the scanner is blind to
                        # that many symbols. Non-zero is NORMAL (the cache is price-filtered to
                        # $1-10); a sudden RISE is the stale-cache signature worth alerting on.
                        "snapshots_without_reference": str(self._snapshots_without_reference),
                        "last_snapshot_symbols": str(self._last_snapshot_symbol_count),
                    },
                )

    async def _publish_historical_warmup(self, symbols: Iterable[str]) -> None:
        normalized = sorted({symbol.upper() for symbol in symbols if symbol})
        if not normalized or not self.settings.market_data_warmup_enabled:
            return

        for symbol in normalized:
            for interval_secs in WARMUP_INTERVALS:
                try:
                    bars = await asyncio.to_thread(
                        self.snapshot_provider.fetch_historical_bars,
                        symbol,
                        interval_secs=interval_secs,
                        lookback_calendar_days=self.settings.market_data_warmup_lookback_days,
                        limit=self.settings.market_data_warmup_bar_limit,
                    )
                except Exception:
                    self.logger.exception(
                        "historical warmup fetch failed for %s @ %ss",
                        symbol,
                        interval_secs,
                    )
                    continue

                if not bars:
                    continue

                try:
                    await self.publisher.publish_historical_bars(
                        HistoricalBarsRecord(
                            symbol=symbol,
                            interval_secs=interval_secs,
                            bars=tuple(bars),
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception(
                        "historical warmup publish failed for %s @ %ss",
                        symbol,
                        interval_secs,
                    )
