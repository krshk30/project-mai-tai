"""§82 — ONE Webull fan-out leg per claim window. The reactive path was not honouring the latch.

⛔⭐⭐ MEASURED, live:orb, 2026-08-01..08-19, over the 119 segments that carry a segment id:
NINETEEN emitted more than one Webull leg for a SINGLE `cw_entry_n` — 14 as `reactive` after
`rth_resting`, 2 as the same source twice, 3 with three legs.

⛔ THE COST WAS ONE-DIRECTIONAL. Of 22 extra legs, ALL 22 filled WORSE than the first leg of their
own segment: median 4.58% worse, best +1.65%, worst +21.14%. Not noise — every duplicate chased.
Live examples: AZI +4.8% five minutes later, CLRO +4.4% ten minutes later, STKH +7.2%.

⛔ THIS FIXES ONE OF THE TWO CAUSES, DELIBERATELY. The reactive site claimed `cw_v2_emit_claimed`
(its own dedup) and never read `fanout_webull_claimed`, so one segment satisfied both gates.
The SECOND cause is NOT fixed here and must not be assumed fixed — see
`test_the_expiry_cause_is_NOT_addressed_here`.
"""

from __future__ import annotations

import inspect

from project_mai_tai.strategy_core import schwab_1m_v2 as strat


def _reactive_src() -> str:
    """The reactive intrabar-enter path, where the un-latched fan-out lived.

    ⛔⭐⭐ THIS SLICE WAS A MAGIC CHARACTER COUNT (`idx : idx + 2200`) AND IT BROKE (§266,
    2026-08-24). Adding the suppression marker's comment block pushed the fan-out gate past 2200
    characters, and BOTH latch tests below went red **while the behaviour was completely
    unchanged** — a false alarm that costs exactly as much attention as a real one, and on a day
    with five PRs queued it is the kind of red that gets waved through.

    ⇒ The window is now bounded STRUCTURALLY, from the entry log line to the `TradeIntentDraft`
    that ends the function. A comment can no longer move it, and a genuine relocation of the
    fan-out block out of this function still fails it — which is the thing these tests exist to
    catch.

    ⛔ These remain SOURCE-INSPECTION tests: they prove the gate is written, never that it is
    REACHED. `test_v2_fanout_suppression_marker.py` drives `_cw_v2_quote` for real and asserts on
    the emitted log; that file is the behavioural cover, this one is the shape.
    """
    whole = inspect.getsource(strat)
    idx = whole.index("[V2-CW] %s v2 INTRABAR ENTER")
    end = whole.index("return TradeIntentDraft(", idx)
    return whole[idx:end]


def test_the_reactive_fanout_honours_the_SHARED_latch() -> None:
    """⛔ THE REGRESSION. Without this the reactive leg fires on a segment the resting leg claimed."""
    src = _reactive_src()
    assert "_pending_webull_fanout_intents.append" in src, "anchor moved — re-point this test"
    gate = src.split("_pending_webull_fanout_intents.append")[0]
    assert "not state.fanout_webull_claimed" in gate, (
        "the reactive fan-out must consult the SAME latch the resting paths set"
    )


def test_the_reactive_fanout_also_STAMPS_the_claim() -> None:
    """Without the stamp this leg would be invisible to the shared expiry — claimed forever."""
    src = _reactive_src()
    gate = src.split("_pending_webull_fanout_intents.append")[0]
    assert "_claim_fanout_webull(" in gate
    assert "identity=shared_fanout_identity" in gate


def test_the_reactive_path_keeps_its_OWN_dedup_too() -> None:
    """⛔ The two latches answer different questions and BOTH must stay. `cw_v2_emit_claimed` dedups
    the SCHWAB primary; `fanout_webull_claimed` dedups the WEBULL leg. Replacing one with the other
    would fix the duplicate and reopen a primary double-emit."""
    whole = inspect.getsource(strat)
    assert "state.cw_v2_emit_claimed = True" in whole


def test_the_resting_paths_still_claim() -> None:
    """Both resting sites must keep setting the latch, or the reactive check has nothing to read."""
    on_fill = (
        inspect.getsource(strat.SchwabV2Strategy._apply_position_transition)
        if hasattr(strat.SchwabV2Strategy, "_apply_position_transition")
        else inspect.getsource(strat)
    )
    cross = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "_claim_fanout_webull(" in cross
    assert "fanout_webull_claimed" in on_fill


def test_the_expiry_requires_absent_positive_outcome_evidence() -> None:
    """D2 closes the second cause without turning a local drop into silent suppression.

    The claim expiry re-opens the latch when `position_qty == 0`, and a Webull fill does NOT raise
    that counter. Live STKH 2026-08-14, one segment, three legs in 63 seconds:

        14:30:31  [V2-FANOUT-RTH-RESTING] leg
        14:31:03  [V2-FANOUT-RTH-RESTING] leg
        14:31:34  [V2-FANOUT-CLAIM-EXPIRED] "claim taken 31.4s ago never became a position"
        14:31:34  [V2-FANOUT-RTH-RESTING] leg

    A durable fill/working outcome holds; queued or could_not_tell remains provisional and releases
    after the grace. This preserves FGI's visible-duplicate failure direction without recreating
    STKH's post-fill duplicate.
    """
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "[V2-FANOUT-CLAIM-EXPIRED]" in src
    assert 'state.fanout_claim_outcome in {"queued", "could_not_tell"}' in src
    assert "apply_fanout_outcome" in inspect.getsource(strat.SchwabV2Strategy)
