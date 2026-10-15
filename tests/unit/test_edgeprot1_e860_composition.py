"""Controlled composition waits, not historical MNDR arrival or broker evidence."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.db.models import VirtualPosition
from project_mai_tai.oms import service as module
from project_mai_tai.settings import Settings
from tests.unit import test_edgeprot1_lane_b as lane_b
from tests.unit.test_edgeprot1_lane_b import drain, quote, setup
from tests.unit.test_edgeprot1_target_cache import row
from tests.unit.test_v2_cw_managed_exit import ACCT, SYM, WEBULL_ACCT


def bind_clock(monkeypatch, initial):
    clock = {"now": initial}
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "utcnow", lambda: clock["now"])
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_controlled_thirteen_second_protection_wait_is_late_submission_not_decision(monkeypatch, account):
    svc, _, broker = setup(monkeypatch, account)
    clock = bind_clock(monkeypatch, datetime(2026, 10, 12, 13, 30, 14, tzinfo=UTC))
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    entered, release = asyncio.Event(), asyncio.Event()
    if account == WEBULL_ACCT:
        original = svc._prepare_confirmation_webull_leg
        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(svc, "_prepare_confirmation_webull_leg", wait)
    else:
        async def wait(**kwargs):
            entered.set()
            await release.wait()
            return ExitPairReleaseResult(outcome="no_reservation")
        monkeypatch.setattr(svc, "_release_exit_reservation_before_close", wait)
    target_at = clock["now"]
    await svc._handle_quote_tick_event(quote("2.64", at=target_at))
    await entered.wait()
    assert broker.requests == []
    clock["now"] += timedelta(seconds=13)  # Virtual phase timing, never a performance sample.
    release.set()
    await drain(svc)
    request, = broker.requests
    assert request.metadata["cw_decided_at"] == target_at.isoformat(timespec="microseconds")
    submitted = datetime.fromisoformat(request.metadata["cw_submitted_at"])
    assert submitted - target_at == timedelta(seconds=13)
    assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"


@pytest.mark.asyncio
async def test_controlled_thirteen_second_intake_delay_hits_named_five_second_guard(monkeypatch):
    svc, _, broker = setup(monkeypatch)
    target_at = datetime(2026, 10, 12, 13, 30, 14, tzinfo=UTC)
    bind_clock(monkeypatch, target_at + timedelta(seconds=13))
    await svc._handle_quote_tick_event(quote("2.64", at=target_at))
    await drain(svc)
    assert broker.requests == []
    assert not svc._cw_target_decisions


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("change", ["entry_price", "entry_order_id", "quantity", "owned_zero", "owned_smaller"])
async def test_bound_target_and_E_owned_quantity_share_final_worker_read(monkeypatch, account, change):
    svc, sf, broker = setup(monkeypatch, account)
    with sf.begin() as session:
        strategy = svc.store.ensure_strategy(session, "schwab_1m_v2")
        broker_account = svc.store.ensure_broker_account(session, account, provider="simulated", environment="paper")
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=broker_account.id,
                                    symbol=SYM, quantity=Decimal(120), average_price=Decimal("2.49")))
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    svc._capture_cw_target_decision(account, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(account, SYM)])
    async def changed(**kwargs):
        with sf.begin() as session:
            managed = row(session, account)
            if change == "entry_price":
                managed.entry_price = Decimal("2.50")
            elif change == "entry_order_id":
                managed.entry_order_id = None
            elif change == "quantity":
                managed.current_quantity = 119
            else:
                owned = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == SYM))
                owned.quantity = Decimal(0 if change == "owned_zero" else 119)
        return ExitPairReleaseResult(outcome="no_reservation")
    monkeypatch.setattr(svc, "_release_exit_reservation_before_close", changed)
    with sf() as session:
        result = await svc._emit_v2_managed_sell(session, row(session, account), intent_type="close",
            quantity=120, reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, confirmation_context=context)
    if change == "owned_smaller":
        request, = broker.requests
        assert request.quantity == 119 and result.submitted_quantity == 119
        assert request.metadata["limit_price"] == "2.62"
    else:
        assert not result and result.ownership_refused and broker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("session", ["AM", "PM"])
async def test_all_on_calls_unchanged_four_transient_bodies_with_coherent_elapsed_clock(monkeypatch, account, session):
    initial, started = datetime(2026, 10, 12, 13, 0, tzinfo=UTC), monotonic()
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            now = initial + timedelta(seconds=monotonic() - started)
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    # Wall-relative, not frozen: the original decided_at < row-read-return assertion stays strict.
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "utcnow", lambda: Clock.now(UTC))
    monkeypatch.setattr(lane_b, "datetime", Clock)
    original_setup = lane_b.setup
    def configured_setup(monkeypatch, *args, **kwargs):
        initial_service, sf, broker = original_setup(monkeypatch, *args, **kwargs)
        flags = Path(__file__).resolve().parents[2] / "docs/review-artifacts/edgeprot1-lane-b/all-on-e860-controlled.env"
        settings = Settings(_env_file=flags, strategy_schwab_1m_v2_account_name=ACCT,
                            strategy_schwab_1m_v2_webull_account_name=WEBULL_ACCT)
        svc = module.OmsRiskService(settings, redis_client=initial_service.redis, session_factory=sf, broker_adapter=broker)
        svc._rehydrate_managed_v2_symbols()
        monkeypatch.setattr(svc, "_market_is_fillable", lambda: True)
        assert settings.oms_v2_eod_oco_transition_enabled and settings.oms_v2_exit_release_reservation_enabled
        assert settings.strategy_schwab_1m_v2_fresh_sell_owner_release_enabled
        assert settings.strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled
        assert settings.strategy_schwab_1m_v2_removed_wait_clear_rules_enabled
        assert not svc._v2_eod_oco_transition_due(initial)
        assert svc._v2_eod_oco_transition_due(datetime(2026, 10, 12, 20, 0, tzinfo=UTC))
        return svc, sf, broker
    monkeypatch.setattr(lane_b, "setup", configured_setup)
    await lane_b.test_transient_target_cross_captured_before_slow_row_read_returns(monkeypatch, account, session)
    assert monotonic() - started >= .05


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_all_on_after_sixteen_unknown_handover_is_not_bypassed(monkeypatch, account):
    svc, _, broker = setup(monkeypatch, account, session="PM")
    svc.settings.oms_v2_eod_oco_transition_enabled = True
    clock = bind_clock(monkeypatch, datetime(2026, 10, 12, 20, 0, tzinfo=UTC))
    assert svc._v2_eod_oco_transition_due()
    await svc._handle_quote_tick_event(quote("2.64", at=clock["now"]))
    await drain(svc)
    assert broker.requests == []  # Missing broker handover evidence is UNKNOWN, never a zero assumption.
