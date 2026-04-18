from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import json
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from project_mai_tai.strategy_core.config import IndicatorConfig

logger = logging.getLogger(__name__)


def _iso_timestamp(value: int | float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(float(value), UTC).isoformat()


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TaapiIndicatorProvider:
    SOURCE = "taapi"
    SUPPORTED_INPUTS = (
        "macd",
        "signal",
        "histogram",
        "ema9",
        "ema20",
        "stoch_k",
        "stoch_d",
        "vwap",
    )
    MISSING_INPUTS = ("extended_vwap",)

    def __init__(
        self,
        secret: str,
        *,
        provider_secret: str,
        provider: str = "polygon",
        base_url: str = "https://us-east.taapi.io",
    ) -> None:
        self.secret = secret
        self.provider_secret = provider_secret
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._cache: dict[tuple[str, int], dict[str, object]] = {}

    def fetch_minute_indicators(
        self,
        symbol: str,
        *,
        bar_time: datetime,
        indicator_config: IndicatorConfig,
    ) -> dict[str, object]:
        normalized_symbol = symbol.upper()
        target_ts = int(bar_time.replace(tzinfo=UTC).timestamp())
        cache_key = (normalized_symbol, target_ts)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        payload: dict[str, object] = {
            "provider_source": "taapi",
            "provider_status": "ready",
            "provider_interval_secs": 60,
            "provider_supported_inputs": list(self.SUPPORTED_INPUTS),
            "provider_missing_inputs": list(self.MISSING_INPUTS),
        }

        body = {
            "secret": self.secret,
            "construct": {
                "type": "stocks",
                "provider": self.provider,
                "providerSecret": self.provider_secret,
                "symbol": normalized_symbol,
                "interval": "1m",
                "indicators": [
                    {"indicator": "macd", "results": 5, "addResultTimestamp": True},
                    {
                        "indicator": "ema",
                        "period": indicator_config.ema1_len,
                        "results": 5,
                        "addResultTimestamp": True,
                    },
                    {
                        "indicator": "ema",
                        "period": indicator_config.ema2_len,
                        "results": 5,
                        "addResultTimestamp": True,
                    },
                    {
                        "indicator": "stoch",
                        "results": 5,
                        "addResultTimestamp": True,
                        "kPeriod": indicator_config.stoch_len,
                        "dPeriod": indicator_config.stoch_smooth_d,
                        "kSmooth": indicator_config.stoch_smooth_k,
                    },
                    {
                        "indicator": "vwap",
                        "results": 5,
                        "addResultTimestamp": True,
                        "anchorPeriod": "session",
                    },
                ],
            },
        }

        try:
            response = self._post_json(f"{self.base_url}/bulk", body)
        except Exception as exc:
            logger.exception("failed to fetch TAAPI indicators for %s", normalized_symbol)
            payload["provider_status"] = "error"
            payload["provider_error"] = str(exc)
            self._cache[cache_key] = dict(payload)
            return payload

        series_map = self._parse_bulk_response(response)
        macd_points = series_map.get("macd", [])
        match_index = self._best_index(macd_points, target_ts)
        if match_index is None:
            payload["provider_status"] = "no_match"
            self._cache[cache_key] = dict(payload)
            return payload

        self._populate_series_values(payload, "macd", macd_points, match_index)
        self._populate_series_values(payload, "ema9", series_map.get("ema9", []), match_index)
        self._populate_series_values(payload, "ema20", series_map.get("ema20", []), match_index)
        self._populate_series_values(payload, "stoch", series_map.get("stoch", []), match_index)
        self._populate_series_values(payload, "vwap", series_map.get("vwap", []), match_index)

        provider_timestamp = payload.get("provider_timestamp")
        payload["provider_last_bar_at"] = _iso_timestamp(provider_timestamp if isinstance(provider_timestamp, (int, float)) else None)

        self._cache[cache_key] = dict(payload)
        return payload

    def _post_json(self, url: str, body: dict[str, object]) -> dict[str, object]:
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "project-mai-tai/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"TAAPI request failed ({exc.code}): {details}") from exc

    def _parse_bulk_response(self, response: dict[str, object]) -> dict[str, list[dict[str, float | int | None]]]:
        data = response.get("data", [])
        if not isinstance(data, list):
            return {}

        parsed: dict[str, list[dict[str, float | int | None]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            indicator = str(item.get("indicator", "")).lower()
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            if indicator == "macd":
                parsed["macd"] = self._zip_points(
                    result.get("timestamp"),
                    value=result.get("valueMACD"),
                    signal=result.get("valueMACDSignal"),
                    histogram=result.get("valueMACDHist"),
                )
            elif indicator == "ema":
                item_id = str(item.get("id", ""))
                key = "ema9" if "_ema_9_" in item_id else "ema20"
                parsed[key] = self._zip_points(result.get("timestamp"), value=result.get("value"))
            elif indicator == "stoch":
                parsed["stoch"] = self._zip_points(
                    result.get("timestamp"),
                    value=result.get("valueK"),
                    signal=result.get("valueD"),
                )
            elif indicator == "vwap":
                parsed["vwap"] = self._zip_points(result.get("timestamp"), value=result.get("value"))
        return parsed

    @staticmethod
    def _ensure_sequence(value: object) -> list[object]:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return list(value)
        if value is None:
            return []
        return [value]

    def _zip_points(self, timestamps: object, **fields: object) -> list[dict[str, float | int | None]]:
        ts_values = self._ensure_sequence(timestamps)
        field_values = {name: self._ensure_sequence(value) for name, value in fields.items()}
        length = min([len(ts_values), *[len(values) for values in field_values.values()]]) if ts_values else 0
        points: list[dict[str, float | int | None]] = []
        for index in range(length):
            point: dict[str, float | int | None] = {"timestamp": int(float(ts_values[index]))}
            for name, values in field_values.items():
                point[name] = _as_float(values[index])
            points.append(point)
        points.sort(key=lambda item: int(item["timestamp"]))
        return points

    @staticmethod
    def _best_index(points: Sequence[dict[str, float | int | None]], target_ts: int) -> int | None:
        best_index: int | None = None
        for index, point in enumerate(points):
            point_ts = int(point.get("timestamp", 0) or 0)
            if point_ts <= target_ts:
                best_index = index
        return best_index

    def _populate_series_values(
        self,
        payload: dict[str, object],
        series_name: str,
        points: Sequence[dict[str, float | int | None]],
        index: int,
    ) -> None:
        if not points:
            return

        current = points[index]
        timestamp = current.get("timestamp")
        if timestamp is not None:
            payload["provider_timestamp"] = int(timestamp)

        if series_name == "macd":
            self._set_point_value(payload, "provider_macd", current, "value")
            self._set_point_value(payload, "provider_signal", current, "signal")
            self._set_point_value(payload, "provider_histogram", current, "histogram")
            self._set_previous_value(payload, "provider_macd_prev", points, index, 1, "value")
            self._set_previous_value(payload, "provider_macd_prev2", points, index, 2, "value")
            self._set_previous_value(payload, "provider_macd_prev3", points, index, 3, "value")
            self._set_previous_value(payload, "provider_signal_prev", points, index, 1, "signal")
            self._set_previous_value(payload, "provider_signal_prev2", points, index, 2, "signal")
            self._set_previous_value(payload, "provider_signal_prev3", points, index, 3, "signal")
            self._set_previous_value(payload, "provider_histogram_prev", points, index, 1, "histogram")
            return

        if series_name == "stoch":
            self._set_point_value(payload, "provider_stoch_k", current, "value")
            self._set_point_value(payload, "provider_stoch_d", current, "signal")
            self._set_previous_value(payload, "provider_stoch_k_prev", points, index, 1, "value")
            self._set_previous_value(payload, "provider_stoch_k_prev2", points, index, 2, "value")
            self._set_previous_value(payload, "provider_stoch_d_prev", points, index, 1, "signal")
            return

        if series_name == "vwap":
            self._set_point_value(payload, "provider_vwap", current, "value")
            self._set_previous_value(payload, "provider_vwap_prev", points, index, 1, "value")
            return

        if series_name in {"ema9", "ema20"}:
            self._set_point_value(payload, f"provider_{series_name}", current, "value")

    @staticmethod
    def _set_point_value(
        payload: dict[str, object],
        key: str,
        point: dict[str, float | int | None],
        field: str,
    ) -> None:
        payload[key] = _as_float(point.get(field))

    @staticmethod
    def _set_previous_value(
        payload: dict[str, object],
        key: str,
        points: Sequence[dict[str, float | int | None]],
        index: int,
        offset: int,
        field: str,
    ) -> None:
        previous_index = index - offset
        payload[key] = None if previous_index < 0 else _as_float(points[previous_index].get(field))
