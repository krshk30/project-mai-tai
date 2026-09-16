from __future__ import annotations

from datetime import UTC, datetime

import pytest

from project_mai_tai.momentum_paper.conditions import build_condition_snapshot


def test_condition_snapshot_fails_closed_on_unknown_and_non_ohlc_conditions() -> None:
    snapshot = build_condition_snapshot(
        [
            {
                "id": 1,
                "name": "regular",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": True,
                        "updates_open_close": True,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 99,
                "name": "cancelled",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": False,
                        "updates_open_close": False,
                        "updates_volume": False,
                    }
                },
            },
        ],
        retrieved_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert snapshot.classify(()) == (True, "no_conditions")
    assert snapshot.classify((1,)) == (True, "aggregate_eligible")
    assert snapshot.classify((99,)) == (False, "not_consolidated_ohlc=99")
    assert snapshot.classify((404,)) == (False, "unknown_conditions=404")


def test_condition_snapshot_refuses_an_empty_reference_response() -> None:
    with pytest.raises(RuntimeError, match="no trade-condition metadata"):
        build_condition_snapshot([], retrieved_at=datetime(2026, 9, 16, tzinfo=UTC))
