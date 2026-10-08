"""Reader observations exercise real boundaries without live DB/HTTP/broker effects."""
from datetime import UTC, datetime
from decimal import Decimal
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import orb_schwab_eod, wbquiet_shadow as shadow
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService, _PositionRead
from project_mai_tai.orb_schwab_macd import MacdVerdict

AT = datetime(2026, 10, 8, 14, tzinfo=UTC)
WB = "live:webull_1m_v2"
SCHWAB = "live:schwab_1m_v2"


def position(account=WB, quantity="2", stamp=AT):
    return BrokerPositionSnapshot(broker_account_name=account, symbol="IPDN",
                                  quantity=Decimal(quantity), average_price=Decimal("5"),
                                  as_of=stamp)


def harness(monkeypatch, *, stamp=100):
    clock = [float(stamp)]
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock[0])
    service = OmsRiskService.__new__(OmsRiskService)
    service.logger = Mock()
    snapshots = [position()]
    service.broker_adapter = SimpleNamespace(
        provider_by_account={WB: "webull", SCHWAB: "schwab"},
        _adapters_by_provider={"webull": SimpleNamespace(
            _positions_lock=threading.Lock(), _positions_cache={WB: (stamp - 1, snapshots)},
            _positions_throttle_secs=10)},
        list_account_positions=AsyncMock(return_value=snapshots),
    )
    service.store = SimpleNamespace(sync_account_positions=Mock(), get_account_position=Mock())
    service.session_factory = Mock(side_effect=AssertionError("hook SQL"))
    service._wbquiet_shadow = shadow.ShadowObserver()
    frame = shadow.Frame(71, AT, clock[0], {WB: {"known": True, "fresh": True}})
    token = shadow.CURRENT.set(frame)
    try:
        shadow.note_read(WB, "returned_not_wire_proof", service=service, positions=snapshots)
        shadow.note_committed([WB])
    finally:
        shadow.CURRENT.reset(token)
    service._wbquiet_shadow.retain(frame, published=True)
    return service, frame, clock


def notes(service):
    return [json.loads(call.args[1]) for call in service.logger.info.call_args_list
            if call.args[0] == "[WBQUIET-READER] %s"]


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity,expected", [("2", _PositionRead.HELD),
                                               ("0", _PositionRead.FLAT_CONFIRMED)])
async def test_reconcile_consumes_own_account_generation_after_context_reset(monkeypatch, quantity, expected):
    service, frame, clock = harness(monkeypatch)
    service.broker_adapter.list_account_positions.return_value = [position(quantity=quantity)]
    clock[0] += 59
    assert shadow.CURRENT.get() is None
    assert await service._broker_symbol_position_state(WB, "IPDN") is expected
    service.broker_adapter.list_account_positions.assert_awaited_once_with(WB)
    row = notes(service)[0]["readers"][0]
    assert row["reader"] == "reconcile_tri_state" and row["account"] == WB
    assert row["generation"] == AT.isoformat() and row["adapter_calls"] == 1
    assert row["provider"] == "webull" and row["webull_cadence_eligible"] is True
    anchor = row["overlapping_periodic_passes"][0]
    assert anchor["pass_id"] == 71 and anchor["account"] == WB
    assert anchor["acquisition_generation"] == frame.committed[WB]["acquisition_generation"]
    assert anchor["adapter_calls"] == 1 and anchor["elapsed_seconds"] == 59
    assert row["same_source_generation"] == "UNMEASURED"
    service.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_refresh_still_persists_once_and_keeps_quantity(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    service.store.get_account_position.return_value = SimpleNamespace(quantity=Decimal("2"))
    session, account_id = object(), uuid4()
    assert await service._refresh_broker_position_quantity(
        session=session, broker_account_id=account_id, broker_account_name=WB, symbol="IPDN",
    ) == Decimal("2")
    service.store.sync_account_positions.assert_called_once_with(
        session, broker_account_id=account_id,
        snapshots=service.broker_adapter.list_account_positions.return_value,
    )
    row = notes(service)[0]["readers"][0]
    assert row["reader"] == "exit_snapshot_refresh" and row["adapter_calls"] == 1
    assert row["generation"] == AT.isoformat()
    service.session_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["reconcile", "refresh", "orb"])
async def test_unreadable_real_read_keeps_existing_failure_semantics(monkeypatch, reader):
    service, _frame, _clock = harness(monkeypatch)
    service.broker_adapter.list_account_positions.side_effect = RuntimeError("unreadable")
    if reader == "reconcile":
        assert await service._broker_symbol_position_state(WB, "IPDN") is _PositionRead.UNKNOWN
    elif reader == "refresh":
        assert await service._refresh_broker_position_quantity(
            session=object(), broker_account_id=uuid4(), broker_account_name=WB, symbol="IPDN",
        ) is None
        service.store.sync_account_positions.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="unreadable"):
            await orb_schwab_eod._broker_quantity(service, SimpleNamespace(
                account=SCHWAB, symbol="IPDN", quantity=Decimal("2")))
    row = notes(service)[0]["readers"][0]
    assert row["outcome"] == "unreadable" and row["adapter_calls"] == 1
    assert row["generation"] == "UNMEASURED"


@pytest.mark.asyncio
async def test_orb_eod_repeated_reads_are_schwab_not_webull_consumption(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    service.broker_adapter.list_account_positions.return_value = [position(SCHWAB)]
    target = SimpleNamespace(account=SCHWAB, symbol="IPDN", quantity=Decimal("2"))
    for _ in range(2):
        assert await orb_schwab_eod._broker_quantity(service, target) == 2
    assert service.broker_adapter.list_account_positions.await_count == 2
    records = notes(service)
    rows = [r["readers"][0] for r in records]
    assert [r["reader"] for r in rows] == ["orb_close_positions"] * 2
    assert sum(r["adapter_calls"] for r in rows) == 2
    assert len({r["adapter_read_id"] for r in rows}) == 2
    assert all(r["provider"] == "schwab" and r["account"] == SCHWAB
               and r["webull_cadence_eligible"] is False for r in rows)
    assert all(r["overlapping_periodic_passes"] == []
               and r["periodic_evidence"] == "UNMEASURED" for r in rows)
    assert all(r["other_oms_and_external_readers"] == "PARTIAL"
               and r["wire_calls"] == "UNMEASURED" for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [False, True])
async def test_real_orb_admission_reader_refusal_and_pull_count_unchanged(monkeypatch, unreadable):
    service, _frame, _clock = harness(monkeypatch)
    service.settings = SimpleNamespace(orb_schwab_atr_entry_gate_enabled=False)
    service._rpg_external_retry = lambda _event: False
    service._refresh_drift_working_cache = AsyncMock()
    monkeypatch.setattr(service_module, "orb_schwab_intent_refusal", lambda *_: None)
    monkeypatch.setattr(service_module, "schwab_completed_bar_macd_gate",
                        lambda *_: (MacdVerdict.ALLOWED, "allowed", 0.1))
    service.broker_adapter.list_account_positions.return_value = [position(SCHWAB)]
    if unreadable:
        service.broker_adapter.list_account_positions.side_effect = RuntimeError("unreadable")
    event = TradeIntentEvent(source_service="reader-test", payload=TradeIntentPayload(
        strategy_code="orb_schwab", broker_account_name=SCHWAB, symbol="IPDN",
        side="buy", intent_type="open", quantity=Decimal("2"), reason="reader test",
    ))
    assert await service.process_trade_intent(event) == []
    service.broker_adapter.list_account_positions.assert_awaited_once_with(SCHWAB)
    service._refresh_drift_working_cache.assert_awaited_once()
    service.session_factory.assert_not_called()
    row = notes(service)[0]["readers"][0]
    assert row["reader"] == "orb_admission_positions" and row["provider"] == "schwab"
    assert row["webull_cadence_eligible"] is False and row["adapter_calls"] == 1
    assert row["outcome"] == ("unreadable" if unreadable else "returned_not_wire_proof")


@pytest.mark.asyncio
async def test_reserve1_renewal_notes_exact_bound_entry_without_position_pull(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    service.settings = SimpleNamespace(oms_native_oco_stand_down_enabled=True,
                                      oms_native_oco_resolve_flat_reconcile_enabled=False)
    service._page_aged_pending_oco_exit_fills = AsyncMock()
    service._managed_v2_symbols = {(SCHWAB, "IPDN")}
    service._native_oco_armed_confirmed_at = {}
    service._native_oco_resolving = {}
    row = SimpleNamespace(id="managed-row-1")
    service.store.get_open_managed_position = Mock(return_value=row)
    service._find_oco_entry_order = lambda *_a, **_kw: SimpleNamespace(broker_order_id="parent-1")
    service._is_v2_webull_account = lambda _acct: False
    async def run_db(fn, **_kwargs):
        return fn(object())
    service._run_db = run_db
    service.broker_adapter.fetch_exit_legs_for_entry = AsyncMock(return_value={
        "working": ["target", "stop"], "unsafe": False, "filled": False,
    })
    service.broker_adapter.fetch_armed_native_oco_symbols = AsyncMock(return_value={"IPDN"})
    monkeypatch.setattr(service_module, "utcnow", lambda: AT)
    await service._refresh_native_oco_armed_state(None)
    assert service._native_oco_armed_confirmed_at == {(SCHWAB, "IPDN"): AT}
    service.broker_adapter.fetch_exit_legs_for_entry.assert_awaited_once_with(SCHWAB, "parent-1")
    service.broker_adapter.list_account_positions.assert_not_awaited()
    note = notes(service)[0]["readers"][0]
    assert note["reader"] == "reserve1_bracket_note_renewal" and note["adapter_calls"] == 0
    assert note["generation"] == f"managed-row-1:parent-1:{AT.isoformat()}"
    assert note["provider"] == "schwab" and note["webull_cadence_eligible"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["logging", "registry", "contention"])
async def test_observer_failure_never_changes_reconcile_or_adds_io(monkeypatch, broken):
    service, _frame, _clock = harness(monkeypatch)
    observer = service._wbquiet_shadow
    if broken == "logging":
        def log(format_string, *_args):
            if format_string == "[WBQUIET-READER] %s":
                raise RuntimeError("observer logging")
        service.logger.info.side_effect = log
    elif broken == "registry":
        monkeypatch.setattr(observer, "reader_note", Mock(side_effect=RuntimeError("observer registry")))
    else:
        observer.reader_lock.acquire()
    try:
        for _ in range(240):
            assert await service._broker_symbol_position_state(WB, "IPDN") is _PositionRead.HELD
        assert service.broker_adapter.list_account_positions.await_count == 240
        assert observer.reader_dropped == 240
        service.session_factory.assert_not_called()
    finally:
        if observer.reader_lock.locked():
            observer.reader_lock.release()


@pytest.mark.asyncio
async def test_missing_observer_or_expired_window_is_not_zero_evidence(monkeypatch):
    service, _frame, clock = harness(monkeypatch)
    clock[0] += 60.001
    assert await service._broker_symbol_position_state(WB, "IPDN") is _PositionRead.HELD
    assert notes(service) == [] and not service._wbquiet_shadow.committed_generations
    del service._wbquiet_shadow
    assert await service._broker_symbol_position_state(WB, "IPDN") is _PositionRead.HELD
    assert notes(service) == []


def test_per_account_registry_is_bounded_and_loss_is_unmeasured(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    observer = service._wbquiet_shadow
    for pass_id in range(100):
        frame = shadow.Frame(pass_id, AT, 100, {WB: {}})
        token = shadow.CURRENT.set(frame)
        try:
            shadow.note_read(WB, "returned_not_wire_proof", service=service,
                             positions=service.broker_adapter.list_account_positions.return_value)
            shadow.note_committed([WB])
        finally:
            shadow.CURRENT.reset(token)
        observer.retain(frame, published=True)
    assert len(observer.committed_generations[WB]) == shadow.MAX_WINDOWS
    shadow.note_consumed([WB], "test_reader", service=service)
    assert notes(service)[0]["readers"][0]["periodic_evidence"] == "UNMEASURED"
    for index in range(shadow.MAX_ACCOUNTS + 10):
        frame = shadow.Frame(index, AT, 100)
        frame.committed[f"account{index}"] = {"account": f"account{index}"}
        observer.retain(frame, published=True)
    assert len(observer.committed_generations) == shadow.MAX_ACCOUNTS


@pytest.mark.asyncio
async def test_one_shadow_receipt_is_not_rewritten_by_later_reader_notes(monkeypatch):
    service, frame, clock = harness(monkeypatch)
    observer = service._wbquiet_shadow
    observer.committed_generations.clear()
    await observer.finish(service, frame, "ok")
    published = service.logger.info.call_args.args[1]
    clock[0] += 30
    await service._broker_symbol_position_state(WB, "IPDN")
    shadow_calls = [c for c in service.logger.info.call_args_list if c.args[0] == "[WBQUIET-SHADOW] %s"]
    assert len(shadow_calls) == 1 and shadow_calls[0].args[1] == published
    assert len(notes(service)) == 1
    assert notes(service)[0]["readers"][0]["periodic_evidence"] == "committed_generation_observed"


def test_failed_or_missing_commit_cannot_anchor_generation(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    service._wbquiet_shadow.committed_generations.clear()
    frame = shadow.Frame(72, AT, 100, {WB: {}})
    token = shadow.CURRENT.set(frame)
    try:
        shadow.note_read(WB, "unreadable")
        shadow.note_committed([WB])
    finally:
        shadow.CURRENT.reset(token)
    service._wbquiet_shadow.retain(frame, published=False)
    shadow.note_consumed([WB], "test_reader", service=service)
    row = notes(service)[0]["readers"][0]
    assert row["overlapping_periodic_passes"] == [] and row["periodic_evidence"] == "UNMEASURED"


def test_account_mismatch_or_iterator_never_supplies_snapshot_generation(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    for positions in ([position(SCHWAB)], iter([position()])):
        shadow.note_consumed([WB], "test_reader", service=service,
                             source="adapter_positions", positions=positions, symbol="IPDN")
        assert notes(service)[-1]["readers"][0]["generation"] == "UNMEASURED"
    assert next(positions).broker_account_name == WB


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_real_sync_only_registers_after_persist_transaction_returns(monkeypatch, commit_fails):
    service, _old_frame, _clock = harness(monkeypatch)
    service.settings = SimpleNamespace(oms_virtual_position_clear_min_age_seconds=24.119)
    account_id = uuid4()
    service.store.list_active_broker_accounts = Mock(return_value=[SimpleNamespace(id=account_id, name=WB)])
    service.store.sync_account_positions.return_value = 1
    service.store.clear_virtual_positions_without_account_backing = Mock(return_value=[])
    service.store.restore_virtual_positions_from_managed = Mock(return_value=[])
    service._observe_settlement = lambda *_: None
    async def run_db(fn, commit=True):
        result = fn(object())
        if commit and commit_fails:
            raise RuntimeError("commit failed")
        return result
    service._run_db = run_db
    frame = shadow.Frame(72, AT, 100, {WB: {}})
    token = shadow.CURRENT.set(frame)
    try:
        if commit_fails:
            with pytest.raises(RuntimeError, match="commit failed"):
                await service.sync_broker_positions()
            assert frame.committed == {}
        else:
            assert await service.sync_broker_positions() == {"accounts": 1, "positions": 1}
            assert frame.committed[WB]["pass_id"] == 72
            assert frame.committed[WB]["adapter_calls"] == 1
            assert frame.committed[WB]["acquisition_generation"] == "adapter_cache:99.0"
        assert [r[1] for r in frame.readers] == ["sync_account_positions", "virtual_clear", "virtual_restore"]
        service.broker_adapter.list_account_positions.assert_awaited_once_with(WB)
    finally:
        shadow.CURRENT.reset(token)


@pytest.mark.asyncio
async def test_failed_shadow_publication_cannot_claim_complete_join(monkeypatch):
    service, frame, _clock = harness(monkeypatch)
    observer = service._wbquiet_shadow
    observer.committed_generations.clear()
    service.logger.info.side_effect = RuntimeError("failed publication")
    await observer.finish(service, frame, "ok")
    service.logger.info.side_effect = None
    shadow.note_consumed([WB], "test_reader", service=service)
    row = notes(service)[0]["readers"][0]
    assert row["overlapping_periodic_passes"][0]["shadow_receipt_emitted"] is False
    assert row["periodic_evidence"] == "UNMEASURED"


def test_window_is_60_seconds_after_commit_and_multiple_passes_are_not_aliases(monkeypatch):
    service, frame, clock = harness(monkeypatch)
    observer = service._wbquiet_shadow
    observer.committed_generations.clear()
    frame.started = 90
    frame.committed[WB]["committed_elapsed_seconds"] = 10
    observer.retain(frame, published=True)
    later = shadow.Frame(72, AT, 150, {WB: {}})
    clock[0] = 150
    token = shadow.CURRENT.set(later)
    try:
        shadow.note_read(WB, "returned_not_wire_proof")
        shadow.note_read(WB, "returned_not_wire_proof")
        shadow.note_committed([WB])
    finally:
        shadow.CURRENT.reset(token)
    observer.retain(later, published=True)
    clock[0] = 160
    shadow.note_consumed([WB], "test_reader", service=service)
    anchors = notes(service)[-1]["readers"][0]["overlapping_periodic_passes"]
    assert [a["pass_id"] for a in anchors] == [71, 72]
    assert [a["adapter_calls"] for a in anchors] == [1, 2]
    assert len({a["local_read_id"] for a in anchors}) == 2
    clock[0] = 160.001
    shadow.note_consumed([WB], "test_reader", service=service)
    assert [a["pass_id"] for a in notes(service)[-1]["readers"][0]["overlapping_periodic_passes"]] == [72]


def test_orb_scope_is_excluded_even_with_misrouted_or_unknown_provider(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    service.broker_adapter.provider_by_account[SCHWAB] = "webull"
    shadow.note_consumed([SCHWAB], "orb_close_positions", service=service, provider="schwab")
    row = notes(service)[-1]["readers"][0]
    assert row["provider"] == "webull" and row["reader_provider_scope"] == "schwab"
    assert row["webull_cadence_eligible"] is False
    service.broker_adapter.provider_by_account.clear()
    shadow.note_consumed([SCHWAB], "orb_close_positions", service=service, provider="schwab")
    assert notes(service)[-1]["readers"][0]["provider"] == "UNMEASURED"


def test_real_acquisition_stamp_and_unknown_source_are_not_pass_id_guesses(monkeypatch):
    service, frame, _clock = harness(monkeypatch)
    assert frame.committed[WB]["acquisition_generation"] == "adapter_cache:99.0"
    adapter = service.broker_adapter._adapters_by_provider["webull"]
    adapter._positions_lock.acquire()
    try:
        unknown = shadow.Frame(72, AT, 100, {WB: {}})
        token = shadow.CURRENT.set(unknown)
        try:
            shadow.note_read(WB, "returned_not_wire_proof", service=service)
            shadow.note_committed([WB])
        finally:
            shadow.CURRENT.reset(token)
        assert unknown.committed[WB]["acquisition_generation"] == "UNMEASURED"
        service._wbquiet_shadow.retain(unknown, published=True)
        shadow.note_consumed([WB], "test_reader", service=service)
        assert notes(service)[-1]["readers"][0]["periodic_evidence"] == "UNMEASURED"
    finally:
        adapter._positions_lock.release()


def test_same_source_only_when_nonempty_immutable_cache_objects_match(monkeypatch):
    service, _frame, _clock = harness(monkeypatch)
    snapshots = service.broker_adapter.list_account_positions.return_value
    shadow.note_consumed([WB], "test_reader", service=service, source="adapter_positions",
                         positions=list(snapshots), symbol="IPDN")
    row = notes(service)[-1]["readers"][0]
    assert row["same_source_generation"] == "observed_adapter_cache_identity"
    assert row["reader_acquisition_generation"] == "adapter_cache:99.0"
    adapter = service.broker_adapter._adapters_by_provider["webull"]
    for returned, cached in [([], []), ([position()], snapshots)]:
        adapter._positions_cache[WB] = (99, cached)
        shadow.note_consumed([WB], "test_reader", service=service, source="adapter_positions",
                             positions=returned, symbol="IPDN")
        row = notes(service)[-1]["readers"][0]
        assert row["same_source_generation"] == "UNMEASURED"
        assert row["reader_acquisition_generation"] == "UNMEASURED"


def test_repeated_read_cannot_inherit_a_prior_source_acquisition_on_metadata_error(monkeypatch):
    service, frame, _clock = harness(monkeypatch)
    adapter = service.broker_adapter._adapters_by_provider["webull"]
    adapter._positions_cache[WB] = ("invalid-stamp", [])
    token = shadow.CURRENT.set(frame)
    try:
        shadow.note_read(WB, "returned_not_wire_proof", service=service, positions=[])
        shadow.note_committed([WB])
    finally:
        shadow.CURRENT.reset(token)
    assert frame.committed[WB]["adapter_calls"] == 2
    assert frame.committed[WB]["acquisition_generation"] == "UNMEASURED"
