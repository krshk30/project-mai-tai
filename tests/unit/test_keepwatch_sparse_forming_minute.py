"""KEEPWATCH1 sparse history: Schwab's still-forming next minute is not a closed candle (ELPW 10-28)."""
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings
from tests.unit.test_line_chart_restoration_integration import _ms


ANCHOR = _ms("2026-10-28T04:00:00-04:00")
CLOSED = _ms("2026-10-28T07:06:00-04:00")


def _candle(stamp):
    return {"datetime": stamp, "open": 2.7, "high": 2.8, "low": 2.6, "close": 2.75, "volume": 100}


def _parse(candles):
    client = SchwabV2RestClient(Settings(), on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    return client._parse_session_history(
        "ELPW", ANCHOR, CLOSED, {"symbol": "ELPW", "empty": False, "candles": candles},
        allow_sparse=True)


def test_forming_next_minute_is_dropped_and_proof_covers_only_closed_bars():
    closed = [_candle(ANCHOR + 27 * 60_000), _candle(CLOSED)]
    bars, proof = _parse(closed + [_candle(CLOSED + 60_000)])
    expected_bars, expected_proof = _parse(closed)
    assert [bar.timestamp_ms for bar in bars] == [ANCHOR + 27 * 60_000, CLOSED]
    assert (bars, proof) == (expected_bars, expected_proof)
    assert proof.end_ms == CLOSED + 60_000


def test_sparse_with_only_the_forming_minute_is_a_closed_empty_prefix():
    bars, proof = _parse([_candle(ANCHOR + 5 * 60_000), _candle(CLOSED + 60_000)])
    assert [bar.timestamp_ms for bar in bars] == [ANCHOR + 5 * 60_000]
    assert proof is not None and proof.closed_ids == (ANCHOR + 5 * 60_000,)


@pytest.mark.parametrize("ahead", [2, 3, 30])
def test_anything_beyond_the_forming_minute_still_refuses(ahead):
    with pytest.raises(ValueError, match="exceeds query window"):
        _parse([_candle(CLOSED), _candle(CLOSED + ahead * 60_000)])
