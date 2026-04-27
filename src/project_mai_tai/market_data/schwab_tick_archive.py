from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.models import HistoricalBarRecord, QuoteTickRecord, TradeTickRecord

EASTERN_TZ = ZoneInfo("America/New_York")


class SchwabTickArchive:
    """Append raw Schwab stream events into per-symbol ET-day JSONL files."""

    def __init__(self, root_path: str | Path) -> None:
        self.root_path = Path(root_path)
        self._handles: dict[Path, TextIO] = {}

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

    def _append(self, *, day: str, symbol: str, payload: dict[str, object]) -> Path:
        normalized_symbol = str(symbol).upper()
        path = self.root_path / day / f"{normalized_symbol}.jsonl"
        handle = self._handles.get(path)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", buffering=1)
            self._handles[path] = handle
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
