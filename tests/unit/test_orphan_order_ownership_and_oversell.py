"""Orphan-order detector, second generation — proven against the REAL 2026-08-11 FRTT incident.

WHY THESE EXIST. The existing `classify_order` works and it caught FRTT: `ORPHAN ORDER RED - 15:00 ET`
was classified, pushed, and received on the phone. But it is a heuristic on PRICE DISTANCE, so it
could only fire once price had fallen 13% away from the trigger — **120 minutes** after the order was
actually disowned.

    13:00:03  accepted   resting buy-stop placed (reclaim slot, stop 1.5200)
    13:01:02  rejected   the CANCEL failed -- "upstream connect error or disconnect/reset before
                         headers. reset reason: connection termination"
    15:00:01  RED        the stale-trigger heuristic finally fires (13.0% away, 120min)
    15:17:41  cancelled  by the OPERATOR, by hand

`classify_unowned` asks a different and stronger question — *does anything own this order?* — which
is a FACT about our own records. On the same tape it fires at **13:01:02**, removing ~2h of
unmanaged live exposure.

`classify_oversell` covers the shape the operator saw on the ladder: FOUR `-2` sells against a TWO
share position. Near the market, on a watched symbol — structurally invisible to BOTH existing
shapes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "orphan_order_check", Path(__file__).resolve().parents[2] / "ops/health/orphan_order_check.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
classify_unowned = _m.classify_unowned
classify_oversell = _m.classify_oversell
CANCEL_GRACE_SEC = _m.CANCEL_GRACE_SEC

# The real order.
FRTT = dict(symbol="FRTT", order_id="1007551298613")


# ------------------------------------------------------------------ ownership: the FRTT tape

def test_the_real_FRTT_failed_cancel_is_RED_at_13_01() -> None:
    """THE CASE THAT HAPPENED, at the instant it happened.

    One second after the cancel was rejected the grace has not elapsed, so this is still silent —
    correct. At CANCEL_GRACE_SEC it must go RED, which on the real tape is 13:03, not 15:00.
    """
    assert classify_unowned(**FRTT, our_status="accepted",
                            cancel_attempt_age_sec=1.0, cancel_failed=True) is None

    v = classify_unowned(**FRTT, our_status="accepted",
                         cancel_attempt_age_sec=float(CANCEL_GRACE_SEC), cancel_failed=True)
    assert v is not None, "a cancel that FAILED must not pass silently"
    assert v[0] == "RED"
    assert "1007551298613" in v[1] and "FAILED" in v[1]


def test_it_fires_two_hours_before_the_stale_trigger_heuristic() -> None:
    """The latency claim, pinned. At 13:03 the trigger was ~1.3% from market — far below the 5%
    the stale check needs — so ONLY the ownership check can see it there."""
    early = classify_unowned(**FRTT, our_status="accepted",
                             cancel_attempt_age_sec=120.0, cancel_failed=True)
    assert early is not None and early[0] == "RED"
    # and the price heuristic is provably blind at that moment
    assert _m.classify_order(symbol="FRTT", instruction="BUY", order_type="STOP_LIMIT",
                             trigger=1.52, age_min=3.0, mid=1.50, in_watchlist=True) is None


def test_our_record_terminal_while_broker_says_working_is_RED() -> None:
    """The second disowned shape: we believe it is dead, the broker has it live."""
    v = classify_unowned(**FRTT, our_status="cancelled",
                         cancel_attempt_age_sec=None, cancel_failed=False)
    assert v is not None and v[0] == "RED" and "nothing owns it" in v[1]


# ------------------------------------------------- ⛔ the false-positive guard the operator asked for

def test_a_cancel_in_flight_is_silent() -> None:
    """⛔ THE ONE PLACE THIS COULD PAGE ON HEALTHY BEHAVIOUR.

    Between emitting a cancel intent and the broker acknowledging it, the order is legitimately
    still WORKING while our row is still non-terminal. That window is NORMAL. Paging on it would
    make the check fire on every single cancel the bot issues — hundreds a day.
    """
    for age in (0.0, 1.0, 30.0, CANCEL_GRACE_SEC - 1):
        assert classify_unowned(**FRTT, our_status="accepted",
                                cancel_attempt_age_sec=age, cancel_failed=True) is None, \
            f"a cancel in flight for {age}s must be silent"


def test_a_healthy_working_order_is_silent() -> None:
    """No cancel attempted, our row agrees it is live -> nothing to say."""
    assert classify_unowned(**FRTT, our_status="accepted",
                            cancel_attempt_age_sec=None, cancel_failed=False) is None


# ------------------------------------------------------------------ oversell: the ladder shape

def test_the_real_ladder_four_sells_against_two_shares_is_RED() -> None:
    """WHAT THE OPERATOR SAW: -2 LMT 1.55, -2 STP 1.44, -2 LMT 1.40, -2 STP 1.30 on a +2 position."""
    v = classify_oversell(symbol="FRTT", account="live:schwab_1m_v2",
                          working_sell_qty=8.0, held_qty=2.0)
    assert v is not None, "8 shares of sells against 2 held must not pass silently"
    assert v[0] == "RED"
    assert "SHORT by 6" in v[1]


def test_one_bracket_on_a_matching_position_is_silent() -> None:
    """⛔ FALSE-POSITIVE GUARD. A normal OCO bracket is a target AND a stop — but they are
    ONE-cancels-the-other, so only one can ever fill. Two legs of 2 on 2 held is HEALTHY and is the
    single most common state this check will ever see."""
    assert classify_oversell(symbol="FRTT", account="live:schwab_1m_v2",
                             working_sell_qty=2.0, held_qty=2.0) is None


def test_no_position_and_no_sells_is_silent() -> None:
    assert classify_oversell(symbol="FRTT", account="live:schwab_1m_v2",
                             working_sell_qty=0.0, held_qty=0.0) is None


def test_a_sell_with_nothing_held_is_RED() -> None:
    """A protective sell against a position that no longer exists is a naked short waiting to
    happen — the exact residue a failed exit-reconcile leaves behind."""
    v = classify_oversell(symbol="FRTT", account="live:schwab_1m_v2",
                          working_sell_qty=2.0, held_qty=0.0)
    assert v is not None and v[0] == "RED" and "SHORT by 2" in v[1]
