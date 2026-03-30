from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from project_mai_tai.events import MarketSnapshotPayload
from project_mai_tai.services.strategy_engine_app import StrategyEngineState, snapshot_from_payload
from project_mai_tai.strategy_core import ReferenceData


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


def test_replay_blacklist_filters_confirmed_watchlists(monkeypatch) -> None:
    fixture_path = Path(__file__).with_name("fixtures") / "scanner_blacklist_replay.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    state = StrategyEngineState(now_provider=fixed_now)
    state.confirmed_scanner._confirmed = list(fixture["confirmed"])
    monkeypatch.setattr(state.alert_engine, "check_alerts", lambda snapshots, reference_data: [])
    monkeypatch.setattr(
        state.confirmed_scanner,
        "process_alerts",
        lambda alerts, reference_data, snapshot_lookup: [],
    )

    summary = state.process_snapshot_batch(
        [
            snapshot_from_payload(MarketSnapshotPayload.model_validate(item))
            for item in fixture["snapshots"]
        ],
        {
            symbol: ReferenceData(
                shares_outstanding=values["shares_outstanding"],
                avg_daily_volume=values["avg_daily_volume"],
            )
            for symbol, values in fixture["reference_data"].items()
        },
        blacklisted_symbols=set(fixture["blacklisted_symbols"]),
    )

    assert summary["watchlist"] == []
    assert summary["top_confirmed"] == []
    assert [item["ticker"] for item in state.confirmed_scanner.get_all_confirmed()] == ["SBET"]
    assert state.bots["macd_30s"].watchlist == set()
    assert state.bots["runner"].watchlist == set()
    assert [item["ticker"] for item in state.confirmed_scanner.get_all_confirmed()] == ["SBET"]
