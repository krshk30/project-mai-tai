"""Orphan-order detector — proven against the REAL 2026-07-28 POLA case.

A green live run only means nothing is orphaned right now. These pin that the detector would
actually have caught POLA, using its real numbers, so the operator never has to find the next one
by eye on a TOS chart.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "orphan_order_check", Path(__file__).resolve().parents[2] / "ops/health/orphan_order_check.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
classify_order = _m.classify_order


def _pola(**kw):
    """The real order: BUY 2 STOP_LIMIT stop=2.19, placed 10:30 ET, market 1.89 an hour later."""
    base = dict(symbol="POLA", instruction="BUY", order_type="STOP_LIMIT", trigger=2.19,
                age_min=60.0, mid=1.895, in_watchlist=True, order_id="1007358561714")
    base.update(kw)
    return classify_order(**base)


def test_the_real_POLA_order_is_flagged_RED() -> None:
    """THE CASE THAT HAPPENED. trigger 2.19 vs market 1.895 = 15.6% away, WORKING 60min, and
    POLA was still in the watchlist — so the watchlist check alone would NOT have caught it.
    Distance from market is what makes this detectable."""
    verdict = _pola()
    assert verdict is not None, "the POLA order must not pass silently"
    assert verdict[0] == "RED"
    assert "POLA" in verdict[1] and "15.6% from market" in verdict[1]


def test_a_normal_resting_order_near_the_market_is_silent() -> None:
    """⛔ THE FALSE-POSITIVE GUARD. A resting buy-stop sits ABOVE the market by design — that is
    the whole strategy. Paging on every healthy resting order would make this pager worthless."""
    assert _pola(trigger=1.93, mid=1.895) is None      # ~1.8% above market: normal setup


def test_a_young_order_is_never_flagged() -> None:
    """A freshly placed order is simply waiting. Age is what separates 'waiting' from 'orphaned'."""
    assert _pola(age_min=5.0) is None


def test_a_stale_order_off_the_watchlist_is_AMBER_not_silent() -> None:
    """Nobody is evaluating it — weaker signal than distance, but it must not vanish."""
    verdict = _pola(trigger=1.93, mid=1.895, in_watchlist=False)
    assert verdict is not None and verdict[0] == "AMBER"


def test_missing_quote_data_does_not_produce_a_false_RED() -> None:
    """No quote -> distance is unknowable. It must fall through to the watchlist check rather
    than invent a percentage."""
    assert _pola(mid=None, in_watchlist=True) is None
    assert _pola(mid=None, in_watchlist=False)[0] == "AMBER"
