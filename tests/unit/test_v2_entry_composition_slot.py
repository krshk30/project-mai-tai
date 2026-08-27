"""Economic-slot evidence must describe #644 composition, not the broker order style."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy, SymbolState


ET = ZoneInfo("America/New_York")
RTH_MS = int(datetime(2026, 8, 27, 11, 0, tzinfo=ET).timestamp() * 1000)


def _resting_strategy() -> tuple[SchwabV2Strategy, SymbolState]:
    strategy = object.__new__(SchwabV2Strategy)
    strategy._pending_intents = []
    strategy._pending_webull_direct_intents = []
    strategy._resting_entry_band_pct = 0.5
    strategy._eh_resting_enabled = False
    strategy._resting_session_is_eh = lambda: False
    strategy._atr_qty = 2
    strategy._webull_fanout_qty = 1
    strategy._webull_resting_mirror_enabled = True
    strategy._dual_broker_fanout_enabled = True
    return strategy, SymbolState(symbol="CELU")


def test_rested_first_slot_is_stamped_on_both_broker_legs() -> None:
    strategy, state = _resting_strategy()

    strategy._queue_resting_place(state, 2.00, slot="first")

    assert strategy._pending_intents[0].metadata["cw_entry_slot"] == "first"
    assert strategy._pending_webull_direct_intents[0].metadata["cw_entry_slot"] == "first"


def test_rested_reclaim_is_reclaim_on_both_legs_despite_resting_style() -> None:
    """The CELU false breach: both drafts are resting orders, but their economic slot is reclaim."""
    strategy, state = _resting_strategy()

    strategy._queue_resting_place(state, 2.08, slot="reclaim")

    for draft in (strategy._pending_intents[0], strategy._pending_webull_direct_intents[0]):
        assert draft.metadata["resting_entry"] == "true"
        assert draft.metadata["cw_entry_slot"] == "reclaim"


def test_reactive_pair_is_stamped_as_the_reclaim_slot() -> None:
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
    ))
    state = strategy.watchlist_state("CELU")
    for high, timestamp in ((2.00, 1), (1.98, 2), (1.99, 3)):
        state.bars.append(OHLCVBar(timestamp, high - 0.01, high, high - 0.02, high, 25_000))
        signal = {
            "touch": False, "touch_price": None,
            "flip": "BUY" if timestamp == 1 else None,
            "flip_level": 1.90 if timestamp == 1 else None,
            "trail": 1.90, "loss": 0.1, "state": "long", "state_age": 1,
        }
        strategy._cw_v2_track(state, signal)
    strategy._cw_v2_track(state, {**signal, "flip": None, "flip_level": None})

    primary = strategy._cw_v2_quote(
        state, Quote("CELU", 2.09, 2.11, 2.10, RTH_MS, 0)
    )
    webull = strategy.drain_webull_fanout_intents()[0]

    assert primary is not None
    assert primary.metadata["cw_entry_slot"] == "reclaim"
    assert webull.metadata["cw_entry_slot"] == "reclaim"
