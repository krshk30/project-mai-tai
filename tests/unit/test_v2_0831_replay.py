"""Replay of the five 2026-08-31 filled duplicate legs against the FIXED code (#858 + #863).

Each case carries the REAL numbers from the box evidence: the mirror's resting level and the
crossing price from the [V2-FANOUT-RTH-RESTING] emission line whose leg later FILLED as a
duplicate on live:orb. The replay drives the fixed state machine through the recorded
sequence — mirror placed, positive OMS evidence held, Schwab union polled at zero past the old
hold bound, then the recorded crossing quote — and asserts the emission is SUPPRESSED, the
claim survives, and the crossing is still COUNTED (the #863 denominator line).

⚠ LIMIT, stated per the operator's instruction: this proves the fix WOULD HAVE PREVENTED the
known 08-31 cases; it does not prove live-timing behaviour (outcome-record transport lag,
broker latencies, quote cadence). The live grade is next session's acceptance watch.
"""

from __future__ import annotations

import logging

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    FANOUT_POSITIVE_ZERO_HOLD_MS,
    OHLCVBar,
    SchwabV2Strategy,
)

# (symbol, mirror resting_level, crossing px, emission UTC on 2026-08-31) — from the box log
# lines whose legs FILLED as duplicates on live:orb, reconciled with broker_orders+fills.
CASES = [
    ("YDDL", 2.4315, 2.4400, "14:29:37"),
    ("WETO", 12.6391, 12.7099, "15:30:11"),
    ("NCRA", 3.0360, 3.0400, "16:21:23"),
    ("RDHL", 1.0747, 1.0750, "16:35:24"),
    ("NCRA", 3.0400, 3.0499, "18:46:49"),
]
T0 = 1787846400000
OBSERVED = "[V2-FANOUT-MIRROR-LIVE-CROSS]"


def _strategy(symbol: str, level: float):
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        )
    )
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._entries_held = False
    strategy._resting_entry_enabled = True
    clock = {"now": T0}
    strategy._now_ms = lambda: clock["now"]
    st = strategy.watchlist_state(symbol)
    st.fanout_segment_id = T0
    st.resting_active = True
    st.resting_level = level
    st.last_resting_placed_slot = "first"
    st.bars.append(OHLCVBar(timestamp_ms=T0 - 30_000, open=level * 0.99, high=level,
                            low=level * 0.98, close=level * 0.995, volume=25_000))
    return strategy, st, clock


def _quote(symbol: str, px: float, ms: int) -> Quote:
    return Quote(symbol, px - 0.01, px + 0.01, px, ms, 0)


def _mirror_placed(strategy, st):
    """The state the 08-31 mirror placements left: rest working at Webull, claim held with
    positive submitted evidence, observation edge armed."""
    identity = strategy._fanout_identity_metadata(st, source="rth_resting_mirror")
    assert strategy._claim_fanout_webull(st, identity=identity, reason="replay-mirror-place")
    st.fanout_claim_outcome = "held"          # positive OMS evidence arrived, as on the day
    st.webull_resting_active = True
    st.fanout_mirror_cross_below_seen = True  # placement arms the edge
    return identity


@pytest.mark.parametrize("symbol,level,px,at", CASES, ids=[f"{s}@{t}Z" for s, _, _, t in CASES])
def test_0831_duplicate_leg_is_suppressed_by_the_fixed_code(symbol, level, px, at, caplog) -> None:
    strategy, st, clock = _strategy(symbol, level)
    _mirror_placed(strategy, st)

    # The recorded sequence: Schwab union polls read ZERO the whole time (every Schwab open was
    # hard-rejected on 08-31), running past the old #824 hold bound that erased the claim.
    strategy.update_position(symbol, 0, held_qty=0)
    clock["now"] = T0 + FANOUT_POSITIVE_ZERO_HOLD_MS + 6_000
    st.bars[-1] = OHLCVBar(timestamp_ms=clock["now"] - 30_000, open=level * 0.99, high=level,
                           low=level * 0.98, close=level * 0.995, volume=25_000)
    strategy.update_position(symbol, 0, held_qty=0)
    assert st.fanout_webull_claimed is True, (
        "the zero-hold erased the claim again — the 08-31 mechanism is back"
    )

    # The recorded crossing quote (real px vs the real mirror level).
    with caplog.at_level(logging.INFO, logger="project_mai_tai.strategy_core.schwab_1m_v2"):
        strategy._fanout_rth_resting_cross(st, _quote(symbol, px, clock["now"]))

    assert strategy.drain_webull_fanout_intents() == [], (
        f"{symbol} {at}Z: the duplicate leg EMITTED against the fixed code"
    )
    assert st.fanout_webull_claimed is True, "the claim must survive the crossing"
    assert OBSERVED in caplog.text, (
        f"{symbol} {at}Z: the crossing was suppressed but NOT counted — the acceptance "
        "denominator loses this case"
    )


@pytest.mark.parametrize("symbol,level,px,at", CASES, ids=[f"{s}@{t}Z" for s, _, _, t in CASES])
def test_0831_fixture_reaches_the_emit_path_when_unguarded(symbol, level, px, at) -> None:
    """Reachability control: the SAME real numbers, with the two fixed-state facts removed
    (claim free, mirror flag down — what the pre-fix zero-hold actually left behind), DO emit.
    Proves each case's suppression above measures the guards, not a dead fixture."""
    strategy, st, clock = _strategy(symbol, level)
    assert st.webull_resting_active is False and st.fanout_webull_claimed is False

    strategy._fanout_rth_resting_cross(st, _quote(symbol, px, clock["now"]))

    assert len(strategy.drain_webull_fanout_intents()) == 1, (
        f"{symbol} {at}Z: fixture cannot reach the emit path — the suppression test is vacuous"
    )
