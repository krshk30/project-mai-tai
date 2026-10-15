"""W3 v2-only composition: retained values, controlled calls, no provider I/O."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.symbol_case import raw_symbol_is_uppercase

CASE = json.loads((Path(__file__).parents[1]/"fixtures/tickcase1_native_pairs.json").read_text())
ROWS = {row["symbol"]: row for row in CASE["pair_snapshots"]}
STAMP = int(datetime.fromisoformat(CASE["produced_at"].replace("Z", "+00:00")).timestamp()*1000)
PAIRS = [("TpC", "TPC"), ("BCpC", "BCPC")]


@pytest.mark.parametrize("raw,allowed", [
    ("TPC", True), ("TpC", False), ("BCPC", True), ("BCpC", False),
    ("BRK.B", True), ("BRK.b", False), ("XYZpA", False), ("XYZr", False),
    ("XYZw", False), ("", False), (None, False), (123, False),
    (" TPC ", True), ("123", True),
])
def test_shared_case_only_policy_matches_frozen_C(raw, allowed):
    assert raw_symbol_is_uppercase(raw) is allowed


@pytest.mark.parametrize("preferred,common", PAIRS)
def test_core_retained_quote_values_never_alias_common(preferred, common):
    strategy = SchwabV2Strategy(Settings(_env_file=None))
    row = ROWS[common]
    q = Quote(common, float(row["bid_price"]), float(row["ask_price"]),
              float(row["last_trade_price"]), STAMP, 0)
    strategy.on_quote(common, q)
    state = strategy.watchlist_state(common)
    assert state.last_quote is q
    bad = replace(q, symbol=preferred, bid_price=float(ROWS[preferred]["bid_price"]))
    assert strategy.on_quote(preferred, bad) is None
    assert strategy.on_quote(common, bad) is None
    assert preferred not in strategy._symbol_states and state.last_quote is q
    count = len(state.bars)
    bar = ChartBar(preferred, 1, 1, 1, 1, 1, STAMP)
    assert strategy.on_bar(common, bar) is None
    assert strategy.on_bar(preferred, replace(bar, symbol=common)) is None
    assert len(state.bars) == count and preferred not in strategy._symbol_states


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred,common", PAIRS)
@pytest.mark.parametrize("method", ["_handle_bar_from_rest", "_handle_bar_from_streamer", "_handle_quote"])
@pytest.mark.parametrize("bad_argument", [True, False])
async def test_ingress_drops_raw_case_before_any_line_or_persistence_side_effect(preferred, common, method, bad_argument):
    bot = object.__new__(SchwabV2BotService)
    symbol = preferred if bad_argument else common
    field = common if bad_argument else preferred
    value = Quote(field, 1, 1, 1, STAMP, 0) if method == "_handle_quote" else ChartBar(field, 1, 1, 1, 1, 1, STAMP)
    await getattr(bot, method)(symbol, value)
    assert bot.__dict__ == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred,common", PAIRS)
async def test_stream_tick_drops_before_trade_capture_or_cross(preferred, common):
    bot = object.__new__(SchwabV2BotService)
    await bot._handle_stream_tick(SimpleNamespace(symbol=preferred))
    assert bot.__dict__ == {}


def test_scanner_selection_keeps_common_and_dot_class_before_normalization():
    event = StrategyStateSnapshotEvent(source_service="CONTROLLED", payload=StrategyStateSnapshotPayload(
        top_confirmed=[{"ticker": "TpC"}, {"ticker": "TPC"}],
        all_confirmed=[{"ticker": "BCpC"}, {"ticker": "BCPC"}], watchlist=["BRK.B", "brk.b"]))
    assert SchwabV2BotService._extract_confirmed_symbols(event) == ["TPC", "BCPC", "BRK.B"]


def test_reconfirmation_identity_cannot_be_supplied_by_preferred_row():
    bot = object.__new__(SchwabV2BotService)
    bot._line_restoration_enabled = True
    bot._line_scanner_symbols = {"TPC"}
    bot._line_confirmed_identity = {"TPC": "common"}
    event = StrategyStateSnapshotEvent(source_service="CONTROLLED", payload=StrategyStateSnapshotPayload(
        top_confirmed=[{"ticker": "TpC", "confirmed_at": "preferred"},
                       {"ticker": "TPC", "confirmed_at": "common"}]))
    assert bot._line_scanner_transition(event, {"TPC"}) == set()
    assert bot._line_confirmed_identity == {"TPC": "common"}
