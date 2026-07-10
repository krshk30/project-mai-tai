"""Unit tests for scripts/broker_ab_report.py — pairing + metric math on synthetic
in-memory rows. No DB is touched (fetch_legs / psycopg are never imported here)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "broker_ab_report",
    Path(__file__).resolve().parents[2] / "scripts" / "broker_ab_report.py",
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
# Register before exec so dataclass string-annotation resolution can find the module.
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


def _dt(second: int, micro: int = 0) -> datetime:
    return datetime(2026, 7, 10, 14, 30, second, micro, tzinfo=UTC)


def _leg(account, symbol, *, status="filled", submitted, ref, is_webull=False,
         reject_reason=None, fills=()):
    return mod.OrderLeg(
        account=account,
        symbol=symbol,
        status=status,
        submitted_at=submitted,
        reference_price=ref,
        is_webull=is_webull,
        reject_reason=reject_reason,
        fills=list(fills),
    )


# --------------------------------------------------------------------------------------
# Per-leg metric math
# --------------------------------------------------------------------------------------
def test_slippage_and_latency_for_filled_leg():
    # ref 10.00, filled 10.05 → +0.5% / +$0.05; submitted @00, filled @03 → 3.0s
    leg = _leg("live:schwab_1m_v2", "AAA", submitted=_dt(0), ref=10.00,
               fills=[mod.FillRow(quantity=10, price=10.05, filled_at=_dt(3))])
    pct, usd = mod.leg_slippage(leg)
    assert round(pct, 6) == 0.5
    assert round(usd, 6) == 0.05
    assert mod.leg_fill_latency_s(leg) == 3.0
    assert mod.leg_outcome(leg) == "filled"


def test_weighted_avg_fill_price_across_partials():
    leg = _leg("a", "AAA", submitted=_dt(0), ref=10.0,
               fills=[mod.FillRow(quantity=4, price=10.00, filled_at=_dt(1)),
                      mod.FillRow(quantity=6, price=10.10, filled_at=_dt(4))])
    # (4*10 + 6*10.1)/10 = 10.06 ; latency uses LAST fill (@04) → 4.0s
    assert round(mod.leg_fill_price(leg), 6) == 10.06
    assert mod.leg_fill_latency_s(leg) == 4.0


def test_webull_internal_place_to_fill():
    leg = _leg("live:v2_webull", "AAA", submitted=_dt(0), ref=10.0, is_webull=True,
               fills=[mod.FillRow(quantity=5, price=10.0, filled_at=_dt(2),
                                  webull_place_time=_dt(0),
                                  webull_fill_time=_dt(1, 500000))])
    assert mod.leg_webull_internal_s(leg) == 1.5


def test_null_reference_price_guards_slippage():
    leg = _leg("a", "AAA", submitted=_dt(0), ref=None,
               fills=[mod.FillRow(quantity=1, price=10.0, filled_at=_dt(1))])
    assert mod.leg_slippage(leg) == (None, None)


def test_reject_leg_has_no_fill_metrics():
    leg = _leg("live:v2_webull", "AAA", status="rejected", submitted=_dt(0), ref=10.0,
               is_webull=True, reject_reason="must-be-placed-with-broker", fills=[])
    assert mod.leg_outcome(leg) == "rejected"
    assert mod.leg_fill_price(leg) is None
    assert mod.leg_fill_latency_s(leg) is None
    assert mod.leg_slippage(leg) == (None, None)


# --------------------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------------------
def test_clean_pair_matches_nearest_within_window():
    prim = _leg("live:schwab_1m_v2", "AAA", submitted=_dt(0), ref=10.0,
                fills=[mod.FillRow(quantity=10, price=10.05, filled_at=_dt(3))])
    mir = _leg("live:v2_webull", "AAA", submitted=_dt(2), ref=10.0, is_webull=True,
               fills=[mod.FillRow(quantity=10, price=10.02, filled_at=_dt(9))])
    pairs = mod.pair_legs([prim], [mir], window_s=15)
    assert len(pairs) == 1 and pairs[0].mirror is mir
    # Different fill prices/times → distinct slippage + latency per leg.
    assert round(mod.leg_slippage(prim)[0], 6) == 0.5
    assert round(mod.leg_slippage(mir)[0], 6) == 0.2
    assert mod.leg_fill_latency_s(prim) == 3.0
    assert mod.leg_fill_latency_s(mir) == 7.0  # filled@09 − submitted@02


def test_unpaired_primary_when_mirror_missing():
    prim = _leg("live:schwab_1m_v2", "AAA", submitted=_dt(0), ref=10.0,
                fills=[mod.FillRow(quantity=10, price=10.0, filled_at=_dt(2))])
    # mirror is a different symbol → cannot pair
    mir = _leg("live:v2_webull", "ZZZ", submitted=_dt(1), ref=5.0, is_webull=True)
    pairs = mod.pair_legs([prim], [mir], window_s=15)
    assert len(pairs) == 1 and pairs[0].mirror is None


def test_out_of_window_does_not_pair():
    prim = _leg("p", "AAA", submitted=_dt(0), ref=10.0)
    mir = _leg("m", "AAA", submitted=_dt(30), ref=10.0, is_webull=True)  # 30s > 15s window
    pairs = mod.pair_legs([prim], [mir], window_s=15)
    assert pairs[0].mirror is None


def test_greedy_one_to_one_pairing():
    p1 = _leg("p", "AAA", submitted=_dt(0), ref=10.0)
    p2 = _leg("p", "AAA", submitted=_dt(5), ref=10.0)
    m1 = _leg("m", "AAA", submitted=_dt(1), ref=10.0, is_webull=True)
    m2 = _leg("m", "AAA", submitted=_dt(6), ref=10.0, is_webull=True)
    pairs = mod.pair_legs([p1, p2], [m1, m2], window_s=15)
    assert pairs[0].mirror is m1  # p1 (00) → m1 (01)
    assert pairs[1].mirror is m2  # p2 (05) → m2 (06), m1 already used
    assert len({id(p.mirror) for p in pairs}) == 2


# --------------------------------------------------------------------------------------
# Aggregation + Webull coverage
# --------------------------------------------------------------------------------------
def test_aggregate_side_stats():
    legs = [
        _leg("m", "AAA", submitted=_dt(0), ref=10.0, is_webull=True,
             fills=[mod.FillRow(quantity=10, price=10.10, filled_at=_dt(2))]),  # +1.0%, 2s
        _leg("m", "BBB", submitted=_dt(0), ref=20.0, is_webull=True,
             fills=[mod.FillRow(quantity=10, price=20.20, filled_at=_dt(4))]),  # +1.0%, 4s
    ]
    agg = mod.aggregate_side("Webull", legs)
    assert agg.count == 2 and agg.fill_count == 2 and agg.fill_rate == 1.0
    assert round(agg.avg_slippage_pct, 6) == 1.0
    assert agg.avg_latency_s == 3.0 and agg.median_latency_s == 3.0


def test_webull_coverage_counts_rejects_not_fill_comparison():
    mirror_legs = [
        _leg("m", "AAA", submitted=_dt(0), ref=10.0, is_webull=True,
             fills=[mod.FillRow(quantity=10, price=10.0, filled_at=_dt(2))]),
        _leg("m", "BBB", status="rejected", submitted=_dt(0), ref=10.0, is_webull=True,
             reject_reason="must-be-placed-with-broker"),
        _leg("m", "CCC", status="rejected", submitted=_dt(0), ref=10.0, is_webull=True,
             reject_reason="must-be-placed-with-broker"),
    ]
    cov = mod.webull_coverage(mirror_legs)
    assert cov.attempts == 3 and cov.filled == 1 and cov.rejected == 2
    assert cov.reject_reasons == {"must-be-placed-with-broker": 2}
    # The rejected legs contribute no fill metrics (would be excluded from comparison).
    assert mod.aggregate_side("Webull", [mirror_legs[1]]).fill_count == 0


# --------------------------------------------------------------------------------------
# Rendering: empty case + end-to-end shape
# --------------------------------------------------------------------------------------
def test_empty_case_renders_gracefully():
    out = mod.render_report(
        range_label="2026-07-10", primary_account="live:schwab_1m_v2",
        mirror_account="live:v2_webull", pairs=[], all_mirror_legs=[], window_s=15,
    )
    assert "no mirrored trades in range" in out


def test_full_report_contains_sections():
    prim = _leg("live:schwab_1m_v2", "AAA", submitted=_dt(0), ref=10.0,
                fills=[mod.FillRow(quantity=10, price=10.05, filled_at=_dt(3))])
    mir = _leg("live:v2_webull", "AAA", submitted=_dt(2), ref=10.0, is_webull=True,
               fills=[mod.FillRow(quantity=10, price=10.02, filled_at=_dt(9),
                                  webull_place_time=_dt(2), webull_fill_time=_dt(9))])
    reject = _leg("live:v2_webull", "BBB", status="rejected",
                  submitted=_dt(0) + timedelta(minutes=1),
                  ref=5.0, is_webull=True, reject_reason="must-be-placed-with-broker")
    pairs = mod.pair_legs([prim], [mir], window_s=15)
    out = mod.render_report(
        range_label="rng", primary_account="live:schwab_1m_v2",
        mirror_account="live:v2_webull", pairs=pairs,
        all_mirror_legs=[mir, reject], window_s=15,
    )
    assert "AGGREGATE" in out
    assert "WEBULL COVERAGE" in out
    assert "must-be-placed-with-broker" in out


def test_pair_window_default_constant():
    # Guards the documented 15s default the mirror relies on.
    assert mod.DEFAULT_PAIR_WINDOW_S == 15.0
    assert timedelta(seconds=mod.DEFAULT_PAIR_WINDOW_S).total_seconds() == 15.0
