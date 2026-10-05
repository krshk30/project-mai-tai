"""Complete-review F1/F2/U1-U4 and additive-schema rollback proofs."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, SystemIncident
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import _EXIT_FETCH_FAILED, _PositionRead, _ConfirmationFanoutDecision
from tests.unit import test_ownmix1_entry_binding as entry_tests
from tests.unit.test_ownmix1_entry_binding import ACCT, ORB_COID, ROWS, V2_COID, _detail, _entry, _row

replay = entry_tests.replay

WEBULL = "live:orb"
WEB_COID = ROWS["orders"][2]["client_order_id"]


@pytest.fixture(autouse=True)
def owned_clock(monkeypatch):
    monkeypatch.setattr(service_module, "utcnow", lambda: datetime(2026, 10, 5, 13, 40, tzinfo=UTC))
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)


@pytest.mark.parametrize("account", [ACCT, WEBULL])
@pytest.mark.parametrize("path", ["reject", "no_bid"])
@pytest.mark.parametrize("proof", ["unbound", "foreign_child", "own_quantity_mismatch", "transient"])
@pytest.mark.asyncio
async def test_f1_confirmed_flat_closes_exact_episode_without_unproven_fill(
    replay, monkeypatch, caplog, account, path, proof
):
    service, sessions, _, _ = replay
    coid = V2_COID if account == ACCT else WEB_COID
    with sessions() as session:
        row, entry = _row(service, session, account), _entry(session, coid)
        row_id = row.id
        entry.payload = {**entry.payload, "native_oco_bracket": "true"}
        detail = _detail(str(entry.broker_order_id), coid, str(row.current_quantity + 1))
        if proof == "unbound":
            row.entry_order_id = row.entry_client_order_id = None
            detail = None
        elif proof == "foreign_child":
            detail = _detail("1008165370304", ORB_COID, "2")
        elif proof == "transient":
            detail = _EXIT_FETCH_FAILED
        session.commit()

    async def fetch(*_args, **_kwargs):
        return detail

    async def flat(*_args, **_kwargs):
        return _PositionRead.FLAT_CONFIRMED

    monkeypatch.setattr(service, "_fetch_oco_exit_detail", fetch)
    monkeypatch.setattr(service, "_broker_symbol_position_state", flat)
    service._managed_v2_symbols.add((account, "MI"))
    if path == "reject":
        service._v2_exit_close_failures[(account, "MI")] = service._V2_EXIT_RECONCILE_AFTER_FAILURES - 1
        with sessions() as session:
            assert await service._v2_close_reconcile_flat(session, account, "MI", session.get(OmsManagedPosition, row_id))
            session.commit()
    else:
        assert await service._close_broker_flat_phantom_managed_row(account, "MI", expected_row_id=str(row_id))
        assert not await service._close_broker_flat_phantom_managed_row(account, "MI", expected_row_id=str(row_id))
    with sessions() as session:
        row = session.get(OmsManagedPosition, row_id)
        assert row.status == "closed" and row.current_quantity == 0
        assert session.scalars(select(Fill)).all() == []
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].status == "open"
        payload = incidents[0].payload
        assert payload["source"] == "oco_exit_fill_unrecorded"
        assert payload["managed_row_id"] == str(row_id) and payload["broker_flat"]
        assert "owned_entry_coid" in payload and "candidate_entry_coid" in payload
    lines = [r.message for r in caplog.records if "ENTRY-OWNERSHIP-MISMATCH" in r.message]
    assert len(lines) == 1
    assert (account, "MI") not in service._managed_v2_symbols


@pytest.mark.asyncio
async def test_f1_transient_child_fetch_without_flat_still_defers(replay, monkeypatch):
    service, sessions, _, _ = replay

    async def fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    monkeypatch.setattr(service, "_fetch_oco_exit_detail", fetch)
    assert not await service._close_resolved_oco_managed_row(ACCT, "MI")
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
        assert session.scalars(select(Fill)).all() == []
    assert (ACCT, "MI") in service._oco_exit_fill_pending


@pytest.mark.asyncio
async def test_f1_prior_unrecorded_page_is_updated_not_duplicated_on_positive_flat(replay, monkeypatch):
    service, sessions, _, _ = replay
    with sessions() as session:
        row_id = str(_row(service, session).id)
        session.add(SystemIncident(
            service_name=service_module.SERVICE_NAME, severity="critical", status="open",
            title="prior child attribution pending", opened_at=service_module.utcnow(),
            payload={"source": "oco_exit_fill_unrecorded", "managed_row_id": row_id},
        ))
        session.commit()

    async def fetch(*_args, **_kwargs):
        return _EXIT_FETCH_FAILED

    monkeypatch.setattr(service, "_fetch_oco_exit_detail", fetch)
    assert await service._close_broker_flat_phantom_managed_row(ACCT, "MI", expected_row_id=row_id)
    with sessions() as session:
        incident, = session.scalars(select(SystemIncident)).all()
        assert incident.payload["broker_flat"] and not incident.payload["attributed"]
        assert incident.payload["owned_entry_coid"] == V2_COID
        assert incident.payload["candidate_entry_coid"] == ""
        assert _row(service, session) is None and session.scalars(select(Fill)).all() == []


@pytest.mark.parametrize("account", [ACCT, WEBULL])
@pytest.mark.parametrize("path", ["poll", "submit_response"])
@pytest.mark.asyncio
async def test_f2_recorded_fill_through_real_path_binds_and_exit_poll_owns_child(
    replay, monkeypatch, account, path
):
    service, sessions, _, _ = replay
    coid = V2_COID if account == ACCT else WEB_COID
    # The entry report values are retained MI fills. Webull's exit-only pair read is
    # controlled: its recorded MI exit was software, not a historical native child.
    with sessions() as session:
        row, entry = _row(service, session, account), _entry(session, coid)
        session.delete(row)
        entry.status = "accepted"
        entry.payload = {**entry.payload, "native_oco_bracket": "true", "path": "ATR Flip"}
        session.commit()
        entry_id, parent, quantity = entry.id, entry.broker_order_id, entry.quantity
        price = Decimal("3.31" if account == ACCT else "3.32")
        report = ExecutionReport(
            event_type="filled", client_order_id=coid, broker_order_id=parent,
            broker_fill_id=f"{parent}:{'180.0:2026-10-05T13:34:24+00:00' if account == ACCT else '90'}",
            symbol="MI", side="buy", intent_type="open", quantity=quantity,
            filled_quantity=quantity, fill_price=price, metadata=entry.payload,
            reported_at=datetime(2026, 10, 5, 13, 34, 24, tzinfo=UTC) if account == ACCT
            else datetime(2026, 10, 5, 13, 39, 37, 372000, tzinfo=UTC),
        )
        event = TradeIntentEvent(
            event_id=UUID(coid.rsplit("-", 1)[1] + "0" * 20),
            source_service="schwab-1m-v2", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=account, symbol="MI",
            side="buy", intent_type="open", quantity=quantity, reason="ATR Flip",
                metadata={**{k: str(v) for k, v in entry.payload.items() if v is not None},
                          "order_type": "stop_limit", "limit_price": str(price),
                      "stop_price": str(price)},
        ))
        if path == "submit_response":
            session.delete(entry)
            session.commit()

    if path == "submit_response":
        submitted = []

        async def submit(request):
            submitted.append(request)
            assert request.client_order_id == coid and request.broker_account_name == account
            assert request.quantity == quantity
            return [report]

        async def later_reconcile(_account):
            # Stop after committed/publicly published submit reports. The child
            # poll below is real; unrelated post-intent position traffic is not.
            return None

        service.settings.strategy_schwab_1m_v2_entry_notional_usd = 0
        service.settings.strategy_schwab_1m_v2_webull_entry_notional_usd = 0
        monkeypatch.setattr(service.broker_adapter, "submit_order", submit)
        monkeypatch.setattr(service, "_reconcile_after_intent", later_reconcile)
        events = await service.process_trade_intent(event)
        assert len(submitted) == 1 and any(e.payload.status == "filled" for e in events)
        with sessions() as session:
            entry_id = _entry(session, coid).id
    else:
        async def update(request):
            return report if request.client_order_id == coid else None

        monkeypatch.setattr(service.broker_adapter, "fetch_order_update", update)
        await service.sync_broker_orders(account_names=[account])
    with sessions() as session:
        row = _row(service, session, account)
        assert row.entry_order_id == entry_id and row.entry_client_order_id == coid
        assert service._find_oco_entry_order(session, account, "MI").id == entry_id
    reads = []

    async def child(acct, symbol, base, **kwargs):
        reads.append((acct, symbol, base, kwargs["entry_broker_order_id"]))
        assert base == coid and kwargs["entry_broker_order_id"] == parent
        return {**_detail(parent, coid, str(int(quantity))), "broker_order_id": "OWNED-CONTROL-CHILD"}

    monkeypatch.setattr(service.broker_adapter, "fetch_oco_exit_fill", child)
    service._v2_accounts = lambda: [account]
    await service._poll_native_oco_exits()
    with sessions() as session:
        assert _row(service, session, account) is None
        sells = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(sells) == 1 and sells[0].quantity == quantity
        assert session.get(BrokerOrder, sells[0].order_id).strategy_id == _entry(session, coid).strategy_id
    assert reads == [(account, "MI", coid, parent)]


@pytest.mark.asyncio
async def test_u2_webull_parent_is_never_queried_for_native_standdown(replay, monkeypatch):
    service, _, _, _ = replay
    service._managed_v2_symbols = {(WEBULL, "MI")}
    calls = []

    async def read(*args):
        calls.append(args)
        return {"working": ["T", "S"], "filled": False, "unsafe": False}

    async def hint(*args):
        calls.append(("hint", *args))
        return {"MI"}

    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", read)
    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    await service._refresh_native_oco_armed_state([WEBULL])
    assert calls == [] and not service._native_oco_stand_down_active(WEBULL, "MI")


@pytest.mark.asyncio
async def test_u3_one_owned_working_leg_cannot_stand_down(replay, monkeypatch):
    service, _, _, _ = replay
    service._managed_v2_symbols = {(ACCT, "MI")}

    async def read(*_args):
        return {"working": ["T"], "filled": False, "unsafe": False}

    async def hint(*_args):
        return {"MI"}

    monkeypatch.setattr(service.broker_adapter, "fetch_exit_legs_for_entry", read)
    monkeypatch.setattr(service.broker_adapter, "fetch_armed_native_oco_symbols", hint)
    await service._refresh_native_oco_armed_state([ACCT])
    assert not service._native_oco_stand_down_active(ACCT, "MI")


@pytest.mark.asyncio
async def test_u4_absent_bound_row_uuid_refuses_before_child_read(replay, monkeypatch):
    service, sessions, _, _ = replay
    with sessions() as session:
        session.delete(_row(service, session))
        session.commit()
    reads = []

    async def fetch(*args, **kwargs):
        reads.append(args)
        return _detail()

    monkeypatch.setattr(service, "_fetch_oco_exit_detail", fetch)
    assert not await service._close_resolved_oco_managed_row(ACCT, "MI")
    assert reads == []


@pytest.mark.parametrize("persist", ["pending", "failed"])
@pytest.mark.asyncio
async def test_u1_daic_bound_memory_pair_is_released_before_sell(replay, monkeypatch, persist):
    from tests.unit.test_confirmation_exit_fanout import _FanoutAdapter

    service, sessions, _, _ = replay
    with sessions() as session:
        entry, row = _entry(session, WEB_COID), _row(service, session, WEBULL)
        entry.symbol = row.symbol = "DAIC"
        entry.quantity = row.current_quantity = row.original_quantity = 1
        row.entry_price = Decimal("5.07")
        entry.payload = {"fanout_leg": "webull", "native_oco_bracket": "false"}
        row_id = str(row.id)
        session.commit()
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(service_module, "uuid4", lambda: UUID("ebfb9d30-c4a6-0000-0000-000000000000"))
    service.settings.oms_v2_cw_target_pct = 5.0
    service.settings.oms_v2_cw_hard_stop_pct = 8.0
    adapter = _FanoutAdapter()
    adapter.submit_results = [[ExecutionReport(
        event_type="accepted", client_order_id="schwab_1m_v2-DAIC-protect-ebfb9d30c4a6",
        symbol="DAIC", side="sell", intent_type="close", quantity=Decimal("1"),
    )]]
    service.broker_adapter = adapter
    entered, finish = asyncio.Event(), asyncio.Event()
    original = service._persist_webull_protect_base

    async def persist_handle(*args, **kwargs):
        entered.set()
        if persist == "pending":
            await finish.wait()
            return await original(*args, **kwargs)
        with monkeypatch.context() as failed_write:
            failed_write.setattr(service, "_find_oco_entry_order", lambda *a, **k: None)
            return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_persist_webull_protect_base", persist_handle)
    task = asyncio.create_task(service._attach_webull_protection(
        broker_account_name=WEBULL, symbol="DAIC", quantity=1, entry_price=5.07,
        strategy_code="schwab_1m_v2", entry_client_order_id=WEB_COID,
    ))
    await asyncio.wait_for(entered.wait(), 2)
    if persist == "failed":
        assert await task
    base = adapter.submitted[0].client_order_id
    assert base == "schwab_1m_v2-DAIC-protect-ebfb9d30c4a6"
    assert adapter.submitted[0].metadata["bracket_target_price"] == "5.3235"
    assert adapter.submitted[0].metadata["bracket_stop_price"] == "4.6644"
    sequence = []
    release = adapter.release_exit_pair_for_close

    async def released(**kwargs):
        sequence.append(("release", kwargs["base_client_order_id"]))
        return await release(**kwargs)

    async def sell(*args, **kwargs):
        sequence.append(("sell", kwargs["reason"]))
        return "close_submitted"

    def c3(*args, **kwargs):
        return "not_applicable"

    monkeypatch.setattr(adapter, "release_exit_pair_for_close", released)
    monkeypatch.setattr(service, "_emit_v2_exit_on_loop", sell)
    monkeypatch.setattr(service, "_post_exit_stale_held_action", c3)
    decision = _ConfirmationFanoutDecision("DAIC", "DAIC-09-17-controlled-close", (WEBULL,), exit_tag="CW_HARD_STOP")
    try:
        outcome = await service._webull_cancel_then_sell(
            WEBULL, "DAIC", exit_tag="CW_HARD_STOP", reason="oms_v2_managed_exit:CW_HARD_STOP", kind="close",
            reference_bid=4.66, expected_row_id=row_id,
            expires_at=service_module.utcnow() + timedelta(seconds=60), decision=decision,
        )
        assert outcome == "close_submitted"
        assert sequence == [("release", base), ("sell", "oms_v2_managed_exit:CW_HARD_STOP")]
    finally:
        finish.set()
        await task
    if persist == "pending":
        with sessions() as session:
            assert _entry(session, WEB_COID).payload["webull_protect_state"] == "released"


def test_u1_foreign_memory_handle_never_authorizes_bound_entry(replay):
    service, sessions, _, _ = replay
    service._webull_protect_base[(WEBULL, "MI")] = "FOREIGN-PAIR"
    with sessions() as session:
        entry = _entry(session, WEB_COID)
        assert service._oco_exit_base_for_entry(entry, broker_account_name=WEBULL, symbol="MI") == ""


@pytest.mark.asyncio
async def test_u1_same_entry_replacement_episode_cannot_reuse_temporary_handle(replay):
    service, sessions, _, _ = replay
    assert await service._bind_webull_protect_memory(WEBULL, "MI", "OWNED-PAIR", WEB_COID) is None
    with sessions() as session:
        entry = service._find_oco_entry_order(session, WEBULL, "MI")
        assert service._oco_exit_base_for_entry(entry, broker_account_name=WEBULL, symbol="MI") == "OWNED-PAIR"
        old = _row(service, session, WEBULL)
        session.delete(old)
        session.flush()
        service.store.create_managed_position(
            session, strategy_code="schwab_1m_v2", broker_account_name=WEBULL, symbol="MI",
            entry_price=Decimal("3.32"), quantity=90,
            entry_order_id=entry.id, entry_client_order_id=entry.client_order_id,
        )
        session.flush()
        current = service._find_oco_entry_order(session, WEBULL, "MI")
        assert service._oco_exit_base_for_entry(current, broker_account_name=WEBULL, symbol="MI") == ""
