"""The Webull leg was racing the broker to notice a fill — and losing almost every time.

⛔⭐⭐ THE DEFECT. `_fanout_rth_resting_cross` watched quotes for price to reach `resting_level`,
then queued the Webull leg. But the Schwab stop-limit sits AT THE BROKER, so it fills the instant
price touches that level — and that detector's own gate (`position_qty != 0` → return) then blocks
it, because by the next quote tick we are already holding.

MEASURED 2026-08-13, regular hours:
    DFSC   filled 3x on Schwab, all three AT the stop price   -> detector fired 0x, Webull got 0
    INHD   filled on Schwab                                    -> Webull got 0
    OFAL   filled on Schwab                                    -> Webull got 0
    FGI    fired ONCE and LATE: px 8.6461 vs level 8.3015 (~4% high), only because a position
           had just closed and briefly re-opened the gate
    XHG    Schwab never traded it -> fired 7 of 7

⭐ XHG is the tell: the leg works precisely when the primary is NOT involved.

THE FIX: the fill is a fact we are already told. Queue the Webull leg from it.
"""
from __future__ import annotations

import inspect

from project_mai_tai.strategy_core import schwab_1m_v2 as strat


def _src() -> str:
    return inspect.getsource(strat.SchwabV2Strategy.update_position)


def test_the_webull_leg_is_queued_from_the_FILL() -> None:
    """THE REGRESSION: without this the leg only ever fires by winning a race it cannot win."""
    s = _src()
    assert "[V2-FANOUT-ON-FILL]" in s
    assert "_build_webull_fanout_draft(" in s
    assert "_pending_webull_fanout_intents.append" in s


def test_it_fires_on_the_ZERO_to_POSITIVE_held_transition() -> None:
    """Must key on FILLS-only (`position_qty_held`), not on the union that counts in-flight
    intents — otherwise our own queued order would trigger it."""
    s = _src()
    assert "prev_held == 0" in s and "state.position_qty_held > 0" in s


def test_the_claim_is_taken_BEFORE_the_draft_is_queued() -> None:
    """⛔ THE LANDMINE. SymbolState is per SYMBOL, not per account, so the WEBULL leg's own fill
    lands in this same transition. The claim must already be set when that second fill arrives, or
    one signal produces two Webull orders."""
    s = _src()
    claim = s.index("state.fanout_webull_claimed = True")
    queue = s.index("_pending_webull_fanout_intents.append")
    assert claim < queue, "claim must be taken before the draft is queued"


def test_it_will_not_fire_when_the_claim_is_already_held() -> None:
    s = _src()
    assert "not state.fanout_webull_claimed" in s


def test_it_anchors_on_the_RESTING_LEVEL_not_a_quote() -> None:
    """The point of firing on the fill is that we know the price. Anchor both the entry and the
    band on the level the primary actually filled at."""
    s = _src()
    assert "entry_px=state.resting_level" in s
    assert "band_anchor=state.resting_level" in s


def test_extended_hours_is_excluded() -> None:
    """EH has its own software soft-rest path; this must not double up with it."""
    assert "not self._resting_session_is_eh()" in _src()


def test_only_a_RESTING_entry_triggers_it() -> None:
    """A reactive entry already queues its own fan-out leg at emit time."""
    assert "state.last_resting_placed_slot" in _src()


def test_default_is_ON_because_OFF_is_the_defect() -> None:
    from project_mai_tai.settings import Settings

    assert Settings().strategy_schwab_1m_v2_webull_fanout_on_fill_enabled is True


def test_there_is_an_off_switch() -> None:
    """Reverting to the racing detector must remain possible without a code change."""
    assert "self._fanout_on_fill_enabled" in _src()
