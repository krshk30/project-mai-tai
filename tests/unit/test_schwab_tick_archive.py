from __future__ import annotations

import json

from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.schwab_tick_archive import SchwabTickArchive


def _read_jsonl(path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_archive_writes_quote_trade_and_subscription_events(tmp_path) -> None:
    archive = SchwabTickArchive(tmp_path)

    quote_path = archive.record_quote(
        QuoteTickRecord(symbol="EFOI", bid_price=6.10, ask_price=6.12, bid_size=100, ask_size=200),
        recorded_at_ns=1_776_496_200_000_000_000,
    )
    trade_path = archive.record_trade(
        TradeTickRecord(
            symbol="EFOI",
            price=6.1005,
            size=3,
            timestamp_ns=1_776_470_399_366_000_000,
            cumulative_volume=167_170_015,
        ),
        recorded_at_ns=1_776_496_200_100_000_000,
    )
    control_path = archive.record_subscription_snapshot(
        ["EFOI", "MSTP"],
        recorded_at_ns=1_776_496_200_200_000_000,
    )
    archive.close()

    assert quote_path.name == "EFOI.jsonl"
    assert trade_path == quote_path
    assert control_path.name == "__CONTROL__.jsonl"

    symbol_rows = _read_jsonl(quote_path)
    assert [row["event_type"] for row in symbol_rows] == ["quote", "trade"]
    assert symbol_rows[1]["cumulative_volume"] == 167170015

    control_rows = _read_jsonl(control_path)
    assert control_rows == [
        {
            "event_type": "subscription_sync",
            "recorded_at_ns": 1776496200200000000,
            "symbols": ["EFOI", "MSTP"],
        }
    ]
