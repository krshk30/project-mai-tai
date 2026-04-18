from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord

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
