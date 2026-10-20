"""Approved refusal through real raw Webull readers, not scalar-release stubs."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from project_mai_tai.broker_adapters import webull as adapter_module
from project_mai_tai.broker_adapters.webull import WebullAccountConfig
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.legacy_cancelled_partial import SOURCE, has_cancelled_child_execution
from tests.unit.test_webull_adapter import _LegClient, _adapter, fake_sdk as fake_sdk
from tests.unit.test_wbattach1_riskoff import closes, make_case as make_case, restarted


def legacy_case(case, monkeypatch, filled):
    service, factory, broker, entry_id, clock = case
    base = "original-legacy-tree"
    with factory.begin() as unit:
        entry = unit.get(BrokerOrder, entry_id)
        entry.payload = {**entry.payload, "webull_protect_base_client_order_id": base,
                         service._WEBULL_PROTECT_STATE_KEY: "resting"}
        row_id = str(unit.scalar(select(OmsManagedPosition.id)))
    bodies = {}
    for suffix in ("T", "S"):
        coid = adapter_module.WebullBrokerAdapter._combo_leg_coid(base, suffix)
        bodies[coid] = {"items": [{"order_id": "native-" + suffix,
            "client_order_id": coid, "account_id": "ACC1", "symbol": "ABC", "side": "SELL",
            "order_status": "CANCELLED", "filled_qty": str(filled if suffix == "T" else 0),
            "filled_price": "11" if filled and suffix == "T" else None}]}
    client = _LegClient(bodies)
    wire = _adapter(client, _CANCEL_CONFIRM_DELAY_SECONDS=0)
    wire.accounts_by_name = {broker.account: WebullAccountConfig(account_id="ACC1")}
    monkeypatch.setattr(adapter_module, "datetime", oms.datetime)
    broker.release_exit_pair_for_close = wire.release_exit_pair_for_close
    broker.confirm_exit_pair_terminal = wire.confirm_exit_pair_terminal
    broker.fetch_oco_exit_fill = wire.fetch_oco_exit_fill
    broker.total = Decimal(1010 - filled)
    return row_id, client, wire


async def handover(case, route, row_id):
    service, factory, broker, _, clock = case
    if route == "ordinary":
        with factory() as unit:
            return (await service._release_exit_reservation_before_close(
                session=unit, broker_account_name=broker.account, symbol="ABC")).outcome
    if route == "confirmation":
        return await service._prepare_confirmation_webull_leg(
            broker.account, "ABC", expected_row_id=row_id,
            expires_at=clock[0] + timedelta(seconds=30),
            decision=oms._ConfirmationFanoutDecision("ABC", "controlled-original", (broker.account,),
                                                   exit_tag="CW_HARD_STOP"))
    return (await service._release_v2_eod_oco(broker.account, "ABC", expected_row_id=row_id)).outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["ordinary", "confirmation", "eod"])
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("hour", [12, 14])
@pytest.mark.parametrize("filled", [0, 2])
async def test_real_raw_legacy_handover_zero_unchanged_partial_refused(
    make_case, fake_sdk, monkeypatch, caplog, route, account, hour, filled
):
    case = make_case(account=account)
    monkeypatch.setattr(case[0].logger, "handlers", [*case[0].logger.handlers, caplog.handler])
    case[4][0] = case[4][0].replace(hour=hour)
    row_id, client, _wire = legacy_case(case, monkeypatch, filled)
    outcome = await handover(case, route, row_id)
    assert client.seen, "The actual original-child reader must be reached"
    assert outcome == ("refused_cancelled_partial" if route == "confirmation" and filled
                       else "unanswerable" if filled else "released")
    assert not closes(case[2])
    with case[1]() as unit:
        incidents = [item for item in unit.scalars(select(SystemIncident))
                     if item.payload.get("source") == SOURCE
                     or item.payload.get("unaccounted_child_state", {}).get("source") == SOURCE]
        assert len(incidents) == int(bool(filled))
        assert unit.scalar(select(OmsManagedPosition.current_quantity)) == 10
        assert not list(unit.scalars(select(Fill).where(Fill.side == "sell")))
    if filled:
        assert "WB-LEGACY-CANCELLED-PARTIAL" in caplog.text
        reads = len(client.seen)
        await handover(case, route, row_id)
        assert len(client.seen) == reads  # A retry cannot erase the execution into scalar UNKNOWN.
        replacement = restarted(case)
        await handover(replacement, route, row_id)
        assert len(client.seen) == reads and not closes(case[2])
    else:
        assert "WB-LEGACY-CANCELLED-PARTIAL" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("control", [False, True])
async def test_actual_protective_owner_cannot_override_partial_refusal_or_double_sell(
    make_case, fake_sdk, monkeypatch, account, control
):
    # A legacy tree predates the strict new-bare-fill provenance marker.
    case = make_case(account=account, control=control, provenance=False)
    service, factory, broker, _, _ = case
    row_id, client, _wire = legacy_case(case, monkeypatch, 2)
    if control:
        with factory() as unit:
            strategy = unit.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
            service._bot_control_runtime.store.seed(strategy.id)
        await service._bot_control_runtime.refresh()
    with factory() as unit:
        snapshot = service._read_v2_managed_snapshot(unit, account, "ABC", True)
    position = service._hydrate_v2_position(snapshot)
    result = await service._emit_v2_exit_on_loop(
        account, "ABC", position, 10, kind="CLOSE", reference_price=9,
        reason="oms_v2_managed_exit:CW_HARD_STOP", bid=9, close_on_fill=True,
        expected_managed_row_id=row_id)
    assert result == "refused" and client.seen
    assert not closes(broker) and broker.total == 1008
    assert not service.__dict__.get("_control_managed_exit_claims")


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_mixed_strict_marker_and_legacy_tree_cannot_bypass_original_proof(
    make_case, fake_sdk, monkeypatch, account
):
    case = make_case(account=account)
    row_id, client, _wire = legacy_case(case, monkeypatch, 2)
    assert await protective_close(case, row_id) == "refused"
    assert not client.seen and not closes(case[2]) and case[2].total == 1008
    with case[1]() as unit:
        entry = unit.get(BrokerOrder, case[3])
        assert entry.payload["wbattach1_bare_fill_observed"]["source"] == "new_bare_fill_writer"
        assert entry.payload["wbattach1"]["legacy_base"] == "original-legacy-tree"
        assert entry.payload["wbattach1"]["history_proven"] is True
        assert unit.get(OmsManagedPosition, UUID(row_id)).current_quantity == 10
        incidents = list(unit.scalars(select(SystemIncident)))
        assert len(incidents) == 1
        assert incidents[0].payload["entry_id"] == str(case[3])
        assert incidents[0].payload["reason"] == (
            "software_exit_child_proof_required:wbattach1_original_tree_or_close_unknown")


@pytest.mark.asyncio
async def test_eod_second_fresh_read_cannot_turn_partial_into_release(make_case, fake_sdk, monkeypatch):
    case = make_case()
    row_id, client, wire = legacy_case(case, monkeypatch, 0)
    ordinary = wire.release_exit_pair_for_close

    async def first_zero_then_partial(**binding):
        result = await ordinary(**binding)
        coid = adapter_module.WebullBrokerAdapter._combo_leg_coid(binding["base_client_order_id"], "T")
        client.by_coid[coid]["items"][0].update(filled_qty="2", filled_price="11")
        case[2].total = Decimal(1008)
        return result

    case[2].release_exit_pair_for_close = first_zero_then_partial
    assert await handover(case, "eod", row_id) == "unanswerable"
    assert not closes(case[2]) and len(client.seen) == 4


@pytest.mark.parametrize("event,quantity,expected", [
    ("cancelled", "2", True), ("cancelled", "0", False),
    ("filled", "2", False), ("partially_filled", "2", False),
])
def test_exact_replay_predicate(event, quantity, expected):
    report = adapter_module.ExecutionReport(event, "original-child", filled_quantity=Decimal(quantity))
    assert has_cancelled_child_execution([report]) is expected


async def protective_close(case, row_id):
    service, factory, broker, _, _ = case
    with factory() as unit:
        snapshot = service._read_v2_managed_snapshot(unit, broker.account, "ABC", True)
    return await service._emit_v2_exit_on_loop(
        broker.account, "ABC", service._hydrate_v2_position(snapshot), 10,
        kind="CLOSE", reference_price=9, reason="oms_v2_managed_exit:CW_HARD_STOP",
        bid=9, close_on_fill=True, expected_managed_row_id=row_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["confirmation", "eod"])
@pytest.mark.parametrize("restart", [False, True])
async def test_existing_partial_fence_blocks_actual_protective_owner_with_release_flag_OFF(
    make_case, fake_sdk, monkeypatch, route, restart
):
    case = make_case()
    object.__setattr__(case[0].settings, "oms_v2_exit_release_reservation_enabled", False)
    row_id, client, _ = legacy_case(case, monkeypatch, 2)
    await handover(case, route, row_id)
    reads = len(client.seen)
    actual = restarted(case) if restart else case
    assert await protective_close(actual, row_id) == "refused"
    assert not closes(case[2]) and case[2].total == 1008 and len(client.seen) == reads


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["ordinary", "confirmation", "eod"])
async def test_real_broker_await_late_A_execution_never_quarantines_new_B(
    make_case, fake_sdk, monkeypatch, route
):
    case = make_case()
    service, factory, broker, entry_id, clock = case
    row_id, _client, wire = legacy_case(case, monkeypatch, 2)
    original = wire.release_exit_pair_for_close
    new_row_id = None
    engine = factory.kw["bind"]
    assert engine.dialect.name == "sqlite"
    model_index = next(index for index in OmsManagedPosition.__table__.indexes
                       if index.name == "uq_oms_managed_positions_open_symbol")
    assert str(model_index.dialect_options["postgresql"]["where"]) == "status = 'open'"
    # Mirror the model's PostgreSQL OPEN-only index in this local SQLite fixture.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_oms_managed_positions_open_symbol")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_oms_managed_positions_open_symbol "
            "ON oms_managed_positions (broker_account_name, symbol) WHERE status = 'open'")
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        with factory.begin() as unit:
            service.store.create_managed_position(unit, strategy_code="schwab_1m_v2",
                broker_account_name=broker.account, symbol="ABC", entry_price=Decimal(10), quantity=10,
                entry_order_id=entry_id, entry_client_order_id="owned-entry", entry_time=clock[0])

    async def replace_episode_while_read_is_awaited(**binding):
        nonlocal new_row_id
        result = await original(**binding)  # Actual old-child raw confirmation, still episode A.
        with factory.begin() as unit:
            old = unit.get(OmsManagedPosition, UUID(row_id))
            old.status, old.current_quantity = "closed", 0
            entry = unit.get(BrokerOrder, entry_id)
            parent = unit.get(TradeIntent, entry.intent_id)
            new_intent = TradeIntent(strategy_id=parent.strategy_id, broker_account_id=parent.broker_account_id,
                symbol="ABC", side="buy", intent_type="open", quantity=Decimal(10),
                reason="controlled successor episode", status="filled", payload={})
            unit.add(new_intent)
            unit.flush()
            new_entry = BrokerOrder(intent_id=new_intent.id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, client_order_id="new-entry-" + uuid4().hex,
                broker_order_id="new-native", symbol="ABC", side="buy", status="filled",
                quantity=Decimal(10), order_type="market", time_in_force="day", submitted_at=clock[0],
                payload={"fanout_leg": "webull", "webull_protect_base_client_order_id": "new-tree"})
            unit.add(new_entry)
            unit.flush()
            unit.add(Fill(order_id=new_entry.id, strategy_id=new_entry.strategy_id,
                broker_account_id=new_entry.broker_account_id, broker_fill_id="new-owned-fill",
                symbol="ABC", side="buy", quantity=Decimal(10), price=Decimal(10), filled_at=clock[0]))
            successor = service.store.create_managed_position(unit, strategy_code="schwab_1m_v2",
                broker_account_name=broker.account, symbol="ABC", entry_price=Decimal(10), quantity=10,
                entry_order_id=new_entry.id, entry_client_order_id=new_entry.client_order_id, entry_time=clock[0])
            unit.flush()
            new_row_id = str(successor.id)
        return result

    broker.release_exit_pair_for_close = replace_episode_while_read_is_awaited
    outcome = await handover(case, route, row_id)
    assert outcome in {"unanswerable", "refused_cancelled_partial"} and not closes(broker)
    with factory() as unit:
        incidents = [item for item in unit.scalars(select(SystemIncident))
                     if item.payload.get("source") == SOURCE
                     or item.payload.get("unaccounted_child_state", {}).get("source") == SOURCE]
        assert len(incidents) == 1 and incidents[0].payload["managed_row_id"] == row_id
        assert incidents[0].payload["entry_order_id"] == str(entry_id)
        assert incidents[0].payload["managed_row_id"] != new_row_id
        assert unit.get(OmsManagedPosition, UUID(row_id)).status == "closed"
        assert unit.get(OmsManagedPosition, UUID(new_row_id)).status == "open"
    assert not await service._legacy_webull_partial_handover_refused(broker.account, "ABC")


@pytest.mark.asyncio
async def test_failed_negative_audit_commit_cannot_erase_partial_into_retry_permission(
    make_case, fake_sdk, monkeypatch
):
    case = make_case()
    service, factory, broker, _, _ = case
    row_id, client, _ = legacy_case(case, monkeypatch, 2)
    failed = []
    engine = factory.kw["bind"]

    def fail_first_negative_commit(unit):
        if failed:
            return
        # Flush/query in the still-active ORM transaction, not an Engine.commit
        # callback whose exception has already deactivated the root transaction.
        unit.flush()
        pending = unit.scalar(select(SystemIncident.id).where(
            SystemIncident.payload["source"].as_string() == SOURCE,
            SystemIncident.payload["managed_row_id"].as_string() == row_id,
        ))
        if pending is not None:
            failed.append(True)
            raise OSError("controlled negative-audit commit failure")

    event.listen(factory.class_, "before_commit", fail_first_negative_commit)
    try:
        with pytest.raises(OSError, match="negative-audit"):
            await handover(case, "ordinary", row_id)
        with factory() as unit:
            assert not list(unit.scalars(select(SystemIncident).where(
                SystemIncident.payload["source"].as_string() == SOURCE)))
        verification_engine = create_engine(engine.url, poolclass=NullPool)
        try:
            with verification_engine.connect() as connection:
                assert not connection.execute(select(SystemIncident.id).where(
                    SystemIncident.payload["source"].as_string() == SOURCE)).all()
        finally:
            verification_engine.dispose()
        reads = len(client.seen)
        assert failed and service._legacy_cancelled_partial_rows[row_id]["children"][0]["filled_quantity"] == "2"
        assert await handover(case, "ordinary", row_id) == "unanswerable"
        assert len(client.seen) == reads and not closes(broker)
        assert await handover(restarted(case), "ordinary", row_id) == "unanswerable"
        with factory() as unit:
            saved = [item for item in unit.scalars(select(SystemIncident)) if item.payload.get("source") == SOURCE]
            assert len(saved) == 1 and saved[0].payload["managed_row_id"] == row_id
    finally:
        event.remove(factory.class_, "before_commit", fail_first_negative_commit)


@pytest.mark.asyncio
async def test_unbound_entry_positive_stays_refused_during_retry_flag_OFF_and_restart(
    make_case, fake_sdk, monkeypatch
):
    # Reach the real legacy reader before injecting the original-binding loss.
    case = make_case(provenance=False)
    row_id, client, _ = legacy_case(case, monkeypatch, 2)
    release = case[2].release_exit_pair_for_close
    invalidated = []

    async def invalidate_binding_after_original_read(*args, **kwargs):
        with case[1]() as unit:
            assert unit.get(OmsManagedPosition, UUID(row_id)).entry_order_id is not None
        result = await release(*args, **kwargs)
        assert client.seen
        with case[1].begin() as unit:
            unit.get(OmsManagedPosition, UUID(row_id)).entry_order_id = None
        invalidated.append(row_id)
        return result

    case[2].release_exit_pair_for_close = invalidate_binding_after_original_read
    assert await protective_close(case, row_id) == "refused"
    assert invalidated == [row_id]
    assert client.seen and not closes(case[2]) and case[2].total == 1008
    with case[1]() as unit:
        quarantines = [item for item in unit.scalars(select(SystemIncident))
                       if item.payload.get("source") == SOURCE
                       or item.payload.get("unaccounted_child_state", {}).get("source") == SOURCE]
        assert len(quarantines) == 1 and quarantines[0].payload["managed_row_id"] == row_id
        assert quarantines[0].payload["entry_order_id"] is None
        assert quarantines[0].payload["execution_binding"] == "UNKNOWN_CALLER_REFUSED"
    reads = len(client.seen)
    object.__setattr__(case[0].settings, "oms_v2_exit_release_reservation_enabled", False)
    assert await protective_close(case, row_id) == "refused"
    assert await protective_close(restarted(case), row_id) == "refused"
    assert len(client.seen) == reads and not closes(case[2])
