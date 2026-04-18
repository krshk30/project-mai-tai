from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import logging
from typing import Dict

from project_mai_tai.strategy_core.config import MomentumAlertConfig
from project_mai_tai.strategy_core.models import MarketSnapshot, ReferenceData
from project_mai_tai.strategy_core.snapshot_utils import (
    get_bid_ask,
    get_current_hod,
    get_current_price,
    get_current_volume,
    now_eastern,
)

logger = logging.getLogger(__name__)

HistoryPoint = tuple[float, int]
HistoryEntry = Dict[str, HistoryPoint]


class MomentumAlertEngine:
    def __init__(
        self,
        config: MomentumAlertConfig,
        scan_interval_secs: int = 30,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.scan_interval = scan_interval_secs
        self.now_provider = now_provider or now_eastern

        max_entries = max(1, 600 // scan_interval_secs)
        self._history: deque[HistoryEntry] = deque(maxlen=max_entries)
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._last_spike_volume: dict[str, int] = {}
        self._volume_spike_tickers: set[str] = set()
        self._snaps_per_5min = max(1, 300 // scan_interval_secs)
        self._snaps_per_10min = max(1, 600 // scan_interval_secs)

    def prefill_history(self, entries: Sequence[HistoryEntry | Sequence[MarketSnapshot]]) -> None:
        self._history.clear()
        for entry in entries:
            if isinstance(entry, dict):
                self._history.append(entry)
            else:
                self._history.append(self._build_history_entry(entry))

    def export_state(self) -> dict[str, object]:
        return {
            "history": list(self._history),
            "cooldowns": [
                {
                    "ticker": ticker,
                    "alert_type": alert_type,
                    "last_fired_at": fired_at.astimezone(UTC).isoformat(),
                }
                for (ticker, alert_type), fired_at in self._cooldowns.items()
            ],
            "last_spike_volume": dict(self._last_spike_volume),
            "volume_spike_tickers": sorted(self._volume_spike_tickers),
            "persisted_at": self.now_provider().astimezone(UTC).isoformat(),
        }

    def restore_state(self, payload: Mapping[str, object]) -> bool:
        history = payload.get("history")
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            return False

        restored_history: deque[HistoryEntry] = deque(maxlen=self._history.maxlen)
        for entry in history:
            if not isinstance(entry, dict):
                continue
            normalized_entry: HistoryEntry = {}
            for ticker, values in entry.items():
                if not isinstance(ticker, str):
                    continue
                normalized = self._normalize_history_point(values)
                if normalized is None:
                    continue
                normalized_entry[ticker.upper()] = normalized
            if normalized_entry:
                restored_history.append(normalized_entry)

        self._history = restored_history
        self._cooldowns.clear()

        cooldowns = payload.get("cooldowns")
        if isinstance(cooldowns, Sequence) and not isinstance(cooldowns, (str, bytes)):
            for item in cooldowns:
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker", "")).upper()
                alert_type = str(item.get("alert_type", "")).upper()
                last_fired_at = item.get("last_fired_at")
                if not ticker or not alert_type or not isinstance(last_fired_at, str):
                    continue
                try:
                    fired_at = datetime.fromisoformat(last_fired_at)
                except ValueError:
                    continue
                if fired_at.tzinfo is None:
                    fired_at = fired_at.replace(tzinfo=UTC)
                self._cooldowns[(ticker, alert_type)] = fired_at.astimezone(self.now_provider().tzinfo or UTC)

        last_spike_volume = payload.get("last_spike_volume")
        self._last_spike_volume = {}
        if isinstance(last_spike_volume, dict):
            for ticker, volume in last_spike_volume.items():
                try:
                    normalized_ticker = str(ticker).upper()
                    self._last_spike_volume[normalized_ticker] = int(volume)
                except (TypeError, ValueError):
                    continue

        volume_spike_tickers = payload.get("volume_spike_tickers")
        self._volume_spike_tickers = set()
        if isinstance(volume_spike_tickers, Sequence) and not isinstance(
            volume_spike_tickers,
            (str, bytes),
        ):
            self._volume_spike_tickers = {
                str(ticker).upper() for ticker in volume_spike_tickers if str(ticker).strip()
            }

        logger.info(
            "Momentum alert engine restored | history_cycles=%s spike_tickers=%s cooldowns=%s",
            len(self._history),
            len(self._volume_spike_tickers),
            len(self._cooldowns),
        )
        return len(self._history) > 0

    def record_snapshot(self, snapshots: Sequence[MarketSnapshot]) -> None:
        self._history.append(self._build_history_entry(snapshots))

    def check_alerts(
        self,
        snapshots: Sequence[MarketSnapshot],
        reference_data: Mapping[str, ReferenceData] | None = None,
    ) -> list[dict[str, object]]:
        alerts: list[dict[str, object]] = []
        now = self.now_provider()
        time_str = now.strftime("%I:%M:%S %p ET")
        history_len = len(self._history)

        current: dict[str, dict[str, float | int]] = {}
        snapshot_lookup: dict[str, MarketSnapshot] = {}
        for snapshot in snapshots:
            if not snapshot.ticker:
                continue
            price = get_current_price(snapshot)
            if price is None:
                continue
            current[snapshot.ticker] = {
                "price": price,
                "volume": get_current_volume(snapshot),
                "hod": get_current_hod(snapshot) or price,
            }
            snapshot_lookup[snapshot.ticker] = snapshot

        for ticker, data in current.items():
            price = float(data["price"])
            volume = int(data["volume"])

            if not (self.config.min_price <= price <= self.config.max_price):
                continue

            if volume < self.config.min_momentum_volume:
                continue

            snapshot = snapshot_lookup[ticker]
            bid_ask = get_bid_ask(snapshot)
            ref = reference_data.get(ticker) if reference_data else None
            float_shares = ref.shares_outstanding if ref else 0

            base: dict[str, object] = {
                "ticker": ticker,
                "price": price,
                "bid": bid_ask.get("bid", 0),
                "ask": bid_ask.get("ask", 0),
                "bid_size": bid_ask.get("bid_size", 0),
                "ask_size": bid_ask.get("ask_size", 0),
                "volume": volume,
                "float": float_shares,
                "time": time_str,
            }

            old_5min = None
            vol_5min = 0
            expected_5min = 0.0
            relative_spike = False
            absolute_spike = False
            if history_len >= self._snaps_per_5min and reference_data is not None:
                old_5min = self._history[-self._snaps_per_5min].get(ticker)
                if old_5min and ref:
                    _old_price, old_volume = old_5min
                    vol_5min = volume - old_volume
                    expected_5min = ref.avg_daily_volume / 78.0
                    relative_spike = (
                        expected_5min > 0
                        and vol_5min >= expected_5min * self.config.volume_spike_mult
                    )
                    absolute_spike = vol_5min >= 50_000

            squeeze_5min_pct = None
            if old_5min and old_5min[0] > 0:
                old_price, _old_volume = old_5min
                squeeze_5min_pct = (price - old_price) / old_price * 100

            old_10min = None
            squeeze_10min_pct = None
            if history_len >= self._snaps_per_10min:
                old_10min = self._history[-self._snaps_per_10min].get(ticker)
                if old_10min and old_10min[0] > 0:
                    old_price, _old_volume = old_10min
                    squeeze_10min_pct = (price - old_price) / old_price * 100

            late_catchup_seed = (
                (relative_spike or absolute_spike)
                and ticker not in self._volume_spike_tickers
                and not self._on_cooldown(ticker, "VOLUME_SPIKE", now)
                and (
                    (squeeze_5min_pct is not None and squeeze_5min_pct >= self.config.squeeze_5min_pct)
                    or (squeeze_10min_pct is not None and squeeze_10min_pct >= self.config.squeeze_10min_pct)
                )
            )

            emitted_volume_spike = False
            if relative_spike or absolute_spike:
                last_spike_volume = self._last_spike_volume.get(ticker, 0)
                should_fire = False
                if last_spike_volume == 0:
                    should_fire = True
                elif volume >= last_spike_volume * 2:
                    should_fire = True
                elif late_catchup_seed:
                    # If we missed the earlier seed, allow a later explosive move to
                    # backfill the spike so squeeze alerts can still be emitted.
                    should_fire = True

                if should_fire and not self._on_cooldown(ticker, "VOLUME_SPIKE", now):
                    spike_type = "relative" if relative_spike else "absolute"
                    details = {
                        "vol_5min": int(vol_5min),
                        "expected_5min": int(expected_5min),
                        "spike_mult": round(vol_5min / max(expected_5min, 1), 1),
                        "total_vol": volume,
                        "spike_type": spike_type,
                    }
                    if late_catchup_seed:
                        details["catchup_seed"] = True
                    self._last_spike_volume[ticker] = volume
                    self._volume_spike_tickers.add(ticker)
                    emitted_volume_spike = True
                    alerts.append(
                        {
                            **base,
                            "type": "VOLUME_SPIKE",
                            "details": details,
                        }
                    )
                    self._set_cooldown(ticker, "VOLUME_SPIKE", now)

            volume_gate_open = ticker in self._volume_spike_tickers or emitted_volume_spike

            if volume_gate_open and squeeze_5min_pct is not None and old_5min:
                old_price, _old_volume = old_5min
                if squeeze_5min_pct >= self.config.squeeze_5min_pct and not self._on_cooldown(
                    ticker,
                    "SQUEEZE_5MIN",
                    now,
                ):
                    alerts.append(
                        {
                            **base,
                            "type": "SQUEEZE_5MIN",
                            "details": {
                                "change_pct": round(squeeze_5min_pct, 1),
                                "price_5min_ago": round(old_price, 3),
                            },
                        }
                    )
                    self._set_cooldown(ticker, "SQUEEZE_5MIN", now)

            if volume_gate_open and squeeze_10min_pct is not None and old_10min:
                old_price, _old_volume = old_10min
                if squeeze_10min_pct >= self.config.squeeze_10min_pct and not self._on_cooldown(
                    ticker,
                    "SQUEEZE_10MIN",
                    now,
                ):
                    alerts.append(
                        {
                            **base,
                            "type": "SQUEEZE_10MIN",
                            "details": {
                                "change_pct": round(squeeze_10min_pct, 1),
                                "price_10min_ago": round(old_price, 3),
                            },
                        }
                    )
                    self._set_cooldown(ticker, "SQUEEZE_10MIN", now)

        for alert in alerts:
            logger.info("[ALERT] [%s] %s @ $%.2f | %s", alert["type"], alert["ticker"], alert["price"], alert["details"])

        if self._cycle_count % 60 == 0:
            self._cleanup_stale_cooldowns(now)

        return alerts

    def get_warmup_status(self) -> dict[str, int | bool]:
        history_len = len(self._history)
        return {
            "history_cycles": history_len,
            "squeeze_5min_ready": history_len >= self._snaps_per_5min,
            "squeeze_5min_needs": self._snaps_per_5min,
            "squeeze_5min_eta_secs": max(0, (self._snaps_per_5min - history_len) * self.scan_interval),
            "squeeze_10min_ready": history_len >= self._snaps_per_10min,
            "squeeze_10min_needs": self._snaps_per_10min,
            "squeeze_10min_eta_secs": max(0, (self._snaps_per_10min - history_len) * self.scan_interval),
            "fully_ready": history_len >= self._snaps_per_10min,
        }

    def reset(self) -> None:
        self._history.clear()
        self._cooldowns.clear()
        self._last_spike_volume.clear()
        self._volume_spike_tickers.clear()
        logger.info("Momentum alert engine reset")

    @property
    def _cycle_count(self) -> int:
        return len(self._history)

    def _build_history_entry(self, snapshots: Sequence[MarketSnapshot]) -> HistoryEntry:
        entry: HistoryEntry = {}
        for snapshot in snapshots:
            if not snapshot.ticker:
                continue
            price = get_current_price(snapshot)
            if price is None:
                continue
            if not (self.config.min_price <= price <= self.config.max_price):
                continue
            entry[snapshot.ticker] = (
                float(price),
                int(get_current_volume(snapshot)),
            )
        return entry

    @staticmethod
    def _normalize_history_point(value: object) -> HistoryPoint | None:
        if isinstance(value, dict):
            price = value.get("price")
            volume = value.get("volume")
            if price is None or volume is None:
                return None
            try:
                return (float(price), int(volume))
            except (TypeError, ValueError):
                return None

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
            try:
                return (float(value[0]), int(value[1]))
            except (TypeError, ValueError):
                return None

        return None

    def _on_cooldown(self, ticker: str, alert_type: str, now: datetime) -> bool:
        last_fired = self._cooldowns.get((ticker, alert_type))
        if last_fired is None:
            return False
        elapsed = (now - last_fired).total_seconds() / 60
        return elapsed < self.config.alert_cooldown_mins

    def _set_cooldown(self, ticker: str, alert_type: str, now: datetime) -> None:
        self._cooldowns[(ticker, alert_type)] = now

    def _cleanup_stale_cooldowns(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=self.config.alert_cooldown_mins + 1)
        self._cooldowns = {key: value for key, value in self._cooldowns.items() if value > cutoff}
