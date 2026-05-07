"""Smoke tests for the LEVELONE/CHART/TIMESALE multi-service streamer."""
from __future__ import annotations

from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient


def test_levelone_trade_extraction_unchanged() -> None:
    payload = {
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [{"key": "ABC", "3": 10.5, "8": 1000, "9": 100, "35": 1700000000000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(trades) == 1
    assert trades[0].symbol == "ABC"
    assert trades[0].price == 10.5
    assert trades[0].cumulative_volume == 1000
    assert bars == []


def test_timesale_trade_extraction() -> None:
    payload = {
        "data": [
            {
                "service": "TIMESALE_EQUITY",
                "content": [{"key": "ABC", "1": 1700000000000, "2": 11.25, "3": 50, "4": 999}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(trades) == 1
    assert trades[0].symbol == "ABC"
    assert trades[0].price == 11.25
    assert trades[0].size == 50
    # TIMESALE record has no cumulative_volume — bar builder should fall back to size
    assert trades[0].cumulative_volume is None


def test_levelone_trade_dedupe_when_symbol_subscribed_to_timesale() -> None:
    """When a symbol is subscribed to TIMESALE_EQUITY, LEVELONE trades for that
    symbol must be suppressed to avoid double-counting volume."""
    payload = {
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [{"key": "ABC", "3": 10.5, "8": 1000, "9": 100, "35": 1700000000000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(
        payload,
        timesale_symbols={"ABC"},
    )
    # Quote should still be extracted (so spreads stay live), but trade is suppressed.
    assert trades == []
    # Quote needs valid bid + ask — payload above only has trade fields, so quotes is also empty.
    # That's fine; the assertion above is the dedupe contract.


def test_chart_equity_bar_extraction() -> None:
    payload = {
        "data": [
            {
                "service": "CHART_EQUITY",
                "content": [{"key": "ABC", "2": 10.0, "3": 10.5, "4": 9.9, "5": 10.2, "6": 5000, "7": 1700000060000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(bars) == 1
    assert bars[0].symbol == "ABC"
    assert bars[0].open == 10.0
    assert bars[0].close == 10.2
    assert bars[0].volume == 5000
    assert bars[0].interval_secs == 60
