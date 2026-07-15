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


# --------------------------------------------------------------------------- #
# Bug A follow-up — reverse-rejected native-guard arms are queued + retried on
# the periodic cadence (non-blocking), instead of only being tolerated.
# --------------------------------------------------------------------------- #
from uuid import uuid4  # noqa: E402

from project_mai_tai.broker_adapters.protocols import ExecutionReport  # noqa: E402


def _arm_service(*, reverse: bool) -> tuple[OmsRiskService, object, object]:
    svc = _bare_service()
    svc._native_guard_rearm_pending = {}
    strategy = SimpleNamespace(id=uuid4(), code="orb")
    broker_account = SimpleNamespace(id=uuid4(), name="live:orb")

    svc.store = SimpleNamespace(
        find_open_native_stop_guard_order=lambda *a, **k: None,  # no existing guard -> no cancel
        create_trade_intent=lambda *a, **k: SimpleNamespace(id=uuid4()),
    )
    svc._record_internal_risk_pass = lambda *a, **k: None

    async def _submit(request):
        if reverse:
            return [ExecutionReport(
                event_type="rejected", client_order_id=request.client_order_id,
                reason="Webull order rejected: ORDER_NOT_SUPPORT_REVERSE_OPTION (http 417)",
            )]
        return [ExecutionReport(event_type="accepted", client_order_id=request.client_order_id)]

    svc.broker_adapter = SimpleNamespace(submit_order=_submit)

    async def _record_order_reports(**_):
        return []

    svc._record_order_reports = _record_order_reports
    return svc, strategy, broker_account


def test_arm_reverse_reject_queues_pending_rearm(monkeypatch):
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)
    svc, strategy, broker_account = _arm_service(reverse=True)
    stop = _armed_stop()
    asyncio.run(svc._arm_or_rearm_native_stop_guard(
        session=SimpleNamespace(), strategy=strategy, broker_account=broker_account, stop=stop))
    key = svc._hard_stop_key(strategy.code, broker_account.name, stop.symbol)
    assert svc._native_guard_rearm_pending.get(key) == (strategy.id, broker_account.id)


def test_arm_success_clears_pending_rearm(monkeypatch):
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)
    svc, strategy, broker_account = _arm_service(reverse=False)
    stop = _armed_stop()
    key = svc._hard_stop_key(strategy.code, broker_account.name, stop.symbol)
    svc._native_guard_rearm_pending[key] = (strategy.id, broker_account.id)  # pre-seed
    asyncio.run(svc._arm_or_rearm_native_stop_guard(
        session=SimpleNamespace(), strategy=strategy, broker_account=broker_account, stop=stop))
    assert key not in svc._native_guard_rearm_pending, "a successful arm must clear the pending entry"


def test_retry_drops_pending_when_stop_is_gone(monkeypatch):
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)
    svc = _bare_service()
    svc._armed_hard_stops = {}  # the stop already closed
    key = ("orb", "live:orb", "LGPS")
    svc._native_guard_rearm_pending = {key: (uuid4(), uuid4())}
    asyncio.run(svc._retry_pending_native_guard_rearms())
    assert key not in svc._native_guard_rearm_pending, "a closed stop's pending re-arm must be dropped"


# --------------------------------------------------------------------------- #
# FALSE-FLAT (2026-07-15 ERNA naked position) — docs/false-flat-reconcile-design.md
#
# ORB rested a buy-stop -> FILLED 2 ERNA @ 9.47 09:33:17 -> the protective sell-STOP was
# reverse-rejected -> bid fell through the trail -> 3 closes failed -> the reconcile read the
# broker, got "flat" 61s after our own fill, and DELETED the armed stop while we held 2 real
# shares. The position went naked and the OMS was then structurally unable to close it (the
# sell clamps to virtual_position=0). The operator closed it by hand at ~-17.5%.
#
# TIME is the discriminator: an empty/absent read is ambiguous (genuine close vs silent read
# failure), so it is refused while the fill is fresh and honoured once it is not.
# --------------------------------------------------------------------------- #

def _flat_svc(positions, *, grace_secs=120, require_positive=True) -> OmsRiskService:
    svc = _bare_service()
    svc.settings = SimpleNamespace(
        oms_reconcile_require_positive_flat=require_positive,
        oms_reconcile_fresh_fill_grace_secs=grace_secs,
    )

    async def _positions(_name):
        return positions

    svc.broker_adapter = SimpleNamespace(list_account_positions=_positions)
    return svc


def _stop_armed_secs_ago(secs: float | None) -> ArmedHardStop:
    from datetime import UTC, datetime, timedelta
    armed_at = None if secs is None else datetime.now(UTC) - timedelta(seconds=secs)
    return ArmedHardStop(
        strategy_code="orb", broker_account_name="live:orb", symbol="ERNA",
        quantity=Decimal("2"), entry_price=Decimal("9.47"), stop_loss_pct=5.0,
        stop_price=Decimal("9.196"), quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
        trail_pct=5.0, high_water_mark=Decimal("9.68"), armed_at=armed_at,
    )


def test_erna_replay_fresh_fill_flat_read_never_clears_the_stop():
    """THE REGRESSION ANCHOR. The exact 2026-07-15 sequence: broker reports flat 61s after our
    own fill. Before the fix this returned True and the caller deleted the stop -> naked."""
    svc = _flat_svc([])                       # what Webull effectively gave us
    stop = _stop_armed_secs_ago(61)           # filled 09:33:17, read 09:34:18
    assert asyncio.run(svc._broker_position_is_flat(stop)) is False


def test_fresh_fill_grace_expires_so_a_genuine_out_of_band_close_still_clears():
    """The grace must not become a permanent block — #436 Bug C (the AGEN 181x phantom churn)
    must still be fixed. Past the grace, an empty read is honoured."""
    svc = _flat_svc([], grace_secs=120)
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(600))) is True


def test_rehydrated_stop_has_no_armed_at_and_still_reconciles():
    """F2 rehydrate leaves armed_at=None (a restored stop is by definition not fresh) -> no
    grace -> behaves exactly as before the fix."""
    svc = _flat_svc([])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(None))) is True


def test_read_error_is_unknown_and_never_clears():
    svc = _bare_service()
    svc.settings = SimpleNamespace(
        oms_reconcile_require_positive_flat=True, oms_reconcile_fresh_fill_grace_secs=120
    )

    async def _boom(_name):
        raise RuntimeError("broker unreachable")

    svc.broker_adapter = SimpleNamespace(list_account_positions=_boom)
    # even an OLD position must not be cleared on a failed read
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(600))) is False


def test_unparseable_quantity_is_unknown_and_never_clears():
    svc = _flat_svc([SimpleNamespace(symbol="ERNA", quantity="not-a-number")])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(600))) is False


def test_symbol_present_and_held_is_never_flat_even_when_stale():
    svc = _flat_svc([SimpleNamespace(symbol="ERNA", quantity=Decimal("2"))])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(600))) is False


def test_positive_zero_quantity_clears_even_inside_the_grace():
    """A POSITIVE confirmation (symbol present at qty 0) is unambiguous -- the broker is
    telling us about this symbol, not omitting it -- so the grace does not apply."""
    svc = _flat_svc([SimpleNamespace(symbol="ERNA", quantity=Decimal("0"))])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(5))) is True


def test_absent_from_a_healthy_read_is_flat_once_past_the_grace():
    svc = _flat_svc([SimpleNamespace(symbol="OTHER", quantity=Decimal("100"))])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(600))) is True


def test_absent_from_a_healthy_read_is_refused_while_the_fill_is_fresh():
    """A positions endpoint that lags a fresh fill omits the symbol while other holdings show
    -- the leading hypothesis for ERNA. Refuse it."""
    svc = _flat_svc([SimpleNamespace(symbol="OTHER", quantity=Decimal("100"))])
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(30))) is False


def test_rollback_flag_restores_pre_fix_semantics():
    """oms_reconcile_require_positive_flat=false must reproduce the OLD behaviour exactly,
    including the naked-position path -- it is a rollback lever, not a safety net."""
    svc = _flat_svc([], require_positive=False)
    assert asyncio.run(svc._broker_position_is_flat(_stop_armed_secs_ago(61))) is True


def test_v2_managed_exit_shares_the_same_guard():
    """The v2 CW managed-exit reconcile calls the SAME helper (service.py:1988). Fixing only
    ORB would be a half fix -- v2 is armed on this bug too."""
    from datetime import UTC, datetime, timedelta
    svc = _flat_svc([])
    fresh = datetime.now(UTC) - timedelta(seconds=30)
    old = datetime.now(UTC) - timedelta(seconds=900)
    assert asyncio.run(
        svc._broker_symbol_is_flat("live:schwab_1m_v2", "KUST", established_at=fresh)
    ) is False
    assert asyncio.run(
        svc._broker_symbol_is_flat("live:schwab_1m_v2", "KUST", established_at=old)
    ) is True
