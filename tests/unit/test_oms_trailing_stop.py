from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService

ratchet = OmsRiskService._ratcheted_trailing_stop  # pure staticmethod (stop_price, hwm, observed, trail_pct)


def _armed(**kw):
    base = dict(
        strategy_code="orb", broker_account_name="paper:orb", symbol="ABC",
        quantity=Decimal("10"), entry_price=Decimal("5.00"), stop_loss_pct=8.0,
        stop_price=Decimal("4.60"), quote_max_age_ms=5000, initial_panic_buffer_pct=1.5,
        trail_pct=8.0, high_water_mark=Decimal("5.00"),
    )
    base.update(kw)
    return ArmedHardStop(**base)


def test_ratchet_is_bid_only_ignores_high_last_on_wide_spread():
    """Wide-spread thin microcap: a new-high LAST (6.00) with a much-lower BID
    (5.40, ~10% spread) must NOT ratchet the stop off the last (which would set
    stop=5.52 and immediately trigger on the 5.40 bid). Bid-only keeps the trail
    at the backtested 8% width."""
    svc = OmsRiskService.__new__(OmsRiskService)  # bypass heavy __init__; method needs only the caches
    now = datetime.now(timezone.utc)
    svc._latest_quotes_by_symbol = {"ABC": {"bid": 5.40, "ask": 6.10, "received_at": now}}
    svc._latest_trades_by_symbol = {"ABC": {"price": 6.00, "received_at": now}}  # high last
    stop = _armed()
    svc._ratchet_trailing_stop(stop)
    assert stop.high_water_mark == Decimal("5.40")          # tracks the BID, not the 6.00 last
    assert stop.stop_price == Decimal("5.40") * Decimal("0.92")  # 4.968, off the bid
    assert stop.stop_price < Decimal("5.40")                # stays below the bid -> no spurious trigger


def test_ratchet_inert_with_stale_bid():
    svc = OmsRiskService.__new__(OmsRiskService)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    svc._latest_quotes_by_symbol = {"ABC": {"bid": 9.0, "received_at": old}}  # stale -> ignored
    svc._latest_trades_by_symbol = {}
    stop = _armed()
    svc._ratchet_trailing_stop(stop)
    assert stop.stop_price == Decimal("4.60") and stop.high_water_mark == Decimal("5.00")


def test_ratchet_inert_when_trail_pct_zero():
    # Default/fixed-stop path: byte-identical — inputs returned unchanged.
    assert ratchet(Decimal("4.60"), Decimal("5.00"), Decimal("6.00"), 0.0) == (Decimal("4.60"), Decimal("5.00"))


def test_ratchet_raises_to_8pct_below_new_high():
    new_stop, new_hwm = ratchet(Decimal("4.60"), Decimal("5.00"), Decimal("6.00"), 8.0)
    assert new_hwm == Decimal("6.00")
    assert new_stop == Decimal("6.00") * (Decimal("1") - Decimal("8") / Decimal("100"))  # 5.52


def test_ratchet_never_lowers_the_stop():
    # Observation below the HWM is not a new high -> stop + HWM unchanged (never down).
    assert ratchet(Decimal("5.52"), Decimal("6.00"), Decimal("5.50"), 8.0) == (Decimal("5.52"), Decimal("6.00"))
    # A new high always raises the stop monotonically (6.00 -> 6.50 -> stop 5.98).
    new_stop, new_hwm = ratchet(Decimal("5.52"), Decimal("6.00"), Decimal("6.50"), 8.0)
    assert new_hwm == Decimal("6.50")
    assert new_stop == Decimal("6.50") * Decimal("0.92")
    assert new_stop > Decimal("5.52")


def test_armed_hard_stop_new_fields_default_inert():
    s = ArmedHardStop(
        strategy_code="x",
        broker_account_name="acct",
        symbol="ABC",
        quantity=Decimal("10"),
        entry_price=Decimal("5"),
        stop_loss_pct=8.0,
        stop_price=Decimal("4.6"),
        quote_max_age_ms=2000,
        initial_panic_buffer_pct=1.5,
    )
    # New fields default to the fixed-stop (inert) configuration.
    assert s.trail_pct == 0.0
    assert s.high_water_mark is None
