"""[codex] Provider over-return never extends the requested closed prefix."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings
from tests.unit.test_line_chart_restoration_integration import _ms


ANCHOR = _ms("2026-10-09T04:00:00-04:00")
CLOSED = _ms("2026-10-09T07:00:00-04:00")


def _candle(stamp):
    return {"datetime": stamp, "open": 2, "high": 2.1, "low": 1.9,
            "close": 2, "volume": 100}


def _parse(candles, **overrides):
    client = SchwabV2RestClient(Settings(), on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    payload = {"symbol": "MI", "empty": False, "candles": candles, **overrides}
    return client._parse_session_history("MI", ANCHOR, CLOSED, payload)


@pytest.mark.parametrize("symbol", ["MI", "VEEA"])
@pytest.mark.parametrize("extra", [1, 2, 3, 4, 5, 21])
def test_open_response_overreturn_matches_requested_prefix(symbol, extra):
    client = SchwabV2RestClient(Settings(), on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    prefix = [_candle(CLOSED)]
    payload = {"symbol": symbol, "empty": False,
               "candles": prefix + [_candle(CLOSED + index * 60_000)
                                     for index in range(1, extra + 1)]}
    before = deepcopy(payload)
    actual = client._parse_session_history(symbol, ANCHOR, CLOSED, payload)
    expected = client._parse_session_history(
        symbol, ANCHOR, CLOSED, {**payload, "candles": prefix},
    )
    assert actual == expected
    assert payload == before
    bars, proof = actual
    assert [bar.timestamp_ms for bar in bars] == [CLOSED]
    assert proof.end_ms == CLOSED + 60_000


def test_only_later_candles_do_not_certify_current_close():
    bars, proof = _parse([_candle(CLOSED + 60_000)])
    assert bars == [] and proof is None


def test_missing_current_with_later_candle_still_has_no_proof():
    bars, proof = _parse([_candle(CLOSED - 60_000), _candle(CLOSED + 60_000)])
    assert [bar.timestamp_ms for bar in bars] == [CLOSED - 60_000]
    assert proof is None


@pytest.mark.parametrize("symbol", ["MI", "VEEA", "AIXI", "DKI", "FLYE"])
def test_fresh_session_read_preserves_exact_closed_prefix_without_mutating_source(symbol):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" /
                          "line_repair_cutoff_1009_fresh.json").read_text())
    row = next(row for row in fixture["rows"] if row["symbol"] == symbol)
    client = SchwabV2RestClient(Settings(), on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    payload = row["schwab"]
    before = deepcopy(payload)
    cutoff = row["cutoff_ms"] - 11 * 60_000
    actual = client._parse_session_history(symbol, row["anchor_ms"], cutoff, payload)
    prefix = {**payload, "candles": [bar for bar in payload["candles"]
                                     if bar["datetime"] <= cutoff]}
    if not prefix["candles"]:
        prefix["empty"] = True
    expected = client._parse_session_history(symbol, row["anchor_ms"], cutoff, prefix)
    assert actual == expected
    assert payload == before


@pytest.mark.parametrize("damage", ["duplicate", "pre_anchor", "malformed", "ohlc",
                                    "symbol", "empty", "pagination", "oversize"])
def test_other_parser_guards_still_reject_with_overreturn(damage):
    candles = [_candle(CLOSED), _candle(CLOSED + 60_000)]
    overrides = {}
    if damage == "duplicate":
        candles.insert(0, _candle(CLOSED))
    elif damage == "pre_anchor":
        candles.insert(0, _candle(ANCHOR - 60_000))
    elif damage == "malformed":
        candles[0].pop("high")
    elif damage == "ohlc":
        candles[0]["low"] = 3
    elif damage == "symbol":
        overrides["symbol"] = "OTHER"
    elif damage == "empty":
        overrides["empty"] = True
    elif damage == "pagination":
        overrides["nextToken"] = "more"
    elif damage == "oversize":
        candles = [_candle(CLOSED)] * 961
    with pytest.raises((ValueError, KeyError)):
        _parse(candles, **overrides)
