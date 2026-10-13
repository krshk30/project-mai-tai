"""O3 controlled ownership probes; recorded MI parents are labelled separately."""
from datetime import UTC, datetime
from decimal import Decimal
from threading import get_ident
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult
from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit import test_ownmix1_entry_binding as mi
from tests.unit import test_confirmation_exit_fanout as confirmation
from tests.unit.managed_entry_fixtures import bind_managed_entry
from tests.unit.test_v2_exit_paper_isolation import _FakeRedis, _make_sf

replay = mi.replay
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def setup(monkeypatch, account, *, managed=3, owned=1, fill=False, aggregate=3):
    sessions = _make_sf()
    adapter = AsyncMock()

    async def submit(request):
        return [ExecutionReport(
            event_type="filled" if fill else "accepted",
            client_order_id=request.client_order_id, broker_order_id="CONTROLLED-close",
            broker_fill_id="CONTROLLED-fill" if fill else None,
            symbol=request.symbol, side="sell", intent_type=request.intent_type,
            quantity=request.quantity, filled_quantity=request.quantity if fill else Decimal(0),
            fill_price=Decimal("10") if fill else None, origin="broker",
        )]

    adapter.submit_order.side_effect = submit
    service = OmsRiskService(
        Settings(_env_file=None, oms_v2_exit_management_enabled=True,
                 oms_v2_exit_close_on_fill_enabled=True,
                 oms_v2_exit_release_reservation_enabled=True,
                 strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
                 strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1],
                 strategy_schwab_1m_v2_broker_provider="schwab",
                 strategy_schwab_1m_v2_dual_broker_fanout_enabled=True),
        redis_client=_FakeRedis(), session_factory=sessions, broker_adapter=adapter,
    )
    with sessions.begin() as session:
        v2 = service.store.ensure_strategy(session, "schwab_1m_v2")
        orb = service.store.ensure_strategy(session, "orb_schwab")
        broker = service.store.ensure_broker_account(
            session, account, provider="schwab" if account == ACCOUNTS[0] else "webull",
            environment="test",
        )
        other = service.store.ensure_broker_account(
            session, "CONTROLLED-other-account", provider="simulated", environment="test",
        )
        session.add_all([
            VirtualPosition(strategy_id=orb.id, broker_account_id=broker.id,
                            symbol="MI", quantity=Decimal(2), average_price=Decimal("10")),
            VirtualPosition(strategy_id=v2.id, broker_account_id=other.id,
                            symbol="MI", quantity=Decimal(99), average_price=Decimal("10")),
            VirtualPosition(strategy_id=v2.id, broker_account_id=broker.id,
                            symbol="OTHER", quantity=Decimal(99), average_price=Decimal("10")),
            AccountPosition(broker_account_id=broker.id, symbol="MI",
                            quantity=Decimal(aggregate), average_price=Decimal("10")),
        ])
        if owned is not None:
            session.add(VirtualPosition(
                strategy_id=v2.id, broker_account_id=broker.id, symbol="MI",
                quantity=Decimal(owned), average_price=Decimal("10"),
            ))
        row = service.store.create_managed_position(
            session, strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="MI", entry_price=Decimal("10"), quantity=managed, entry_path="ATR Flip",
        )
        bind_managed_entry(session, row)
        row_id = row.id
        position = service._hydrate_v2_position(row)
        v2_id, broker_id = v2.id, broker.id
    service._managed_v2_symbols.add((account, "MI"))
    service._v2_eod_handover_ready = AsyncMock(return_value=True)
    service._release_native_oco_for_cw_flip = AsyncMock(return_value="released")
    service._release_exit_reservation_before_close = AsyncMock(
        return_value=ExitPairReleaseResult(outcome="released")
    )
    service._cw_exit_enabled = True
    service._cw_floor_exit_enabled = False
    service._cw_target_pct = 5.0
    service._cw_stop_pct = 8.0
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(service_module, "_extended_hours_session", lambda now=None: None)
    return service, sessions, adapter, row_id, position, v2_id, broker_id


async def emit(service, account, row_id, position, *, kind="HARD", close_on_fill=True):
    return await service._emit_v2_exit_on_loop_claimed(
        account, "MI", position, 10.0, kind=kind, reference_price=10.5,
        reason="oms_v2_managed_exit:CW_TARGET", bid=10.5, close_on_fill=close_on_fill,
        expected_managed_row_id=str(row_id), sell_qty=3, level="L1",
    )


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("tag,bid", [("CW_TARGET", 10.5), ("CW_HARD_STOP", 9.1), ("CW_FLIP", 10.)])
@pytest.mark.parametrize("managed,owned,expected", [(3, 1, 1), (3, 3, 3), (2, 4, 2)])
@pytest.mark.asyncio
async def test_real_v2_evaluator_owns_quantity_and_preserves_exit_shape(
    monkeypatch, account, tag, bid, managed, owned, expected,
):
    service, sessions, adapter, row_id, _, _, _ = setup(
        monkeypatch, account, managed=managed, owned=owned,
        aggregate=0,  # The shared account book is NOT the authority for this cap.
    )
    if tag == "CW_FLIP":
        service._arm_cw_flip_pending(
            (account, "MI"), bar_time_ms=int(datetime.now(UTC).timestamp() * 1000) - 1,
            managed_row_id=str(row_id),
        )
    service._latest_quotes_by_symbol["MI"] = {
        "bid": bid, "ask": bid + .01, "received_at": datetime.now(UTC),
    }
    await service._evaluate_v2_managed_exit(account, "MI")
    adapter.submit_order.assert_awaited_once()
    request = adapter.submit_order.call_args.args[0]
    assert request.quantity == expected
    assert request.strategy_code == "schwab_1m_v2" and request.broker_account_name == account
    assert request.reason == f"oms_v2_managed_exit:{tag}"
    assert request.time_in_force == "day"
    if tag == "CW_TARGET":
        assert request.order_type == "limit" and Decimal(request.metadata["limit_price"]) >= Decimal("10.5")
    else:
        assert request.order_type == "market"
    assert request.metadata["session"] == "NORMAL"
    with sessions() as session:
        assert session.get(OmsManagedPosition, row_id).current_quantity == managed
        intent = session.scalar(select(TradeIntent).where(TradeIntent.side == "sell"))
        assert intent.quantity == expected


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("change", ["ledger_decrease", "ledger_zero", "managed_decrease", "closed", "replacement", "foreign"])
@pytest.mark.asyncio
async def test_cap_revalidates_after_reservation_await(monkeypatch, account, change):
    service, sessions, adapter, row_id, position, v2_id, broker_id = setup(
        monkeypatch, account, owned=3,
    )

    async def release(**_kwargs):
        with sessions.begin() as session:
            row = session.get(OmsManagedPosition, row_id)
            ledger = service.store.get_virtual_position(
                session, strategy_id=v2_id, broker_account_id=broker_id, symbol="MI",
            )
            if change.startswith("ledger"):
                ledger.quantity = 1 if change == "ledger_decrease" else 0
            elif change == "managed_decrease":
                row.current_quantity = 1
            elif change == "foreign":
                row.strategy_code = "orb_schwab"
            else:
                service.store.close_managed_position(session, row)
                if change == "replacement":
                    service.store.create_managed_position(
                        session, strategy_code="schwab_1m_v2", broker_account_name=account,
                        symbol="MI", entry_price=Decimal("10"), quantity=3,
                    )
        return ExitPairReleaseResult(outcome="released")

    service._release_exit_reservation_before_close.side_effect = release
    outcome = await emit(service, account, row_id, position, close_on_fill=False)
    if change in {"ledger_decrease", "managed_decrease"}:
        assert adapter.submit_order.call_args.args[0].quantity == 1
        with sessions() as session:
            current = session.get(OmsManagedPosition, row_id)
            assert current.status == "open"
            assert current.current_quantity == (1 if change == "managed_decrease" else 3)
    else:
        assert outcome == "refused"
        adapter.submit_order.assert_not_called()
        with sessions() as session:
            assert session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all() == []
            if change in {"ledger_zero", "foreign"}:
                assert session.get(OmsManagedPosition, row_id).status == "open"


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.asyncio
async def test_explicit_zero_never_uses_foreign_or_account_shares(monkeypatch, account):
    service, sessions, adapter, row_id, position, _, _ = setup(monkeypatch, account, owned=0)
    assert await emit(service, account, row_id, position) == "refused"
    adapter.submit_order.assert_not_called()
    with sessions() as session:
        assert session.get(OmsManagedPosition, row_id).status == "open"


@pytest.mark.asyncio
async def test_missing_ledger_is_disclosed_managed_only_not_broker_proof(monkeypatch):
    service, _, adapter, row_id, position, _, _ = setup(monkeypatch, ACCOUNTS[0], owned=None)
    warning = Mock(wraps=service.logger.warning)
    monkeypatch.setattr(service.logger, "warning", warning)
    await emit(service, ACCOUNTS[0], row_id, position)
    assert adapter.submit_order.call_args.args[0].quantity == 3
    assert warning.call_args.args[-1] == "managed_only_no_ledger"


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("close_on_fill", [True, False])
@pytest.mark.asyncio
async def test_capped_scale_books_only_submitted_quantity(monkeypatch, account, close_on_fill):
    service, sessions, adapter, row_id, position, _, _ = setup(monkeypatch, account)
    await emit(service, account, row_id, position, kind="SCALE", close_on_fill=close_on_fill)
    assert adapter.submit_order.call_args.args[0].quantity == 1
    assert position.quantity == 2
    with sessions() as session:
        assert session.get(OmsManagedPosition, row_id).current_quantity == 3


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.asyncio
async def test_foreign_managed_row_cannot_release_reservation(monkeypatch, account):
    service, sessions, adapter, row_id, position, _, _ = setup(monkeypatch, account)
    with sessions.begin() as session:
        session.get(OmsManagedPosition, row_id).strategy_code = "orb_schwab"
    assert await emit(service, account, row_id, position) == "refused"
    adapter.submit_order.assert_not_called()
    service._release_exit_reservation_before_close.assert_not_called()


@pytest.mark.asyncio
async def test_quantity_read_is_off_loop_and_only_runs_for_triggered_exit(monkeypatch):
    service, _, adapter, _, _, _, _ = setup(monkeypatch, ACCOUNTS[0])
    loop_thread, read_threads = get_ident(), []
    original = service.store.get_virtual_position

    def read(*args, **kwargs):
        read_threads.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "get_virtual_position", read)
    service._latest_quotes_by_symbol["MI"] = {
        "bid": 10., "ask": 10.01, "received_at": datetime.now(UTC),
    }
    await service._evaluate_v2_managed_exit(ACCOUNTS[0], "MI")
    assert read_threads == []
    adapter.submit_order.assert_not_called()
    service._latest_quotes_by_symbol["MI"]["bid"] = 10.5
    await service._evaluate_v2_managed_exit(ACCOUNTS[0], "MI")
    assert read_threads and all(thread != loop_thread for thread in read_threads)
    adapter.submit_order.assert_awaited_once()


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("reservation", ["reserved", "unanswerable", "resolved_by_fill"])
@pytest.mark.asyncio
async def test_target_reservation_stand_down_is_not_bypassed(monkeypatch, account, reservation):
    service, _, adapter, row_id, position, _, _ = setup(monkeypatch, account)
    service._release_exit_reservation_before_close.return_value = ExitPairReleaseResult(
        outcome=reservation,
    )
    assert await emit(service, account, row_id, position) == "refused"
    adapter.submit_order.assert_not_called()


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.asyncio
async def test_existing_retry_never_reuses_quantity_after_ownership_changes(monkeypatch, account):
    service, sessions, adapter, row_id, position, v2_id, broker_id = setup(monkeypatch, account)
    service._reserve1_original_close_rejected = AsyncMock(return_value=True)

    async def reject(request):
        if account == ACCOUNTS[1]:
            with sessions.begin() as session:
                service.store.get_virtual_position(
                    session, strategy_id=v2_id, broker_account_id=broker_id, symbol="MI",
                ).quantity = 0
        return [ExecutionReport(
            event_type="rejected", client_order_id=request.client_order_id,
            symbol="MI", side="sell", intent_type="close", quantity=request.quantity,
            reason="oversold" if account == ACCOUNTS[0] else "ILLEGAL_PARAMETER",
            metadata={"webull_error_code": "ILLEGAL_PARAMETER"}, origin="broker",
        )]

    async def release(*_args, **_kwargs):
        with sessions.begin() as session:
            service.store.get_virtual_position(
                session, strategy_id=v2_id, broker_account_id=broker_id, symbol="MI",
            ).quantity = 0
        return "released"

    service._release_native_oco_for_cw_flip.side_effect = release
    adapter.submit_order.side_effect = reject
    with sessions() as session:
        events = await service._emit_v2_managed_sell(
            session, session.get(OmsManagedPosition, row_id), intent_type="close", quantity=3,
            reference_price=10.5, reason="oms_v2_managed_exit:CONFIRMATION_EXIT", bid=10.5,
            confirmation_context={"session": "NORMAL"},
        )
        session.commit()
    assert adapter.submit_order.await_count == 1
    assert adapter.submit_order.call_args.args[0].quantity == 1
    assert events.terminal_events[0].payload.status == "rejected"


@pytest.mark.asyncio
async def test_recorded_mi_orb_two_and_controlled_v2_180_sell_only_own_shares(replay, monkeypatch):
    service, sessions, _, _ = replay
    with sessions.begin() as session:
        v2, orb = mi._entry(session), mi._entry(session, mi.ORB_COID)
        session.add_all([
            VirtualPosition(strategy_id=v2.strategy_id, broker_account_id=v2.broker_account_id,
                            symbol="MI", quantity=180, average_price=Decimal("3.0498")),
            VirtualPosition(strategy_id=orb.strategy_id, broker_account_id=orb.broker_account_id,
                            symbol="MI", quantity=2, average_price=Decimal("3.0498")),
            AccountPosition(broker_account_id=v2.broker_account_id, symbol="MI",
                            quantity=182, average_price=Decimal("3.0498")),
        ])
        row = mi._row(service, session)
        row_id, position = row.id, service._hydrate_v2_position(row)
    # This child-fill body is recorded MI evidence, not a newly executed live order.
    await service._poll_orb_schwab_child_exits()

    async def submit(request):
        return [ExecutionReport(
            event_type="filled", client_order_id=request.client_order_id,
            broker_order_id="CONTROLLED-v2-close", broker_fill_id="CONTROLLED-v2-fill",
            symbol="MI", side="sell", intent_type="close", quantity=request.quantity,
            filled_quantity=request.quantity, fill_price=Decimal("3.05"), origin="broker",
        )]

    service.broker_adapter.submit_order = AsyncMock(side_effect=submit)
    service._v2_eod_handover_ready = AsyncMock(return_value=True)
    monkeypatch.setattr(service_module, "_extended_hours_session", lambda now=None: None)
    assert await service._emit_v2_exit_on_loop_claimed(
        mi.ACCT, "MI", position, 3.0498, kind="HARD", reference_price=3.05,
        reason="oms_v2_managed_exit:CW_TARGET", bid=3.05, close_on_fill=True,
        expected_managed_row_id=str(row_id),
    ) == "closed"
    assert service.broker_adapter.submit_order.call_args.args[0].quantity == 180
    with sessions() as session:
        fills = session.scalars(select(Fill).order_by(Fill.quantity)).all()
        assert [fill.quantity for fill in fills] == [2, 180]
        assert [session.get(BrokerOrder, fill.order_id).strategy_id for fill in fills] == [
            mi._entry(session, mi.ORB_COID).strategy_id, mi._entry(session).strategy_id,
        ]
        assert session.get(OmsManagedPosition, row_id).status == "closed"


@pytest.mark.asyncio
async def test_release_stays_durable_when_ownership_read_fails(monkeypatch):
    service, sessions, clock = confirmation._uncovered_service(monkeypatch)

    class SessionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock["now"]
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    # The EOD guard uses datetime.now; keep it on the same clock as utcnow.
    monkeypatch.setattr(service_module, "datetime", SessionClock)
    assert not service._v2_eod_oco_transition_due()
    confirmation.set_protect_base(
        service, sessions, confirmation.WEBULL, confirmation.SYMBOL, "known-protect-base",
    )
    with sessions() as session:
        row = service.store.get_open_managed_position(
            session, broker_account_name=confirmation.WEBULL, symbol=confirmation.SYMBOL,
        )
        row_id, position = row.id, service._hydrate_v2_position(row)
    service._v2_managed_sell_owned_quantity = AsyncMock(
        side_effect=RuntimeError("CONTROLLED ownership reader failure"),
    )
    assert await service._emit_v2_exit_on_loop_claimed(
        confirmation.WEBULL, confirmation.SYMBOL, position, 10., kind="HARD",
        reference_price=9.4, reason="oms_v2_managed_exit:CW_HARD_STOP", bid=9.4,
        close_on_fill=True, expected_managed_row_id=str(row_id),
    ) == "refused"
    assert len(service.broker_adapter.cancel_pair_calls) == 1
    assert service.broker_adapter.submitted == []
    fresh = confirmation._restarted(service, sessions)
    assert await fresh._check_webull_uncovered_shares() == 0
    clock["now"] += confirmation.timedelta(seconds=31)
    assert await fresh._check_webull_uncovered_shares() == 1
    assert confirmation._uncovered_incidents(sessions)[0]["cause"] == "pair_released_not_sold"
    with sessions() as session:
        assert session.get(OmsManagedPosition, row_id).status == "open"
    clock["now"] = datetime(2026, 9, 21, 20, 0, tzinfo=UTC)
    assert service._v2_eod_oco_transition_due()


@pytest.mark.asyncio
async def test_first_malformed_refusal_stays_durable_if_retry_ownership_read_fails(monkeypatch):
    adapter = confirmation._FanoutAdapter()
    adapter.submit_results.append([confirmation.lgps_133416_malformed_client_order_id_reject()])
    service, sessions = confirmation._service(fanout=True, adapter=adapter)
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    original = service._v2_managed_sell_owned_quantity
    reads = 0

    async def read(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise RuntimeError("CONTROLLED retry ownership reader failure")
        return await original(**kwargs)

    service._v2_managed_sell_owned_quantity = read
    with pytest.raises(RuntimeError, match="CONTROLLED retry ownership reader failure"):
        with sessions() as session:
            row = service.store.get_open_managed_position(
                session, broker_account_name=confirmation.WEBULL, symbol=confirmation.SYMBOL,
            )
            await service._emit_v2_managed_sell(
                session, row, intent_type="close", quantity=1, reference_price=9.9,
                reason="oms_v2_managed_exit:CONFIRMATION_EXIT", bid=9.9,
                confirmation_context={"session": "NORMAL"},
            )
    assert len(adapter.submitted) == 1
    with sessions() as session:
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
        assert len(orders) == 1 and orders[0].status == "rejected"
        assert orders[0].client_order_id == adapter.submitted[0].client_order_id
        intent = session.get(TradeIntent, orders[0].intent_id)
        assert intent is not None and intent.status == "rejected"
        assert session.scalars(select(Fill)).all() == []
