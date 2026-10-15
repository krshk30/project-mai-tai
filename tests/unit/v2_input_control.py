"""Preserve candle scalars while supplying the caller's explicit raw identity."""
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar


def chart_input(symbol, bar):
    return ChartBar(symbol, bar.open, bar.high, bar.low, bar.close,
                    bar.volume, bar.timestamp_ms)
