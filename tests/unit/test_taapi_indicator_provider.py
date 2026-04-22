from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.market_data.taapi_indicator_provider import TaapiIndicatorProvider
from project_mai_tai.strategy_core.config import IndicatorConfig


def test_taapi_provider_uses_polygon_backed_stock_mode() -> None:
    provider = TaapiIndicatorProvider("taapi-secret", provider_secret="polygon-secret")
    captured: dict[str, object] = {}

    def fake_post_json(url: str, body: dict[str, object]) -> dict[str, object]:
        captured["url"] = url
        captured["body"] = body
        return {
            "data": [
                {
                    "indicator": "macd",
                    "result": {
                        "valueMACD": [0.1],
                        "valueMACDSignal": [0.05],
                        "valueMACDHist": [0.05],
                        "timestamp": [int(datetime(2026, 4, 2, 14, 0, tzinfo=UTC).timestamp())],
                    },
                },
                {
                    "id": "stocks_BDRX_1m_ema_9_5_true_0",
                    "indicator": "ema",
                    "result": {
                        "value": [1.02],
                        "timestamp": [int(datetime(2026, 4, 2, 14, 0, tzinfo=UTC).timestamp())],
                    },
                },
                {
                    "id": "stocks_BDRX_1m_ema_20_5_true_0",
                    "indicator": "ema",
                    "result": {
                        "value": [0.98],
                        "timestamp": [int(datetime(2026, 4, 2, 14, 0, tzinfo=UTC).timestamp())],
                    },
                },
                {
                    "indicator": "stoch",
                    "result": {
                        "valueK": [65.0],
                        "valueD": [60.0],
                        "timestamp": [int(datetime(2026, 4, 2, 14, 0, tzinfo=UTC).timestamp())],
                    },
                },
                {
                    "indicator": "vwap",
                    "result": {
                        "value": [1.0],
                        "timestamp": [int(datetime(2026, 4, 2, 14, 0, tzinfo=UTC).timestamp())],
                    },
                },
            ]
        }

    provider._post_json = fake_post_json  # type: ignore[method-assign]

    result = provider.fetch_minute_indicators(
        "BDRX",
        bar_time=datetime(2026, 4, 2, 14, 0, tzinfo=UTC),
        indicator_config=IndicatorConfig(),
    )

    assert captured["url"] == "https://us-east.taapi.io/bulk"
    construct = captured["body"]["construct"]  # type: ignore[index]
    assert construct["type"] == "stocks"
    assert construct["provider"] == "polygon"
    assert construct["providerSecret"] == "polygon-secret"
    assert construct["symbol"] == "BDRX"
    assert construct["interval"] == "1m"
    assert result["provider_status"] == "ready"
    assert result["provider_macd"] == 0.1
    assert result["provider_stoch_k"] == 65.0
    assert result["provider_vwap"] == 1.0
