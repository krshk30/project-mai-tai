"""SYNTHETIC SPHL-shaped controls; no copied production tables or native proof.

Only local SQLite and a controlled submit port are used. The incident symbol
and refusal points are retained; prices, source identity and rows are invented.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import logging
from shutil import copyfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Signal
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


def service_at(path, settings):
    engine = create_engine(f"sqlite+pysqlite:///{path}", poolclass=NullPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    venue = SimpleNamespace(submit_order=AsyncMock())
    service = OmsRiskService(settings, SimpleNamespace(xadd=AsyncMock()),
        session_factory=factory, broker_adapter=venue)
    service.seed_runtime_metadata()
    return service, engine


@pytest.fixture
def env(tmp_path):
    now = [datetime(2026, 10, 21, 13, 33, 22, tzinfo=UTC)]
    settings = Settings(_env_file=None, momentum_live_enabled=True, oms_adapter="paper",
        bot_control_enabled=False,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    path = tmp_path / "source.sqlite"
    service, engine = service_at(path, settings)
    runtime = OmsLiveRuntime(service, clock=lambda: now[0], identity="100:200")
    runtime.book = EpisodeLedger.restore([])
    quote = {"ask": D("3.00"), "bid": D("2.99"), "received_at": now[0]}
    runtime.quote = lambda symbol: quote
    sent = []

    async def submit(request):
        sent.append(request)
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="synthetic-native-id", symbol=request.symbol,
            side="buy", intent_type="open", quantity=request.quantity,
            origin="broker", reported_at=now[0])]

    service.broker_adapter.submit_order = submit

    async def claim(row, *args):
        result = await runtime._claim(row, *args)
        if result:
            # Synthetic creation clock makes date-filtered restore deterministic.
            def stamp(session):
                parent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == row["symbol"]))
                parent.created_at = now[0]
            await service._run_db(stamp)
        return result

    ports = ExecutionPorts(runtime.clock, runtime.quote, runtime.route_for,
        AsyncMock(return_value=False), claim, runtime._prepare,
        runtime._save, runtime._submit, runtime._record, runtime._route_report,
        request_quote_owner=AsyncMock(), refuse_unsent_entry=runtime._refuse_unsent_entry)
    executor = LiveEntryExecutor(runtime.book, ports, schwab_account=runtime.accounts[0],
                                 webull_account=runtime.accounts[1])
    signal = Signal("2026-10-21", "SPHL", "SYNTHETIC-momstuck2", 100, 200,
        D("3.00"), runtime.now_ns(), 50000, country_route="webull",
        country_category="sec_unknown_at_decision")
    h = SimpleNamespace(now=now, settings=settings, path=path, service=service,
        engine=engine, runtime=runtime, quote=quote, sent=sent,
        ports=ports, executor=executor, signal=signal)
    yield h
    engine.dispose()


def change_at(h, point):
    if point == "after_durable":
        original = h.ports.durable_order

        async def prepare(*args):
            await original(*args)
            h.quote["ask"] = D("3.01")

        h.ports.durable_order = prepare
    else:
        original = h.ports.save_episode

        async def save(row):
            await original(row)
            if row["state"] == "submitting":
                h.quote["ask"] = D("3.01")

        h.ports.save_episode = save


def durable(h):
    with h.service.session_factory() as session:
        return (session.scalar(select(TradeIntent).where(TradeIntent.symbol == "SPHL")),
                session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "SPHL")))


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_durable", "before_wire"])
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("hour", [8, 13, 19], ids=["premarket", "open", "afternoon"])
@pytest.mark.parametrize("controlled", [False, True], ids=["no-control", "controlled-generation"])
async def test_sphl_shaped_refusal_settles_both_rows_releases_slot_and_admits_next_symbol(
    env, point, provider, hour, controlled, caplog, monkeypatch,
):
    h = env
    monkeypatch.setattr(logging.getLogger("momentum-live"), "handlers", [caplog.handler])
    caplog.set_level(logging.INFO, logger="momentum-live")
    if controlled:
        h.ports.entry_metadata = lambda signal: {"bot_control_generation": "0"}
    h.now[0] = h.now[0].replace(hour=hour)
    # Schwab only opens at 07 ET. Its premarket control uses 08 ET.
    if provider == "schwab" and hour == 8:
        h.now[0] = h.now[0].replace(hour=12)
    h.quote["received_at"] = h.now[0]
    signal = replace(h.signal, country_route=provider, observed_ns=h.runtime.now_ns())
    # Occupy the other slot: the next symbol can trade only if SPHL is released.
    other = replace(signal, symbol="HOLD", source_id="SYNTHETIC-held-slot")
    h.runtime.book.claim(other, now_ns=h.runtime.now_ns())
    change_at(h, point)
    with pytest.raises(Refusal, match=f"quote_changed_{point}"):
        await h.executor.admit(signal)
    parent, order = durable(h)
    assert parent.status == "cancelled" and order.status == "aborted"
    assert parent.payload["momlive_episode"]["state"] == "terminal_no_fill"
    assert parent.payload["momlive_episode"]["source_ids"] == [signal.source_id]
    assert order.broker_order_id is None and order.submitted_at is None
    assert order.payload["reject_reason"] == "refused_before_wire"
    assert order.payload["momlive_entry_never_dispatched"] is True
    assert order.payload["momstuck2_refusal"]["context"]["ask"] == "3.01"
    assert h.runtime.book._active_count == 1 and "SPHL" not in h.runtime.book._active_by_symbol
    assert not h.sent
    assert "no_wire=true settlement=refused_before_wire" in caplog.text
    assert "ask=3.01 ask_age_ms=0.0" in caplog.text and "cap=3.045" in caplog.text
    assert (await h.executor.admit(signal))[1] is None
    assert not h.sent  # Same SPHL source cannot be resubmitted after release.
    h.ports.durable_order, h.ports.save_episode = h.runtime._prepare, h.runtime._save
    _, request, _ = await h.executor.admit(replace(signal, symbol="NEXT", source_id="SYNTHETIC-next"))
    assert request.symbol == "NEXT" and len(h.sent) == 1 and h.runtime.book._active_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_durable", "before_wire"])
@pytest.mark.parametrize("fault", ["stale", "future", "cap", "expired"])
async def test_recheck_refusals_also_settle_without_a_changed_price(env, point, fault):
    h = env
    original = h.ports.durable_order if point == "after_durable" else h.ports.save_episode

    async def change(*args):
        await original(*args)
        if point == "before_wire" and args[0]["state"] != "submitting":
            return
        if fault == "stale":
            h.quote["received_at"] -= timedelta(seconds=3)
        elif fault == "future":
            h.quote["received_at"] += timedelta(microseconds=1)
        elif fault == "cap":
            h.quote["ask"] = D("4")
        else:
            h.now[0] += timedelta(seconds=3)

    if point == "after_durable":
        h.ports.durable_order = change
    else:
        h.ports.save_episode = change
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    assert durable(h)[1].status == "aborted"
    assert h.runtime.book._active_count == 0 and not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["broker", "client", "unknown", "timeout", "refusal"])
async def test_actual_submit_attempt_never_enters_unsent_settlement(env, result):
    h = env
    settle = AsyncMock(wraps=h.runtime._refuse_unsent_entry)
    h.ports.refuse_unsent_entry = settle

    async def submit(request):
        h.sent.append(request)
        if result == "timeout":
            raise TimeoutError("SYNTHETIC lost ACK")
        if result == "refusal":
            raise Refusal("SYNTHETIC adapter failure after possible send")
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="synthetic-native-id" if result == "broker" else None,
            symbol="SPHL", side="buy", intent_type="open", quantity=request.quantity,
            origin=result, reported_at=h.now[0])]

    h.ports.submit = submit
    if result in {"timeout", "refusal"}:
        with pytest.raises(TimeoutError if result == "timeout" else Refusal):
            await h.executor.admit(h.signal)
    else:
        await h.executor.admit(h.signal)
    settle.assert_not_awaited()
    assert h.runtime.book._active_count == 1
    assert durable(h)[0].payload["momlive_episode"]["state"] == "submitting"
    assert "momlive_entry_never_dispatched" not in durable(h)[1].payload
    assert (await h.executor.admit(h.signal))[1] is None and len(h.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["native_id", "submitted", "permit", "event", "accepted", "ack_unknown"])
async def test_conflicting_dispatch_evidence_blocks_settlement_and_keeps_slot(env, evidence):
    h = env
    prepare = h.ports.durable_order

    async def changed(*args):
        await prepare(*args)
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder))
            if evidence == "native_id":
                order.broker_order_id = "possibly-sent"
            elif evidence == "submitted":
                order.submitted_at = h.now[0]
            elif evidence == "permit":
                order.payload = {**order.payload, "bot_control_permit": "MAY_HAVE_SENT"}
            elif evidence == "ack_unknown":
                order.payload = {**order.payload, "momcancel1_ack_identity": "UNKNOWN"}
            elif evidence == "accepted":
                order.status = "accepted"
            else:
                session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected",
                    event_source="client", payload={"reason": "unknown wire outcome"}))
            session.commit()
        h.quote["ask"] = D("3.01")

    h.ports.durable_order = changed
    with pytest.raises(Refusal, match="prewire_settlement_.*UNKNOWN"):
        await h.executor.admit(h.signal)
    assert h.runtime.book._active_count == 1 and not h.sent
    parent, order = durable(h)
    assert parent.payload["momlive_episode"]["state"] == "claimed"
    assert "momlive_entry_never_dispatched" not in order.payload


@pytest.mark.asyncio
async def test_settlement_database_failure_keeps_ram_reserved(env):
    h = env
    change_at(h, "before_wire")
    h.ports.refuse_unsent_entry = AsyncMock(side_effect=RuntimeError("SYNTHETIC DB unavailable"))
    with pytest.raises(RuntimeError, match="DB unavailable"):
        await h.executor.admit(h.signal)
    assert h.runtime.book._active_count == 1 and not h.sent
    assert durable(h)[1].status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["after_durable", "before_wire"])
async def test_next_day_boot_from_copy_of_synthetic_settled_tables(env, point, tmp_path):
    h = env
    change_at(h, point)
    with pytest.raises(Refusal, match="quote_changed"):
        await h.executor.admit(h.signal)
    # Reopen the settled row on the SAME day first: source idempotence survives restore.
    rows = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, h.now[0].replace(hour=4, minute=0, second=0)), commit=False)
    restored_book = EpisodeLedger.restore(rows["episodes"])
    assert restored_book.claim(h.signal, now_ns=h.runtime.now_ns())[1] is False
    assert restored_book._active_count == 0
    # A real file copy, but ONLY of the synthetic fixture, never tonight's export.
    h.engine.dispose()
    copied_path = tmp_path / "synthetic-next-day.sqlite"
    copyfile(h.path, copied_path)
    copied_service, copied_engine = service_at(copied_path, h.settings)
    h.now[0] += timedelta(days=1)
    restored = OmsLiveRuntime(copied_service, clock=lambda: h.now[0], identity="101:201")
    try:
        assert await restored.start()
        assert restored.book.ready and restored.book._active_count == 0
        assert restored.actor.restored and not restored.actor.work.owners
        with copied_service.session_factory() as session:
            parent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == "SPHL"))
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "SPHL"))
            assert parent.payload["momlive_episode"]["state"] == "terminal_no_fill"
            assert order.status == "aborted" and order.broker_order_id is None
        assert not h.sent
    finally:
        await restored.close()
        copied_engine.dispose()


@pytest.mark.asyncio
async def test_legacy_claimed_pending_row_still_fails_boot_without_external_proof(env):
    h = env
    change_at(h, "after_durable")
    # Emulate the OLD code's missing cleanup, not a copied SPHL row.
    h.ports.refuse_unsent_entry = AsyncMock(side_effect=Refusal("SYNTHETIC old cleanup absent"))
    with pytest.raises(Refusal):
        await h.executor.admit(h.signal)
    h.now[0] += timedelta(days=1)
    restored = OmsLiveRuntime(h.service, clock=lambda: h.now[0], identity="101:201")
    with pytest.raises(Refusal, match="boot_quote_reservation_order_or_exposure_UNKNOWN"):
        await restored.start()
    assert durable(h)[1].status == "pending" and not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("has_ack", [False, True], ids=["unknown-send", "broker-accepted"])
async def test_next_day_restore_keeps_sent_or_unknown_episode_and_never_resends(env, has_ack):
    h = env
    if has_ack:
        await h.executor.admit(h.signal)
    else:
        async def unknown(request):
            h.sent.append(request)
            raise TimeoutError("SYNTHETIC send ACK missing")

        h.ports.submit = unknown
        with pytest.raises(TimeoutError):
            await h.executor.admit(h.signal)
    h.now[0] += timedelta(days=1)
    bundle = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, h.now[0].replace(hour=4, minute=0, second=0)), commit=False)
    book = EpisodeLedger.restore(bundle["episodes"])
    assert book._active_count == 1
    episode = next(iter(book.episodes.values()))
    assert episode.state == "submitting" and episode.first_submit_ns is not None
    assert book.claim(h.signal, now_ns=h.runtime.now_ns())[1] is False
    assert len(h.sent) == 1 and "momlive_entry_never_dispatched" not in durable(h)[1].payload
