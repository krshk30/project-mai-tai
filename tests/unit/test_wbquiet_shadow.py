from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, TradeIntent
from project_mai_tai.oms import wbquiet_shadow as shadow
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore


AT = datetime(2026, 10, 8, 14, tzinfo=UTC)
NAME = "live:orb"


@pytest.mark.parametrize("facts,at,expected", [
    ({"held": True}, AT, ("held", 15)),
    ({"broker_held": True}, AT, ("held", 15)),
    ({"working": True}, AT, ("working", 15)),
    ({"fresh_fill": True}, AT, ("fresh-fill", 15)),
    ({"known": True, "fresh": True}, AT, ("flat", 60)),
    ({"known": True, "fresh": True}, datetime(2026, 10, 9, 0, tzinfo=UTC), ("overnight", 300)),
    ({"known": True, "fresh": True}, datetime(2026, 10, 9, 8, tzinfo=UTC), ("flat", 60)),
    ({"known": True, "fresh": False}, AT, ("unknown", 15)),
    ({"fresh": True}, AT, ("unknown", 15)),
    ({"configured": False}, AT, ("no_credentials", None)),
    ({"held": True, "working": True, "fresh_fill": True}, AT, ("held", 15)),
])
def test_rule_a_classification_is_only_a_nominal_model(facts, at, expected):
    assert shadow.classify(facts, at) == expected


@pytest.mark.parametrize("stamp,backoff,fresh,age", [
    (99, 0, True, 1), (80, 0, False, 20), (99, 101, False, 1),
    (101, 0, False, None), (float("nan"), 0, False, None),
])
def test_cache_age_is_actual_metadata_never_a_fresh_pass_guess(stamp, backoff, fresh, age):
    adapter = SimpleNamespace(_positions_lock=threading.Lock(),
                              _positions_cache={NAME: (stamp, [])},
                              _positions_backoff_until={NAME: backoff},
                              _positions_throttle_secs=10)
    receipt = shadow.source_evidence(adapter, NAME, 100)
    assert receipt["fresh"] is fresh
    assert receipt["source_age_seconds"] == age


def test_contested_cache_lock_and_unconstructed_router_do_not_create_work():
    lock = threading.Lock()
    adapter = SimpleNamespace(_positions_lock=lock)
    lock.acquire()
    try:
        assert shadow.source_evidence(adapter, NAME, 100)["fresh"] is False
    finally:
        lock.release()
    router = SimpleNamespace(provider_by_account={NAME: "webull"}, _adapters_by_provider={})
    assert shadow.cached_adapter(router, NAME) == ("webull", None)
    assert router._adapters_by_provider == {}


@pytest.mark.asyncio
async def test_nominal_due_clock_truthful_readers_and_unreadable_veto():
    observer = shadow.ShadowObserver()
    logger = Mock()
    service = SimpleNamespace(logger=logger)
    for started, actual in [(100, "returned_not_wire_proof"),
                            (115, "returned_not_wire_proof"), (130, "unreadable")]:
        frame = shadow.Frame(1, AT, started,
                             {NAME: {"known": True, "fresh": True}}, {NAME: actual})
        frame.readers = [(NAME, "virtual_clear", 1), (NAME, "virtual_restore", 61)]
        await observer.finish(service, frame, "ok")
    receipts = [json.loads(call.args[1]) for call in logger.info.call_args_list]
    assert [r["accounts"][0]["would"] for r in receipts] == ["read", "skip", "read"]
    assert receipts[-1]["accounts"][0]["state"] == "unknown"
    assert receipts[0]["accounts"][0]["covered_readers"][1]["within_60s"] is False
    assert all(r["other_oms_and_external_readers"] == "UNMEASURED" for r in receipts)
    assert all(r["wire_calls_saved"] == "UNMEASURED" and r["policy_applied"] is False
               for r in receipts)


@pytest.mark.asyncio
async def test_worker_timeout_is_bounded_and_has_no_backlog(monkeypatch):
    monkeypatch.setattr(shadow, "WAIT_SECONDS", 0.01)
    observer = shadow.ShadowObserver()
    release = threading.Event()
    entered = threading.Event()

    def blocked():
        entered.set()
        release.wait(1)

    started = time.monotonic()
    try:
        assert await observer.off_loop(blocked) is None
        assert entered.is_set()
        for _ in range(200):
            assert await observer.off_loop(lambda: pytest.fail("queued worker")) is None
        assert time.monotonic() - started < 0.2
        assert observer.busy.locked()
        assert observer.dropped == 201
    finally:
        release.set()
        for _ in range(100):
            if not observer.busy.locked():
                break
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_240_events_per_second_have_no_observer_sql_or_tick_work():
    observer = shadow.ShadowObserver()
    threads = []
    loop_thread = threading.get_ident()
    await observer.off_loop(lambda: threads.append(threading.get_ident()))
    assert threads == [threads[0]] and threads[0] != loop_thread
    # No tick hook exists: the only entry point is the periodic ContextVar.
    service = SimpleNamespace(broker_adapter=Mock(), session_factory=Mock())
    started = time.monotonic()
    for _ in range(240):
        assert await shadow.prepare(service, None) == (None, None)
    assert time.monotonic() - started < 0.05
    service.session_factory.assert_not_called()
    assert not service.broker_adapter.mock_calls


def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shadow.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def adapter():
    return SimpleNamespace(accounts_by_name={NAME: object()}, app_key="not-logged",
                           app_secret="not-logged", _positions_lock=threading.Lock(),
                           _positions_cache={NAME: (time.monotonic(), [])},
                           _positions_backoff_until={}, _positions_throttle_secs=10)


@pytest.mark.asyncio
async def test_observation_sql_is_off_loop_read_only_and_books_hold_promotes(tmp_path):
    sessions = factory(tmp_path)
    account_id = uuid4()
    with sessions() as session:
        session.add(BrokerAccount(id=account_id, name=NAME, provider="webull",
                                  environment="live", is_active=True))
        session.add(AccountPosition(broker_account_id=account_id, symbol="IPDN",
                                    quantity=Decimal("1000"), average_price=Decimal("4.52")))
        session.commit()
    threads = []

    def observed_session():
        threads.append(threading.get_ident())
        return sessions()

    observer = shadow.ShadowObserver()
    service = SimpleNamespace(session_factory=observed_session, broker_adapter=adapter())
    frame = await observer.prepare(service, [NAME])
    assert frame is not None
    assert shadow.classify(frame.accounts[NAME], frame.at) == ("held", 15)
    assert threads and all(t != threading.get_ident() for t in threads)
    with sessions() as session:
        assert session.scalar(select(AccountPosition.quantity)) == Decimal("1000")


@pytest.mark.parametrize("state", ["working", "fresh-buy", "fresh-sell", "old-fill",
                                  "held-intent", "created-intent", "submitted-intent"])
def test_book_query_uses_pending_orders_and_both_recent_fill_sides(tmp_path, state):
    sessions = factory(tmp_path)
    account_id, strategy_id, order_id = uuid4(), uuid4(), uuid4()
    with sessions() as session:
        session.add(BrokerAccount(id=account_id, name=NAME, provider="webull",
                                  environment="live", is_active=True))
        session.add(BrokerOrder(id=order_id, strategy_id=strategy_id, broker_account_id=account_id,
                                client_order_id="recorded-shadow-test", symbol="TEST", side="buy",
                                order_type="limit", time_in_force="day", quantity=Decimal("1"),
                                status="accepted" if state == "working" else "filled"))
        if state.endswith("-intent"):
            session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=account_id,
                                    symbol="TEST", side="buy", intent_type="open", quantity=Decimal("1"),
                                    reason="observation test", status=state.removesuffix("-intent")))
        elif state != "working":
            session.add(Fill(order_id=order_id, strategy_id=strategy_id,
                             broker_account_id=account_id, symbol="TEST", quantity=Decimal("1"),
                             price=Decimal("2"), side="sell" if state == "fresh-sell" else "buy",
                             filled_at=AT - timedelta(minutes=11 if state == "old-fill" else 1)))
        session.commit()
    row = shadow.sample_books(sessions, [NAME], AT)[NAME]
    assert row["working"] is (state == "working" or state.endswith("-intent"))
    assert row["fresh_fill"] is (state in {"fresh-buy", "fresh-sell"})
    assert row["held"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "enabled", "broken"])
@pytest.mark.parametrize("failure", [None, "broker", "persist", "cancel"])
async def test_real_sync_operations_results_and_exceptions_unchanged(tmp_path, mode, failure):
    calls = []
    sessions = factory(tmp_path)
    with sessions() as session:
        session.add(BrokerAccount(name=NAME, provider="webull", environment="live", is_active=True))
        session.commit()
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = SimpleNamespace(oms_native_oco_confirmation_max_age_seconds=30,
                                      oms_virtual_position_clear_min_age_seconds=24.119)
    service.session_factory = sessions
    service.logger = Mock()
    broker = adapter()

    async def positions(name):
        calls.append("positions")
        if failure == "broker":
            raise RuntimeError("broker unreadable")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return [BrokerPositionSnapshot(broker_account_name=name, symbol="TEST",
                                       quantity=Decimal("1"), average_price=Decimal("2"),
                                       market_value=None, as_of=None)]

    broker.list_account_positions = positions
    service.broker_adapter = broker

    class Store(OmsStore):
        def sync_account_positions(self, session, **kwargs):
            calls.append("persist")
            if failure == "persist":
                raise RuntimeError("persist failed")
            return super().sync_account_positions(session, **kwargs)

        def clear_virtual_positions_without_account_backing(self, *args, **kwargs):
            calls.append("clear")
            return super().clear_virtual_positions_without_account_backing(*args, **kwargs)

        def restore_virtual_positions_from_managed(self, *args, **kwargs):
            calls.append("restore")
            return super().restore_virtual_positions_from_managed(*args, **kwargs)

    service.store = Store()

    async def actual_pass(**kwargs):
        calls.append("orders")
        return await service.sync_broker_positions(**kwargs)

    service._sync_broker_state_pass = actual_pass
    if mode == "broken":
        class Broken:
            async def prepare(self, *args):
                raise RuntimeError("broken observer")
        service._wbquiet_shadow = Broken()
    token = shadow.PERIODIC.set(mode != "disabled")
    try:
        if failure == "persist":
            with pytest.raises(RuntimeError, match="persist failed"):
                await service.sync_broker_state()
        elif failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await service.sync_broker_state()
        else:
            assert await service.sync_broker_state() == {
                "accounts": 1, "positions": 0 if failure == "broker" else 1,
            }
    finally:
        shadow.PERIODIC.reset(token)
    assert shadow.CURRENT.get() is None
    assert calls == (["orders", "positions"] if failure == "cancel" else
                     ["orders", "positions", "persist"] if failure == "persist" else
                     ["orders", "positions", "clear", "restore"] if failure == "broker" else
                     ["orders", "positions", "persist", "clear", "restore"])
    receipts = [json.loads(c.args[1]) for c in service.logger.info.call_args_list
                if c.args[0] == "[WBQUIET-SHADOW] %s"]
    if mode == "enabled" and failure not in ("cancel", "persist"):
        assert len(receipts) == 1
        assert len(receipts[0]["accounts"][0]["covered_readers"]) == (0 if failure == "broker" else 3)
        assert "not-logged" not in json.dumps(receipts)


@pytest.mark.asyncio
async def test_logging_failure_does_not_escape_or_retain_lock():
    observer = shadow.ShadowObserver()
    logger = Mock()
    logger.info.side_effect = RuntimeError("logger broken")
    frame = shadow.Frame(1, AT, time.monotonic(), {NAME: {"known": True, "fresh": True}},
                         {NAME: "returned_not_wire_proof"})
    await observer.finish(SimpleNamespace(logger=logger), frame, "ok")
    assert observer.dropped == 1
    assert not observer.busy.locked()


def test_reader_memory_is_bounded_and_missing_pass_never_becomes_zero():
    frame = shadow.Frame(1, AT, time.monotonic(), {NAME: {}})
    token = shadow.CURRENT.set(frame)
    try:
        for _ in range(1000):
            shadow.note_consumed([NAME], "virtual_clear")
        assert len(frame.readers) == shadow.MAX_READERS
        shadow.note_read("unobserved-account", "returned_not_wire_proof")
        assert not frame.actual
    finally:
        shadow.CURRENT.reset(token)


@pytest.mark.asyncio
async def test_account_overflow_fails_observation_not_sync():
    observer = shadow.ShadowObserver()
    service = SimpleNamespace(
        broker_adapter=SimpleNamespace(provider_by_account={f"a{i}": "webull" for i in range(17)},
                                       _adapters_by_provider={}), session_factory=Mock(),
    )
    assert await observer.prepare(service, None) is None
    assert observer.dropped == 1
    service.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_unreached_read_does_not_advance_nominal_clock():
    observer = shadow.ShadowObserver()
    logger = Mock()
    frame = shadow.Frame(1, AT, 100, {NAME: {"known": True, "fresh": True}})
    await observer.finish(SimpleNamespace(logger=logger), frame, "failed")
    record = json.loads(logger.info.call_args.args[1])["accounts"][0]
    assert record["would"] == "not_evaluated" and record["state"] == "unknown"
    assert not observer.last_nominal
