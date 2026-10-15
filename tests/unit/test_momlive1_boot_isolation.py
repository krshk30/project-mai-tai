import asyncio
from datetime import UTC, datetime
from decimal import Decimal as D
import logging

import pytest
from sqlalchemy import select, text

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.models import BrokerOrder, Strategy
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Signal
from project_mai_tai.momentum_live.persistence import LivePersistence
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_oms_risk_service import FakeRedis, build_test_session_factory


class BootRedis(FakeRedis):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.quote_sent = False

    async def xrevrange(self, stream, count=1):
        if self.case == "redis_timeout":
            raise TimeoutError("controlled owner-proof read timeout")
        if self.case == "malformed_off_owner":
            return [("1-0", {"data": "not-json"})]
        return []

    async def xread(self, offsets, block=0, count=0):
        if not self.quote_sent:
            self.quote_sent = True
            quote = QuoteTickEvent(source_service="market-data", payload=QuoteTickPayload(
                symbol="ATRBOOT", bid_price=D(3), ask_price=D("3.01")))
            return [(next(iter(offsets)), [("1-0", {"data": quote.model_dump_json()})])]
        await asyncio.Event().wait()


def seed_unknown_reservation(service):
    now = datetime.now(UTC)
    signal = Signal(str(now.date()), "SCNI", "boot-only", 1, 2, D(3), int(now.timestamp()*1e9), 50000)
    book = EpisodeLedger.restore([])
    episode, _ = book.claim(signal, now_ns=signal.observed_ns)
    db = LivePersistence(service.store)
    with service.session_factory() as session:
        assert db.claim(session, episode=book.snapshot()[0], account="live:orb", provider="webull", signal={})
        from project_mai_tai.broker_adapters.protocols import OrderRequest
        request = OrderRequest("unknown-mom-boot", "live:orb", "momentum_warn_live",
            "SCNI", "buy", "open", D(9), "controlled", {"episode_id": episode.episode_id}, "limit", "day")
        db.prepare_order(session, episode.episode_id, request, provider="webull")
        session.commit()


def boot_service(case):
    settings = Settings(_env_file=None, redis_stream_prefix="test", oms_adapter="paper",
        momentum_live_enabled=case == "on_reservation_unknown",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    service = OmsRiskService(settings, BootRedis(case), session_factory=build_test_session_factory(),
        broker_adapter=SimulatedBrokerAdapter())
    if case == "on_reservation_unknown":
        service.seed_runtime_metadata()
        seed_unknown_reservation(service)
    return service


async def exercise_actual_boot_and_atr(monkeypatch, service, caplog):
    from project_mai_tai.oms import service as module
    logger = logging.getLogger("momlive1-boot-isolation-control")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.CRITICAL)
    monkeypatch.setattr(service, "logger", logger)
    monkeypatch.setattr(module, "_install_signal_handlers", lambda _: None)
    # Do not create a separate receipt pool in this in-memory controlled database.
    monkeypatch.setattr(module, "receipt_session_factory", lambda _: (_ for _ in ()).throw(
        RuntimeError("controlled receipt writer unavailable")))
    tick_seen = asyncio.Event()
    handler = service._handle_quote_tick_event
    async def actual_quote(event):
        await handler(event)
        tick_seen.set()
    monkeypatch.setattr(service, "_handle_quote_tick_event", actual_quote)
    results = []
    async def control(stop):
        await asyncio.wait_for(tick_seen.wait(), 2)
        assert service._latest_quotes_by_symbol["ATRBOOT"]["ask"] == 3.01
        assert service._momentum_live_actor is None and service._momentum_live_quote_dispatch is None
        event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=service.settings.strategy_schwab_1m_v2_account_name,
            symbol="ATRBOOT", side="buy", quantity=D(200), intent_type="open",
            reason="ATR Flip controlled boot neighbour", metadata={"reference_price": "3", "entry_size_price": "3"}))
        results.extend(await service.process_trade_intent(event))
        stop.set()
    monkeypatch.setattr(service, "_run_control_loop", control)
    await asyncio.wait_for(service.run(), 5)
    assert [event.payload.status for event in results] == ["accepted", "filled"], [
        (event.payload.status, event.payload.reason) for event in results]
    with service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).join(Strategy).where(
            Strategy.code == "schwab_1m_v2", BrokerOrder.symbol == "ATRBOOT"))
        assert order is not None and order.status == "filled" and order.quantity == 200
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tables_missing", "redis_timeout", "malformed_off_owner",
                                 "on_reservation_unknown", "normal_off_empty"])
async def test_mom_boot_failure_never_blocks_actual_tick_consumer_and_atr_intent(case, monkeypatch, caplog):
    service = boot_service(case)
    if case == "tables_missing":
        # MOM-only restore SQL fault; shared ATR tables remain available. No
        # production claim that the current restore uses a dedicated MOM table.
        def missing_table(self, session, day_start):
            return session.execute(text("SELECT * FROM controlled_missing_mom_restore_table")).all()
        monkeypatch.setattr(LivePersistence, "restore_bundle", missing_table)
    with caplog.at_level(logging.CRITICAL):
        await exercise_actual_boot_and_atr(monkeypatch, service, caplog)
    if case == "normal_off_empty":
        assert "[MOMLIVE1-BOOT-REFUSED]" not in caplog.text
    else:
        assert "[MOMLIVE1-BOOT-REFUSED]" in caplog.text
        assert "symbol=UNKNOWN quantity=UNKNOWN account=UNKNOWN" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_exception_cannot_reverse_atr_boot_isolation(monkeypatch, caplog):
    service = boot_service("malformed_off_owner")
    async def broken_close(self):
        raise RuntimeError("controlled cleanup failure")
    monkeypatch.setattr(OmsLiveRuntime, "close", broken_close)
    with caplog.at_level(logging.CRITICAL):
        await exercise_actual_boot_and_atr(monkeypatch, service, caplog)
    assert "reason=cleanup_RuntimeError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, KeyboardInterrupt])
async def test_base_exceptions_from_mom_start_propagate_before_tick_consumer(failure, monkeypatch):
    service = boot_service("normal_off_empty")
    from project_mai_tai.oms import service as module
    monkeypatch.setattr(module, "_install_signal_handlers", lambda _: None)
    async def interrupt(self):
        raise failure()
    monkeypatch.setattr(OmsLiveRuntime, "start", interrupt)
    with pytest.raises(failure):
        await service.run()
    assert not service.redis.quote_sent


@pytest.mark.asyncio
async def test_failed_held_restore_logs_inspected_owned_quantity_account_and_preserves_atr(monkeypatch, caplog):
    from tests.unit.test_momlive1_runtime import event, offer_and_drain, setup
    now, broker, service, runtime = setup()
    await runtime.start()
    await offer_and_drain(runtime, event(now[0]))
    await runtime.close()
    service.redis = BootRedis("held_restore")
    submit, atr_adapter = broker.submit_order, SimulatedBrokerAdapter()
    async def existing_adapters(request):
        return await atr_adapter.submit_order(request) if request.strategy_code == "schwab_1m_v2" else await submit(request)
    broker.submit_order = existing_adapters
    cancelled_before = list(broker.cancelled)
    async def protect_refused(self, position):
        raise Refusal("controlled held protection read refused")
    monkeypatch.setattr(OmsLiveRuntime, "_protect", protect_refused)
    with caplog.at_level(logging.CRITICAL):
        await exercise_actual_boot_and_atr(monkeypatch, service, caplog)
    assert "symbol=SCNI quantity=33.00000000 account=live:orb" in caplog.text
    assert "controlled held protection read refused" in caplog.text
    assert broker.cancelled == cancelled_before


@pytest.mark.asyncio
async def test_partial_start_cancel_at_initial_publisher_stops_all_mom_tasks_and_propagates(monkeypatch):
    from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher
    from project_mai_tai.oms import service as module
    service = boot_service("normal_off_empty")
    service.settings.momentum_live_enabled = True
    monkeypatch.setattr(module, "_install_signal_handlers", lambda _: None)
    started, tasks = asyncio.Event(), []
    # Keep the actual start path and actor task creation. The publisher owns the
    # same lock as the runtime; capture the runtime through its existing factory.
    original_init = OmsLiveRuntime.__init__
    captured = []
    def capture(self, *args, **kwargs):
        kwargs.setdefault("identity", "123:456")  # Controlled Linux PID:ticks, no /proc on macOS.
        original_init(self, *args, **kwargs)
        captured.append(self)
    async def initial_wait(self):
        tasks.extend(captured[0].actor.tasks)
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(OmsLiveRuntime, "__init__", capture)
    monkeypatch.setattr(OwnerProofPublisher, "publish_once", initial_wait)
    runner = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert len(tasks) == 4 and any(not task.done() for task in tasks)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert all(task.done() for task in tasks)
    assert not captured[0].actor.restored
    assert service._momentum_live_actor is None and service._momentum_live_quote_dispatch is None
    assert not service.redis.quote_sent


@pytest.mark.asyncio
async def test_partial_publisher_failure_and_close_exception_stop_tasks_preserve_actual_atr(monkeypatch, caplog):
    from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher
    service = boot_service("normal_off_empty")
    service.settings.momentum_live_enabled = True
    tasks, captured = [], []
    original_init = OmsLiveRuntime.__init__
    def capture(self, *args, **kwargs):
        kwargs.setdefault("identity", "123:456")
        original_init(self, *args, **kwargs)
        captured.append(self)
    async def failed_publisher(self):
        tasks.extend(captured[0].actor.tasks)
        assert len(tasks) == 4 and any(not task.done() for task in tasks)
        raise RuntimeError("controlled partial-start publisher failure")
    async def failed_close(self):
        raise RuntimeError("controlled partial-start close failure")
    monkeypatch.setattr(OmsLiveRuntime, "__init__", capture)
    monkeypatch.setattr(OwnerProofPublisher, "publish_once", failed_publisher)
    monkeypatch.setattr(OmsLiveRuntime, "close", failed_close)
    with caplog.at_level(logging.CRITICAL):
        await exercise_actual_boot_and_atr(monkeypatch, service, caplog)
    assert len(tasks) == 4 and all(task.done() for task in tasks)
    assert not captured[0].actor.restored
    assert "controlled partial-start publisher failure" in caplog.text
    assert "controlled partial-start close failure" in caplog.text
