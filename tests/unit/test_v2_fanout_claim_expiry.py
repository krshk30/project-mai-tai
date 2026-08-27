"""A blocked Webull leg burned the whole flip — the claim was taken at QUEUE time and never released.

⛔⭐⭐ THE LIVE COST, FGI 2026-08-13. The 10:02 fan-out leg was blocked by our OWN band cap
(`ASK_PAST_BAND`). The once-per-flip claim stayed set, and the next TWO Schwab entries — 10:18 and
13:44 — were never offered to Webull at all. **Zero orders sent, so zero broker errors**: the silence
read as the broker refusing us when in fact we had stopped asking.

Same day, same session: Schwab received **57 FGI** orders and **47 DFSC** orders. Webull received
**none of either**.

⛔ THE CLAIM MEANT "WE TRIED ONCE". IT MUST MEAN "WE HAVE A LEG WORKING". The only releases were a
position close, a fresh flip, or the 04:00 roll — none of which fire when an order is simply refused.
"""
from __future__ import annotations

import inspect

from project_mai_tai.strategy_core import schwab_1m_v2 as strat


def test_the_claim_is_timestamped_when_taken() -> None:
    """Without the stamp there is nothing to expire against."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "_claim_fanout_webull(" in src
    helper = inspect.getsource(strat.SchwabV2Strategy._claim_fanout_webull)
    assert "state.fanout_claim_ms = self._now_ms()" in helper


def test_an_unfilled_claim_is_RELEASED_after_the_grace() -> None:
    """THE REGRESSION. A claim that never became a position must not hold the flip."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "_release_fanout_webull_claim(" in src
    assert "_fanout_claim_grace_ms" in src
    assert "[V2-FANOUT-CLAIM-EXPIRED]" in src, "the release must be visible in the log"


def test_release_requires_NO_position() -> None:
    """⛔ THE DOUBLE-LEG GUARD. If the leg actually filled we must NOT re-open the claim — that would
    queue a second Webull order for one signal. The release is conditional on position_qty == 0."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    head = src.split("_release_fanout_webull_claim(")[0]
    assert "state.position_qty == 0" in head, "release must be gated on being flat"


def test_release_happens_BEFORE_the_claim_gate() -> None:
    """Ordering is the whole fix: releasing after the `return` would never run."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    release = src.index("_release_fanout_webull_claim(")
    gate = src.index("if state.position_qty != 0 or state.fanout_webull_claimed:")
    assert release < gate, "the expiry must be evaluated before the early return"


def test_grace_zero_disables_it() -> None:
    """⛔ The escape hatch: 0 restores today's behaviour exactly."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "self._fanout_claim_grace_ms > 0" in src


def test_the_default_grace_is_set_and_sane() -> None:
    from project_mai_tai.settings import Settings

    g = Settings().strategy_schwab_1m_v2_webull_fanout_claim_grace_secs
    assert g == 30.0
    assert 5.0 <= g <= 120.0, "long enough for a fill to confirm, short enough not to lose the flip"


def test_every_release_site_also_clears_the_timestamp() -> None:
    """A stale ms left behind on a reset would expire a FRESH claim instantly."""
    helper = inspect.getsource(strat.SchwabV2Strategy._release_fanout_webull_claim)
    assert "state.fanout_webull_claimed = False" in helper
    assert "state.fanout_claim_ms = 0" in helper
