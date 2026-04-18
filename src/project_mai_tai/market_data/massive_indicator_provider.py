from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging

from project_mai_tai.strategy_core.config import IndicatorConfig

logger = logging.getLogger(__name__)


def _timestamp_seconds(value: int | float | None) -> float | None:
    if value is None:
        return None
    raw = float(value)
    if raw > 1_000_000_000_000:
        return raw / 1000.0
    return raw


def _iso_timestamp(value: int | float | None) -> str:
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, UTC).isoformat()


class MassiveIndicatorProvider:
    SOURCE = "massive"
    SUPPORTED_INPUTS = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "macd",
        "signal",
        "histogram",
        "ema9",
        "ema20",
    )
    MISSING_INPUTS = ("stoch_k", "stoch_d", "extended_vwap")

    def __init__(
        self,
        api_key: str,
        *,
        client_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.api_key = api_key
        self._client_factory = client_factory or self._default_client_factory
        self._client = None
        self._cache: dict[tuple[str, int, str], dict[str, object]] = {}

    def fetch_minute_indicators(
        self,
        symbol: str,
        *,
        bar_time: datetime,
        indicator_config: IndicatorConfig,
    ) -> dict[str, object]:
        normalized_symbol = symbol.upper()
        cache_key = (normalized_symbol, int(bar_time.timestamp()), "minute")
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        payload = self._base_payload(
            interval_secs=60,
            supported_inputs=("macd", "signal", "histogram", "ema9", "ema20"),
            missing_inputs=("stoch_k", "stoch_d", "vwap", "extended_vwap"),
        )

        try:
            client = self._get_client()
            macd_result = client.get_macd(
                normalized_symbol,
                timestamp_lte=bar_time,
                timespan="minute",
                short_window=indicator_config.macd_fast,
                long_window=indicator_config.macd_slow,
                signal_window=indicator_config.macd_signal,
                adjusted=True,
                order="desc",
                limit=1,
                series_type="close",
            )
            ema9_result = client.get_ema(
                normalized_symbol,
                timestamp_lte=bar_time,
                timespan="minute",
                window=indicator_config.ema1_len,
                adjusted=True,
                order="desc",
                limit=1,
                series_type="close",
            )
            ema20_result = client.get_ema(
                normalized_symbol,
                timestamp_lte=bar_time,
                timespan="minute",
                window=indicator_config.ema2_len,
                adjusted=True,
                order="desc",
                limit=1,
                series_type="close",
            )
        except Exception as exc:
            logger.exception("failed to fetch Massive indicators for %s", normalized_symbol)
            payload["provider_status"] = "error"
            payload["provider_error"] = str(exc)
            self._cache[cache_key] = dict(payload)
            return payload

        macd_value = self._latest_value(macd_result)
        ema9_value = self._latest_value(ema9_result)
        ema20_value = self._latest_value(ema20_result)

        payload["provider_macd"] = self._as_float(getattr(macd_value, "value", None))
        payload["provider_signal"] = self._as_float(getattr(macd_value, "signal", None))
        payload["provider_histogram"] = self._as_float(getattr(macd_value, "histogram", None))
        payload["provider_ema9"] = self._as_float(getattr(ema9_value, "value", None))
        payload["provider_ema20"] = self._as_float(getattr(ema20_value, "value", None))

        provider_timestamp = (
            getattr(macd_value, "timestamp", None)
            or getattr(ema9_value, "timestamp", None)
            or getattr(ema20_value, "timestamp", None)
        )
        payload["provider_timestamp"] = _timestamp_seconds(provider_timestamp)
        payload["provider_last_bar_at"] = _iso_timestamp(provider_timestamp)

        self._cache[cache_key] = dict(payload)
        return payload

    def fetch_aggregate_overlay(
        self,
        symbol: str,
        *,
        bar_time: datetime,
        interval_secs: int,
    ) -> dict[str, object]:
        normalized_symbol = symbol.upper()
        cache_key = (normalized_symbol, int(bar_time.timestamp()), f"agg_{interval_secs}")
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        payload = self._base_payload(
            interval_secs=interval_secs,
            supported_inputs=("open", "high", "low", "close", "volume", "vwap"),
            missing_inputs=("macd", "signal", "histogram", "ema9", "ema20", "stoch_k", "stoch_d", "extended_vwap"),
        )

        try:
            client = self._get_client()
            aggs = client.get_aggs(
                normalized_symbol,
                interval_secs if interval_secs < 60 else interval_secs // 60,
                "second" if interval_secs < 60 else "minute",
                from_=bar_time,
                to=bar_time + timedelta(seconds=max(1, interval_secs) - 1),
                adjusted=True,
                sort="desc",
                limit=1,
            )
        except Exception as exc:
            logger.exception("failed to fetch Massive aggregate overlay for %s", normalized_symbol)
            payload["provider_status"] = "error"
            payload["provider_error"] = str(exc)
            self._cache[cache_key] = dict(payload)
            return payload

        latest_agg = aggs[0] if aggs else None
        if latest_agg is None:
            payload["provider_status"] = "no_match"
            self._cache[cache_key] = dict(payload)
            return payload

        payload["provider_open"] = self._as_float(getattr(latest_agg, "open", None))
        payload["provider_high"] = self._as_float(getattr(latest_agg, "high", None))
        payload["provider_low"] = self._as_float(getattr(latest_agg, "low", None))
        payload["provider_close"] = self._as_float(getattr(latest_agg, "close", None))
        payload["provider_volume"] = self._as_float(getattr(latest_agg, "volume", None))
        payload["provider_vwap"] = self._as_float(getattr(latest_agg, "vwap", None))

        provider_timestamp = getattr(latest_agg, "timestamp", None)
        payload["provider_timestamp"] = _timestamp_seconds(provider_timestamp)
        payload["provider_last_bar_at"] = _iso_timestamp(provider_timestamp)

        self._cache[cache_key] = dict(payload)
        return payload

    def _get_client(self):
        if self._client is None:
            self._client = self._client_factory(self.api_key)
        return self._client

    @staticmethod
    def _default_client_factory(api_key: str):
        try:
            from massive import RESTClient
        except ImportError as exc:
            raise RuntimeError("The 'massive' package is required for indicator comparison.") from exc
        return RESTClient(api_key=api_key)

    @staticmethod
    def _base_payload(
        *,
        interval_secs: int,
        supported_inputs: tuple[str, ...],
        missing_inputs: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "provider_source": "massive",
            "provider_status": "ready",
            "provider_interval_secs": interval_secs,
            "provider_supported_inputs": list(supported_inputs),
            "provider_missing_inputs": list(missing_inputs),
        }

    @staticmethod
    def _latest_value(result):
        values = getattr(result, "values", None) or []
        return values[0] if values else None

    @staticmethod
    def _as_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
