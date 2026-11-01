"""Bound-row target capture is memory-only, not permission to sell."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select, update

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition
from project_mai_tai.oms import service as module
from project_mai_tai.oms.target_cache import BoundTargetRow
from tests.unit.test_edgeprot1_lane_b import drain, quote, setup
from tests.unit.test_v2_cw_managed_exit import ACCT, SYM, WEBULL_ACCT, _arm


def row(session, account=ACCT):
    return session.scalar(select(OmsManagedPosition).where(
        OmsManagedPosition.broker_account_name == account,
        OmsManagedPosition.status == "open"))


def test_create_publishes_only_after_commit_and_rollback_does_not_publish(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    assert {f.name for f in fields(BoundTargetRow)} == {
        "managed_row_id", "entry_order_id", "entry_price", "quantity"}
    with sf() as session:
        existing = row(session)
        session.delete(existing)
        session.commit()
    assert svc._target_row_cache.get((ACCT, SYM)) is None
    from tests.unit.managed_entry_fixtures import bind_managed_entry
    for commit in (False, True):
        with sf() as session:
            created = svc.store.create_managed_position(session, strategy_code="schwab_1m_v2",
                broker_account_name=ACCT, symbol=SYM, entry_price=Decimal("2.49"),
                quantity=120, entry_path="ATR Flip")
            bind_managed_entry(session, created)
            session.flush()
            assert svc._target_row_cache.get((ACCT, SYM)) is None
            session.expunge(created)
            assert row(session) is not None
            assert svc._target_row_cache.get((ACCT, SYM)) is None
            session.commit() if commit else session.rollback()
        assert (svc._target_row_cache.get((ACCT, SYM)) is not None) == commit


@pytest.mark.parametrize("commit", [False, True])
def test_uncommitted_reload_cannot_republish_a_modified_row(monkeypatch, commit):
    svc, sf, _ = setup(monkeypatch)
    with sf() as session:
        existing = row(session)
        existing.entry_price = Decimal("2.50")
        session.flush()
        session.expunge(existing)
        assert row(session).entry_price == Decimal("2.50")
        assert svc._target_row_cache.get((ACCT, SYM)) is None
        session.commit() if commit else session.rollback()
    assert svc._target_row_cache.get((ACCT, SYM)) is None
    svc._rehydrate_managed_v2_symbols()
    assert svc._target_row_cache.get((ACCT, SYM)).entry_price == Decimal("2.50" if commit else "2.49")


@pytest.mark.parametrize("field,value", [("current_quantity", 119), ("entry_price", Decimal("2.50")),
    ("entry_order_id", None), ("status", "closed"), ("peak_profit_pct", 3.0)])
def test_any_committed_row_write_drops_cache_until_next_load(monkeypatch, field, value):
    svc, sf, _ = setup(monkeypatch)
    bound = svc._target_row_cache.get((ACCT, SYM))
    with sf() as session:
        existing = row(session)
        setattr(existing, field, value)
        session.flush()
        assert svc._target_row_cache.get((ACCT, SYM)) is None
        session.commit()
    assert svc._target_row_cache.get((ACCT, SYM)) is None
    svc._rehydrate_managed_v2_symbols()
    refreshed = svc._target_row_cache.get((ACCT, SYM))
    assert refreshed is not bound
    if field in {"status", "entry_order_id"}:
        assert refreshed is None
    else:
        assert refreshed is not None


@pytest.mark.parametrize("bulk", [False, True])
def test_write_rollback_fences_old_decision_and_other_account_survives(monkeypatch, bulk):
    svc, sf, _ = setup(monkeypatch)
    _arm(svc, sf, account=WEBULL_ACCT, entry=2.49, qty=120)
    other = svc._target_row_cache.get((WEBULL_ACCT, SYM))
    svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
    with sf() as session:
        existing = row(session)
        if bulk:
            session.execute(update(OmsManagedPosition).where(OmsManagedPosition.id == existing.id)
                .values(current_quantity=119))
        else:
            existing.current_quantity = 119
            session.flush()
        assert svc._target_row_cache.get((ACCT, SYM)) is None
        assert svc._target_row_cache.get((WEBULL_ACCT, SYM)) is other
        session.rollback()
    with sf() as session:
        snapshot = svc._read_v2_managed_snapshot(session, ACCT, SYM, True)
    assert svc._take_cw_target_decision((ACCT, SYM), snapshot) is None


@pytest.mark.parametrize("seconds", [-6, 1])
def test_stale_and_future_quotes_do_not_capture(monkeypatch, seconds):
    svc, _, _ = setup(monkeypatch)
    svc._capture_cw_target_decision(ACCT, SYM, {
        "bid": 2.64, "received_at": datetime.now(UTC) + timedelta(seconds=seconds)})
    assert svc._cw_target_decisions == {}


def test_capture_uses_no_sql_and_keeps_first_cross_timestamp(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    def forbid_sql(*args):
        pytest.fail("target quote capture must not read the database")
    engine = sf.kw["bind"]
    event.listen(engine, "before_cursor_execute", forbid_sql)
    try:
        svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
        first = svc._cw_target_decisions[(ACCT, SYM)]
        svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.70, "received_at": datetime.now(UTC)})
        assert svc._cw_target_decisions[(ACCT, SYM)] is first
    finally:
        event.remove(engine, "before_cursor_execute", forbid_sql)


def test_expired_captured_decision_is_not_consumed(monkeypatch):
    svc, sf, _ = setup(monkeypatch)
    now = datetime.now(UTC)
    svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.64, "received_at": now})
    with sf() as session:
        snapshot = svc._read_v2_managed_snapshot(session, ACCT, SYM, True)
    monkeypatch.setattr(module, "utcnow", lambda: now + timedelta(seconds=6))
    assert svc._take_cw_target_decision((ACCT, SYM), snapshot) is None


@pytest.mark.asyncio
async def test_cold_quote_worker_does_not_invent_target_decision_after_db_read(monkeypatch):
    svc, sf, broker = setup(monkeypatch)
    svc._target_row_cache.drop_row(svc._target_row_cache.get((ACCT, SYM)).managed_row_id)
    await svc._handle_quote_tick_event(quote("2.64"))
    await drain(svc)
    assert broker.requests == []
    svc._rehydrate_managed_v2_symbols()
    await svc._handle_quote_tick_event(quote("2.64"))
    await drain(svc)
    assert len(broker.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flip", [False, True])
async def test_native_standdown_confirmed_during_read_suppresses_peak(monkeypatch, flip):
    svc, _, broker = setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._run_db
    async def delayed(work, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(work, **kwargs)
    monkeypatch.setattr(svc, "_run_db", delayed)
    await svc._handle_quote_tick_event(quote("2.52"))
    await entered.wait()
    await svc._handle_quote_tick_event(quote("2.64"))
    released_rows = []
    async def unconfirmed(acct, symbol, *, expected_row_id):
        released_rows.append(expected_row_id)
        return "unconfirmed"
    monkeypatch.setattr(svc, "_release_native_oco_for_cw_flip", unconfirmed)
    bound = svc._target_row_cache.get((ACCT, SYM))
    if flip:
        svc._arm_cw_flip_pending((ACCT, SYM), bar_time_ms=int(datetime.now(UTC).timestamp() * 1000),
            managed_row_id=bound.managed_row_id)
        await svc._handle_quote_tick_event(quote("2.50"))
    monkeypatch.setattr(svc, "_native_oco_stand_down_active", lambda *args: True)
    release.set()
    await drain(svc)
    assert broker.requests == []
    assert released_rows == ([bound.managed_row_id] if flip else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("safety,bid", [("CW_HARD_STOP", "2.20"), ("CW_FLIP", "2.50"), ("CW_FLOOR", "2.50")])
async def test_latest_safety_during_row_wait_wins_over_captured_peak(monkeypatch, account, safety, bid):
    svc, _, broker = setup(monkeypatch, account, session="PM")
    bound = svc._target_row_cache.get((account, SYM))
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._run_db
    async def delayed(work, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(work, **kwargs)
    monkeypatch.setattr(svc, "_run_db", delayed)
    await svc._handle_quote_tick_event(quote("2.52"))
    await entered.wait()
    await svc._handle_quote_tick_event(quote("2.64"))
    assert (account, SYM) in svc._cw_target_decisions
    if safety == "CW_FLIP":
        svc._arm_cw_flip_pending((account, SYM), bar_time_ms=int(datetime.now(UTC).timestamp() * 1000),
            managed_row_id=bound.managed_row_id)
    elif safety == "CW_FLOOR":
        svc._cw_floor_exit_enabled = True
        svc._cw_floor_armed.add((account, SYM))
        svc._cw_floor_pct = 1.0
    await svc._handle_quote_tick_event(quote(bid))
    release.set()
    await drain(svc)
    request, = broker.requests
    assert request.reason.endswith(safety)
    assert "cw_target_bound_row_id" not in request.metadata
    await svc._handle_quote_tick_event(quote(bid))
    await drain(svc)
    assert broker.requests == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_row_replacement_during_wait_does_not_sell_new_episode(monkeypatch, account):
    svc, sf, broker = setup(monkeypatch, account)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._run_db
    async def delayed(work, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(work, **kwargs)
    monkeypatch.setattr(svc, "_run_db", delayed)
    await svc._handle_quote_tick_event(quote("2.52"))
    await entered.wait()
    await svc._handle_quote_tick_event(quote("2.64"))
    with sf.begin() as session:
        session.delete(row(session, account))
    _arm(svc, sf, account=account, entry=2.50, qty=120)
    release.set()
    await drain(svc)
    assert broker.requests == []  # Even a new row whose target is below 2.64 needs its own quote.


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("current_quantity", 119), ("entry_price", Decimal("2.50")),
    ("entry_order_id", None), ("status", "closed")])
async def test_sell_rechecks_bound_row_after_reservation_wait(monkeypatch, field, value):
    svc, sf, broker = setup(monkeypatch)
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(ACCT, SYM)])
    async def changed(**kwargs):
        from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
        with sf.begin() as session:
            setattr(row(session), field, value)
        return ExitPairReleaseResult(outcome="no_reservation")
    monkeypatch.setattr(svc, "_release_exit_reservation_before_close", changed)
    with sf() as session:
        result = await svc._emit_v2_managed_sell(session, row(session), intent_type="close",
            quantity=120, reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, confirmation_context=context)
    assert not result and broker.requests == []


@pytest.mark.asyncio
async def test_rejected_webull_target_is_not_corrected_to_market(monkeypatch):
    svc, sf, broker = setup(monkeypatch, WEBULL_ACCT)
    svc._capture_cw_target_decision(WEBULL_ACCT, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(WEBULL_ACCT, SYM)])
    async def reject(request):
        broker.requests.append(request)
        return [ExecutionReport(event_type="rejected", origin="broker",
            client_order_id=request.client_order_id, symbol=SYM, side="sell", intent_type="close",
            reason="controlled CORRECT ORDER TYPE rejection")]
    monkeypatch.setattr(broker, "submit_order", reject)
    with sf() as session:
        await svc._emit_v2_managed_sell(session, row(session, WEBULL_ACCT), intent_type="close",
            quantity=120, reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, confirmation_context=context)
    request, = broker.requests
    assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"


@pytest.mark.asyncio
async def test_schwab_rejected_target_retry_rechecks_entry_binding(monkeypatch):
    svc, sf, broker = setup(monkeypatch)
    with sf.begin() as session:
        session.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCT)).provider = "schwab"
    svc._capture_cw_target_decision(ACCT, SYM, {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(ACCT, SYM)])
    async def reject(request):
        broker.requests.append(request)
        return [ExecutionReport(event_type="rejected", origin="broker", symbol=SYM,
            client_order_id=request.client_order_id, side="sell", intent_type="close",
            reason="controlled oversold rejection")]
    async def release(*args, **kwargs):
        with sf.begin() as session:
            row(session).entry_price = Decimal("2.50")
        return "released"
    monkeypatch.setattr(broker, "submit_order", reject)
    monkeypatch.setattr(svc, "_release_native_oco_for_cw_flip", release)
    with sf() as session:
        await svc._emit_v2_managed_sell(session, row(session), intent_type="close",
            quantity=120, reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, confirmation_context=context)
    assert len(broker.requests) == 1
