from __future__ import annotations

from decimal import Decimal

from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService

ratchet = OmsRiskService._ratcheted_trailing_stop  # pure staticmethod (stop_price, hwm, observed, trail_pct)


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
