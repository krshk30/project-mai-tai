"""OMS working-order refresh EXEMPTION for RESTING TRIGGER entries (buy STOP/STOP_LIMIT).

A resting trigger is DESIGNED to sit at the ATR line until price crosses it. It was already exempt
from the INTENT_MAX_AGE/SETUP_INVALID abandons (2026-07-23), but NOT from the working-order refresh,
so `_refresh_working_order` cancel/replaced it every 5s (~12x/min) — re-opening the "no order resting
when price crosses" miss the STABLE-REST rework closed. Default now = FULL-EXEMPT: the refresh leaves
it in place (`oms_refresh_resting_trigger_orders=False`). Two invariants must hold:
  1. the refresh SKIPS a resting trigger (no cancel/replace), and
  2. `should_refresh` still fires for it, so the MARKET_CLOSED out-of-session abandon (a backstop that
     lives inside `if should_refresh:`) is preserved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


def _svc(**over) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)   # only reads self.settings + static helpers
    svc.settings = Settings(**over)
    return svc


def _order(order_type: str, *, stop_guard: bool = False, age_secs: float = 0.0):
    payload = {"order_type": order_type}
    if stop_guard:
        payload["stop_guard"] = "true"
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return SimpleNamespace(payload=payload, order_type=order_type,
                           updated_at=updated, submitted_at=updated)


def test_default_is_full_exempt():
    assert Settings().oms_refresh_resting_trigger_orders is False


def test_resting_trigger_is_refresh_exempt_by_default():
    svc = _svc()
    assert svc._resting_trigger_refresh_exempt(_order("STOP_LIMIT")) is True
    assert svc._resting_trigger_refresh_exempt(_order("STOP")) is True


def test_marketable_orders_are_never_refresh_exempt():
    svc = _svc()
    assert svc._resting_trigger_refresh_exempt(_order("LIMIT")) is False
    assert svc._resting_trigger_refresh_exempt(_order("MARKET")) is False


def test_flag_true_restores_the_old_refresh_behavior():
    # opt back in -> a resting trigger is NO LONGER exempt (refreshes like before)
    svc = _svc(oms_refresh_resting_trigger_orders=True)
    assert svc._resting_trigger_refresh_exempt(_order("STOP_LIMIT")) is False


def test_should_refresh_still_fires_so_market_closed_backstop_is_preserved():
    # The exemption must NOT suppress should_refresh (the MARKET_CLOSED abandon lives inside it).
    # An aged resting trigger still reports should_refresh=True; the refresh ACTION is skipped
    # downstream by _resting_trigger_refresh_exempt, not by turning should_refresh off.
    svc = _svc()
    assert svc._should_refresh_working_order(_order("STOP_LIMIT", age_secs=6)) is True
    assert svc._refresh_after_seconds(_order("STOP_LIMIT")) == 5.0   # standard cadence, unchanged


def test_protective_stop_guard_is_NOT_refresh_exempt():
    # A protective sell stop-guard is also STOP-typed, but it MUST keep its staged re-arm cadence —
    # the exemption must exclude it (else the hard-stop guard would never re-arm).
    svc = _svc()
    assert svc._resting_trigger_refresh_exempt(_order("STOP", stop_guard=True)) is False
    # its refresh cadence comes from the stop-guard staged branch, not the 5s default
    assert svc._refresh_after_seconds(_order("STOP", stop_guard=True)) == max(
        0.1, float(svc.settings.oms_stop_guard_refresh_stage_1_seconds)
    )
