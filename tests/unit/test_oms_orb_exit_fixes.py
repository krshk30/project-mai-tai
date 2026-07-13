"""ORB/Webull hard-stop exit fixes (2026-07-13 live incident).

Three defects surfaced when ORB's 3% trailing stops fired on live Webull winners:
  A) native-stop-guard (re)arm reverse-rejected (ORDER_NOT_SUPPORT_REVERSE_OPTION) when
     the just-cancelled guard / entry fill had not settled -> backup guard failed to arm.
  B) close/guard RETRY client_order_ids appended `-r<8hex>` each attempt, blowing past
     Webull's 40-char cap -> ILLEGAL_PARAMETER, so the exit could never place.
  C) after the position was flattened out-of-band (manual close), the in-memory armed
     stop kept re-submitting closes on a PHANTOM forever (never reconciled to broker-flat).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService


def _bare_service() -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    return svc


def _armed_stop() -> ArmedHardStop:
    return ArmedHardStop(
        strategy_code="orb", broker_account_name="live:orb", symbol="LGPS",
        quantity=Decimal("5"), entry_price=Decimal("1.19"), stop_loss_pct=3.0,
        stop_price=Decimal("1.29"), quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
        trail_pct=8.0, high_water_mark=Decimal("1.40"),
    )


# --------------------------------------------------------------------------- #
# Bug B — client_order_id stays within Webull's 40-char cap
# --------------------------------------------------------------------------- #
def test_replacement_client_order_id_within_cap_and_does_not_grow_on_retries():
    base = "orb-LGPS-close-e49f4a08e6f2"  # a real close coid
    r1 = OmsRiskService._replacement_client_order_id(base)
    r2 = OmsRiskService._replacement_client_order_id(r1)  # retry the retry
    r3 = OmsRiskService._replacement_client_order_id(r2)  # and again
    for r in (r1, r2, r3):
        assert len(r) <= 40, f"{r!r} exceeds Webull's 40-char client_order_id cap"
        assert r.startswith("orb-LGPS-close")
    # Each retry REPLACES the prior -r suffix rather than accumulating it.
    assert r3.count("-r") == 1


def test_replacement_client_order_id_bounds_an_already_long_base():
    r = OmsRiskService._replacement_client_order_id("x" * 200)
    assert len(r) <= 40


def test_build_client_order_id_is_bounded_to_cap():
    svc = _bare_service()
    event = SimpleNamespace(
        event_id=SimpleNamespace(hex="0123456789abcdef0123"),
        payload=SimpleNamespace(strategy_code="a" * 30, symbol="LONGSYM", intent_type="close"),
    )
    assert len(svc._build_client_order_id(event)) <= OmsRiskService._CLIENT_ORDER_ID_MAX_LEN


def test_build_client_order_id_byte_identical_for_normal_inputs():
    svc = _bare_service()
    event = SimpleNamespace(
        event_id=SimpleNamespace(hex="e49f4a08e6f2abcd"),
        payload=SimpleNamespace(strategy_code="orb", symbol="LGPS", intent_type="close"),
    )
    assert svc._build_client_order_id(event) == "orb-LGPS-close-e49f4a08e6f2"


# --------------------------------------------------------------------------- #
# Bug A — reverse-conflict detection on the native-guard arm
# --------------------------------------------------------------------------- #
def test_reverse_conflict_reject_detected():
    reports = [SimpleNamespace(
        event_type="rejected",
        reason="Webull order rejected: ORDER_NOT_SUPPORT_REVERSE_OPTION ORDER_NOT_SUPPORT_REVERSE_OPTION (http 417)",
    )]
    assert OmsRiskService._is_reverse_conflict_reject(reports) is True


def test_reverse_conflict_reject_false_for_other_outcomes():
    assert OmsRiskService._is_reverse_conflict_reject(
        [SimpleNamespace(event_type="accepted", reason=None)]
    ) is False
    # a different rejection (e.g. the coid-length one) is NOT a reverse conflict
    assert OmsRiskService._is_reverse_conflict_reject(
        [SimpleNamespace(event_type="rejected", reason="ILLEGAL_PARAMETER client_order_id value length between 1 and 40")]
    ) is False


# --------------------------------------------------------------------------- #
# Bug C — reconcile a phantom armed stop against the broker
# --------------------------------------------------------------------------- #
def _reconcile_service(*, broker_flat: bool) -> OmsRiskService:
    svc = _bare_service()
    svc._armed_hard_stops = {}
    svc._armed_stop_persistence_enabled = False

    async def _no_guard(**_):
        return False

    svc._has_active_native_stop_guard_order = _no_guard

    async def _failing_close(event):
        # the close is rejected with a reason that is NOT a known no-position reason
        return [SimpleNamespace(payload=SimpleNamespace(
            status="rejected",
            reason="Webull order rejected: ORDER_NOT_SUPPORT_REVERSE_OPTION (http 417)",
        ))]

    svc.process_trade_intent = _failing_close

    async def _positions(_name):
        if broker_flat:
            return []
        return [SimpleNamespace(symbol="LGPS", quantity=Decimal("5"))]

    svc.broker_adapter = SimpleNamespace(list_account_positions=_positions)
    return svc


def test_reconcile_clears_phantom_stop_when_broker_flat(monkeypatch):
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)

    svc = _reconcile_service(broker_flat=True)
    stop = _armed_stop()
    key = svc._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
    svc._armed_hard_stops[key] = stop

    for _ in range(OmsRiskService._HARD_STOP_RECONCILE_AFTER_FAILURES):
        asyncio.run(svc._trigger_hard_stop(stop, trigger_price=Decimal("1.17"), trigger_source="bid"))

    assert key not in svc._armed_hard_stops, "phantom armed stop was NOT cleared after a broker-flat read"


def test_reconcile_keeps_stop_when_broker_still_holds(monkeypatch):
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)

    svc = _reconcile_service(broker_flat=False)
    stop = _armed_stop()
    key = svc._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
    svc._armed_hard_stops[key] = stop

    for _ in range(OmsRiskService._HARD_STOP_RECONCILE_AFTER_FAILURES + 2):
        asyncio.run(svc._trigger_hard_stop(stop, trigger_price=Decimal("1.17"), trigger_source="bid"))

    assert key in svc._armed_hard_stops, "a genuinely-held position must KEEP its protective stop"
    # The counter resets to 0 each time the broker read confirms the position is still held,
    # so it never accumulates unbounded — it always re-checks within the threshold window.
    assert stop.consecutive_close_failures < OmsRiskService._HARD_STOP_RECONCILE_AFTER_FAILURES


def test_broker_position_read_failure_never_clears_the_stop():
    svc = _bare_service()

    async def _boom(_name):
        raise RuntimeError("broker unreachable")

    svc.broker_adapter = SimpleNamespace(list_account_positions=_boom)
    assert asyncio.run(svc._broker_position_is_flat(_armed_stop())) is False
