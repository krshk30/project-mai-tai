"""Read-only-source PG workload/GC assessment; never a token-proof certificate.

Main has no submission-token protocol or its test. This common harness compares
the same OMS quote/BUY/SELL workload on untouched source trees. The full token
test remains a separate mandatory check on F/I.
"""

import asyncio
from collections import Counter
from decimal import Decimal
import gc
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep
import tracemalloc
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerAccount, Strategy, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


def rss_kib():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("current RSS unreadable")


async def noop(*_args, **_kwargs):
    return None


async def workload(factory):
    leaf = object.__new__(WebullBrokerAdapter)
    leaf.accounts_by_name = {"live:orb": WebullAccountConfig(account_id="ACC1")}
    wires, wire_times = [], {}

    async def wire(request):
        wires.append((request.side, request.intent_type))
        wire_times[request.symbol] = monotonic()
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="controlled-" + request.client_order_id,
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, metadata=request.metadata)]

    leaf.submit_order = wire
    routing = RoutingBrokerAdapter(default_provider="webull",
        provider_by_account={"live:orb": "webull"}, factories_by_provider={"webull": lambda: leaf})
    service = OmsRiskService(Settings(oms_adapter="simulated", broker_default_provider="webull",
        orb_broker_account_name="unused"), SimpleNamespace(),
        session_factory=factory, broker_adapter=routing)
    service._evaluate_risk = lambda _event: (True, "controlled")
    service._market_is_fillable = lambda *_args: True
    service._reconcile_after_intent = noop
    service._publish_order_event = noop
    if hasattr(service.broker_adapter, "start"):
        await service.broker_adapter.start()
    with factory() as session:
        strategy = Strategy(code="macd_30s", name="controlled", execution_mode="live")
        account = BrokerAccount(name="live:orb", provider="webull", environment="test")
        session.add_all([strategy, account,
            Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")])
        session.flush()
        for index in range(1, 200, 2):
            session.add_all([
                VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                    symbol=f"EXIT{index}", quantity=Decimal(1), average_price=Decimal(2)),
                AccountPosition(broker_account_id=account.id, symbol=f"EXIT{index}",
                    quantity=Decimal(1), average_price=Decimal(2)),
            ])
        session.commit()

    def intent(side, index):
        return TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
            strategy_code="macd_30s", broker_account_name="live:orb",
            symbol=f"BUY{index}" if side == "buy" else f"EXIT{index}", side=side,
            quantity=Decimal(1), intent_type="open" if side == "buy" else "close",
            reason=f"CONTROLLED_{index}", metadata={"reference_price": "2"}))

    def quote():
        return QuoteTickEvent(source_service="test", payload=QuoteTickPayload(
            symbol="DKI", bid_price=Decimal(2), ask_price=Decimal("2.01")))

    old_threshold = gc.get_threshold()
    frozen = os.environ.get("GC_POLICY") == "freeze"
    tracing = os.environ.get("ALLOCATION_TRACE") == "1"
    if frozen:
        gc.freeze()
        gc.set_threshold(old_threshold[0], old_threshold[1], max(100, old_threshold[2]))
    frozen_count = gc.get_freeze_count()
    if tracing:
        tracemalloc.start(3)
        allocation_before = tracemalloc.take_snapshot()
    rss_before, rss_samples = rss_kib(), []
    started_gc, gc_spans = {}, []

    def on_gc(phase, info):
        generation = info["generation"]
        if phase == "start":
            started_gc[generation] = monotonic()
        elif generation in started_gc:
            elapsed = (monotonic() - started_gc.pop(generation)) * 1000
            if generation == 2 and len(gc_spans) < 32:
                frame, frames = sys._getframe(1), []
                while frame is not None and len(frames) < 12:
                    frames.append((frame.f_code.co_filename, frame.f_code.co_name, frame.f_lineno))
                    frame = frame.f_back
                del frame
                gc_spans.append(dict(ms=elapsed, collected=info["collected"], frames=frames))

    gc.callbacks.append(on_gc)
    quote_ms, close_ms, loop_ms, pump_quote_ms = [], [], [], []
    before_wire, after_wire = [], []
    baseline = monotonic()

    async def buys():
        for index in range(25):
            await service.process_trade_intent(intent("buy", index))
            await asyncio.sleep(0.4)

    async def pump():
        for index in range(14_400):
            due = baseline + index / 240
            await asyncio.sleep(max(0, due - monotonic()))
            begin = monotonic()
            loop_ms.append(max(0, begin - due) * 1000)
            await service._handle_quote_tick_event(quote())
            pump_quote_ms.append((monotonic() - begin) * 1000)

    async def memory():
        for _index in range(60):
            rss_samples.append(rss_kib())
            await asyncio.sleep(1)

    # Common external blocking-read load, not main's nonexistent token-proof worker.
    tasks = [asyncio.create_task(buys()), asyncio.create_task(pump()),
             asyncio.create_task(memory()), asyncio.create_task(asyncio.to_thread(sleep, 30))]
    try:
        for index in range(200):
            await asyncio.sleep(max(0, baseline + index * 0.3 - monotonic()))
            begin = monotonic()
            if index % 2 == 0:
                await service._handle_quote_tick_event(quote())
                quote_ms.append((monotonic() - begin) * 1000)
            else:
                result = await service.process_trade_intent(intent("sell", index))
                end = monotonic()
                assert result and result[0].payload.status == "accepted"
                close_ms.append((end - begin) * 1000)
                before_wire.append((wire_times[f"EXIT{index}"] - begin) * 1000)
                after_wire.append((end - wire_times[f"EXIT{index}"]) * 1000)
        await asyncio.gather(*tasks)
        elapsed = monotonic() - baseline
        allocations = []
        if tracing:
            allocations = [dict(site=str(row.traceback), bytes=row.size_diff, objects=row.count_diff)
                for row in tracemalloc.take_snapshot().compare_to(allocation_before, "lineno")[:15]]
        metrics = dict(source=os.environ["SOURCE_SHA"], source_file=__import__(
            "project_mai_tai.oms.service", fromlist=["__file__"]).__file__,
            harness="common unchanged-source OMS workload; NOT full cancel-token proof test",
            policy="freeze+gen2=100" if frozen else "normal", allocation_trace=tracing,
            events=len(pump_quote_ms), seconds=elapsed, rate=len(pump_quote_ms) / elapsed,
            buys=wires.count(("buy", "open")), exits=wires.count(("sell", "close")),
            loop_stall_ms=max(loop_ms), quote_ms=max(quote_ms), close_ms=max(close_ms),
            before_wire_ms=max(before_wire), after_wire_ms=max(after_wire),
            rss_before_kib=rss_before, rss_after_kib=rss_kib(), rss_peak_kib=max(rss_samples),
            frozen_objects=frozen_count, thresholds=gc.get_threshold(), gen2=gc_spans,
            allocations=allocations)
        print("[PG-GC-ASSESSMENT] " + json.dumps(metrics), flush=True)
        assert Counter(wires) == Counter({("buy", "open"): 25, ("sell", "close"): 100})
        assert len(pump_quote_ms) == 14_400
        if not tracing:
            assert metrics["rate"] >= 200
            assert max(loop_ms) < 50 and max(pump_quote_ms) < 50
            assert max(quote_ms) < 50 and max(close_ms) < 50
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        gc.callbacks.remove(on_gc)
        if tracing:
            tracemalloc.stop()
        if frozen:
            gc.unfreeze()
            gc.set_threshold(*old_threshold)


def main():
    url = os.environ["MAI_TAI_DATABASE_URL"]
    parsed = make_url(url)
    assert (parsed.get_backend_name() == "postgresql" and parsed.host in {"127.0.0.1", "localhost"}
        and parsed.database == "project_mai_tai_test" and not parsed.query)
    engine = create_engine(url)
    schema = "gc_assessment_" + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(isolated)
    try:
        asyncio.run(workload(sessionmaker(bind=isolated, expire_on_commit=False)))
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


if __name__ == "__main__":
    main()
