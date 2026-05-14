from __future__ import annotations

import json
import time
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.models import (
    HistoricalBarRecord,
    LiveBarRecord,
    QuoteTickRecord,
    TradeTickRecord,
)

EASTERN_TZ = ZoneInfo("America/New_York")

# Default cap on cached append-mode handles. Set well below the typical Linux
# soft FD limit (1024) and well above the typical watchlist size (~30 symbols
# per bot × 3 active bots ≈ 90). Production crash on 2026-05-14 hit `EMFILE`
# after ~6h of operation because the cache grew unbounded as symbols rotated
# in and out of watchlists; LRU eviction prevents that.
DEFAULT_MAX_HANDLES = 256


class SchwabTickArchive:
    """Append raw Schwab stream events into per-symbol ET-day JSONL files.

    Caches file handles keyed by absolute path, bounded by ``max_handles`` to
    avoid file-descriptor exhaustion. Eviction is least-recently-used: every
    successful append marks its handle MRU; when the cache exceeds the cap,
    the eldest handle is closed before the new one is admitted.
    """

    def __init__(
        self,
        root_path: str | Path,
        *,
        max_handles: int = DEFAULT_MAX_HANDLES,
    ) -> None:
        self.root_path = Path(root_path)
        self.max_handles = max(1, int(max_handles))
        self._handles: OrderedDict[Path, TextIO] = OrderedDict()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def record_quote(self, record: QuoteTickRecord, *, recorded_at_ns: int | None = None) -> Path:
        stored_at_ns = int(recorded_at_ns or time.time_ns())
        payload = asdict(record)
        payload["event_type"] = "quote"
        payload["recorded_at_ns"] = stored_at_ns
        day = self._session_day_from_ns(stored_at_ns)
        return self._append(day=day, symbol=record.symbol, payload=payload)

    def record_trade(self, record: TradeTickRecord, *, recorded_at_ns: int | None = None) -> Path:
        stored_at_ns = int(recorded_at_ns or time.time_ns())
        payload = asdict(record)
        payload["event_type"] = "trade"
        payload["recorded_at_ns"] = stored_at_ns
        payload["conditions"] = list(record.conditions)
        day = self._session_day_from_ns(stored_at_ns)
        return self._append(day=day, symbol=record.symbol, payload=payload)

    def record_live_bar(self, record: LiveBarRecord, *, recorded_at_ns: int | None = None) -> Path:
        stored_at_ns = int(recorded_at_ns or time.time_ns())
        payload = asdict(record)
        payload["event_type"] = "live_bar"
        payload["recorded_at_ns"] = stored_at_ns
        day = self._session_day_from_epoch(float(record.timestamp))
        return self._append(day=day, symbol=record.symbol, payload=payload)

    def record_subscription_snapshot(
        self,
        symbols: Sequence[str],
        *,
        recorded_at_ns: int | None = None,
    ) -> Path:
        stored_at_ns = int(recorded_at_ns or time.time_ns())
        payload = {
            "event_type": "subscription_sync",
            "recorded_at_ns": stored_at_ns,
            "symbols": sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()}),
        }
        day = self._session_day_from_ns(stored_at_ns)
        return self._append(day=day, symbol="__control__", payload=payload)

    def _session_day_from_ns(self, value_ns: int) -> str:
        dt = datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC).astimezone(EASTERN_TZ)
        return dt.strftime("%Y-%m-%d")

    def _session_day_from_epoch(self, value: float) -> str:
        dt = datetime.fromtimestamp(value, tz=UTC).astimezone(EASTERN_TZ)
        return dt.strftime("%Y-%m-%d")

    def _append(self, *, day: str, symbol: str, payload: dict[str, object]) -> Path:
        normalized_symbol = str(symbol).upper()
        path = self.root_path / day / f"{normalized_symbol}.jsonl"
        handle = self._handles.get(path)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", buffering=1)
            self._handles[path] = handle
            while len(self._handles) > self.max_handles:
                _evicted_path, evicted_handle = self._handles.popitem(last=False)
                try:
                    evicted_handle.close()
                except OSError:
                    pass
        else:
            self._handles.move_to_end(path)
        handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        handle.write("\n")
        return path


def load_aggregated_trade_bars(
    root_path: str | Path,
    *,
    symbol: str,
    day: str,
    interval_secs: int,
    start_at_ns: int | None = None,
    end_at_ns: int | None = None,
) -> tuple[HistoricalBarRecord, ...]:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ()

    path = Path(root_path) / day / f"{normalized_symbol}.jsonl"
    if not path.exists():
        return ()

    buckets: dict[int, dict[str, float | int]] = defaultdict(dict)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("event_type", "")).lower() != "trade":
                    continue
                timestamp_ns = payload.get("timestamp_ns") or payload.get("recorded_at_ns")
                try:
                    event_ns = int(timestamp_ns)
                except (TypeError, ValueError):
                    continue
                if start_at_ns is not None and event_ns < start_at_ns:
                    continue
                if end_at_ns is not None and event_ns > end_at_ns:
                    continue
                try:
                    price = float(payload.get("price", 0) or 0)
                    size = int(payload.get("size", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if price <= 0 or size <= 0:
                    continue
                bucket_start_ns = (event_ns // (interval_secs * 1_000_000_000)) * (
                    interval_secs * 1_000_000_000
                )
                bucket = buckets[bucket_start_ns]
                if not bucket:
                    bucket["open"] = price
                    bucket["high"] = price
                    bucket["low"] = price
                    bucket["close"] = price
                    bucket["volume"] = size
                    bucket["trade_count"] = 1
                    continue
                bucket["high"] = max(float(bucket["high"]), price)
                bucket["low"] = min(float(bucket["low"]), price)
                bucket["close"] = price
                bucket["volume"] = int(bucket["volume"]) + size
                bucket["trade_count"] = int(bucket["trade_count"]) + 1
    except OSError:
        return ()

    records: list[HistoricalBarRecord] = []
    for bucket_start_ns in sorted(buckets):
        bucket = buckets[bucket_start_ns]
        records.append(
            HistoricalBarRecord(
                open=float(bucket["open"]),
                high=float(bucket["high"]),
                low=float(bucket["low"]),
                close=float(bucket["close"]),
                volume=int(bucket["volume"]),
                timestamp=bucket_start_ns / 1_000_000_000,
                trade_count=int(bucket["trade_count"]),
            )
        )
    return tuple(records)


def load_recorded_trades(
    root_path: str | Path,
    *,
    symbol: str,
    day: str,
    start_at_ns: int | None = None,
    end_at_ns: int | None = None,
) -> tuple[TradeTickRecord, ...]:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ()

    path = Path(root_path) / day / f"{normalized_symbol}.jsonl"
    if not path.exists():
        return ()

    records: list[TradeTickRecord] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("event_type", "")).lower() != "trade":
                    continue
                timestamp_ns = payload.get("timestamp_ns") or payload.get("recorded_at_ns")
                try:
                    event_ns = int(timestamp_ns)
                    price = float(payload.get("price", 0) or 0)
                    size = int(payload.get("size", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if start_at_ns is not None and event_ns < start_at_ns:
                    continue
                if end_at_ns is not None and event_ns >= end_at_ns:
                    continue
                if price <= 0 or size <= 0:
                    continue
                conditions = payload.get("conditions", ())
                if not isinstance(conditions, list | tuple):
                    conditions = ()
                records.append(
                    TradeTickRecord(
                        symbol=normalized_symbol,
                        price=price,
                        size=size,
                        timestamp_ns=event_ns,
                        cumulative_volume=(
                            int(payload["cumulative_volume"])
                            if payload.get("cumulative_volume") is not None
                            else None
                        ),
                        exchange=str(payload.get("exchange")) if payload.get("exchange") is not None else None,
                        conditions=tuple(str(condition) for condition in conditions),
                    )
                )
    except OSError:
        return ()

    records.sort(key=lambda record: int(record.timestamp_ns or 0))
    return tuple(records)


def load_recorded_live_bars(
    root_path: str | Path,
    *,
    symbol: str,
    day: str,
    interval_secs: int,
    start_at: float | None = None,
    end_at: float | None = None,
) -> tuple[HistoricalBarRecord, ...]:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ()

    path = Path(root_path) / day / f"{normalized_symbol}.jsonl"
    if not path.exists():
        return ()

    records: list[HistoricalBarRecord] = []
    seen_timestamps: set[float] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("event_type", "")).lower() != "live_bar":
                    continue
                try:
                    payload_interval_secs = int(payload.get("interval_secs", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if payload_interval_secs != int(interval_secs):
                    continue
                try:
                    timestamp = float(payload.get("timestamp", 0) or 0)
                    open_price = float(payload.get("open", 0) or 0)
                    high_price = float(payload.get("high", 0) or 0)
                    low_price = float(payload.get("low", 0) or 0)
                    close_price = float(payload.get("close", 0) or 0)
                    volume = int(payload.get("volume", 0) or 0)
                    trade_count = int(payload.get("trade_count", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if timestamp <= 0 or close_price <= 0:
                    continue
                if start_at is not None and timestamp < start_at:
                    continue
                if end_at is not None and timestamp > end_at:
                    continue
                if timestamp in seen_timestamps:
                    continue
                seen_timestamps.add(timestamp)
                records.append(
                    HistoricalBarRecord(
                        open=open_price,
                        high=high_price,
                        low=low_price,
                        close=close_price,
                        volume=volume,
                        timestamp=timestamp,
                        trade_count=max(0, trade_count),
                    )
                )
    except OSError:
        return ()

    records.sort(key=lambda bar: bar.timestamp)
    return tuple(records)
