"""Additive controlled WB/O14 coverage; execute only with an explicit quiet grant.

Quotes use the real OMS callback. Fill producers, attach tasks, native attribution,
claimed sellers and committed pending orders are real. Only external broker and
Redis responses are controlled. This is not historical or real-SDK evidence.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
from time import monotonic
import traceback
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot, ExecutionReport, ExitPairReleaseResult, OrderRequest,
)
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db import models as models_module
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import (
    QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload,
)
from project_mai_tai.oms import (
    bot_control, bot_control_runtime, managed_sell_prewire, orb_country,
    orb_schwab_eod, service as oms, store as store_module,
    webull_attach_riskoff as wb,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata


WEBULL = "live:orb"
SCHWAB = "live:schwab_1m_v2"
CASES = {
    "WACC": ("schwab_1m_v2", WEBULL),
    "WRISK": ("schwab_1m_v2", WEBULL),
    "ORISK": ("orb_schwab", WEBULL),
    "SOWN": ("schwab_1m_v2", SCHWAB),
}
RETRY_NAMES = (
    "oms_webull_protect_attempts", "oms_webull_protect_interval_seconds",
    "oms_webull_protect_backoff_multiplier", "oms_webull_protect_max_interval_seconds",
)


class LoopStallMonitor:
    """Arm at the measurement origin, including its final outstanding timer."""

    def __init__(self, origin, *, clock=monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self.deadline = origin + .005
        self.ready = asyncio.Event()
        self.stopped = False
        self.max_stall = 0.0

    def observe(self, at):
        self.max_stall = max(self.max_stall, at - self.deadline)

    def finish(self, at):
        # No await: a producer blocking the last timer cannot escape observation.
        self.observe(at)
        self.stopped = True

    async def run(self):
        self.ready.set()
        while not self.stopped:
            await self.sleep(max(0.0, self.deadline - self.clock()))
            if self.stopped:
                break
            at = self.clock()
            self.observe(at)
            self.deadline = at + .005


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_timer_resume", [False, True])
async def test_monitor_covers_initial_producer_block_and_final_interval(finish_before_timer_resume):
    """Deterministic scheduling clock, NOT a wall-clock performance result."""
    clock = [0.0]
    timer = asyncio.Event()

    async def controlled_timer(_delay):
        await timer.wait()
        timer.clear()

    monitor = LoopStallMonitor(0.0, clock=lambda: clock[0], sleep=controlled_timer)
    task = asyncio.create_task(monitor.run())
    try:
        await monitor.ready.wait()
        assert monitor.deadline == .005
        # Model an uninterrupted75ms initial producer AFTER readiness; no yield.
        clock[0] = .075
        if not finish_before_timer_resume:
            timer.set()
            await asyncio.sleep(0)
            assert monitor.max_stall == pytest.approx(.070)
        monitor.finish(clock[0])
        assert monitor.max_stall == pytest.approx(.070)
        with pytest.raises(AssertionError):
            assert monitor.max_stall < .05
        timer.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class BoundedRedis:
    def __init__(self):
        self.count = 0

    async def xadd(self, *_args, **_kwargs):
        self.count += 1
        assert self.count <= 256, "unexpected repeated publication"
        return f"{self.count}-0"


class ControlledBroker:
    """Exact per-request external responses, never an OMS owner replacement."""

    def __init__(self, factory, clock, counts):
        self.factory, self.clock, self.counts = factory, clock, counts
        self.entries, self.pairs, self.exits = {}, {}, {}
        self.total = {symbol: Decimal(1000) for symbol in CASES}
        self.read_at = {}
        self.released_roots = set()

    async def fetch_quotes(self, symbols):
        await asyncio.sleep(.01)
        result = {}
        for symbol in symbols:
            # Separate broker and streamed quote surfaces are explicit here.
            # The broker quote crosses only AFTER its first rejected attach.
            crossed = symbol in {"WRISK", "ORISK"} and any(
                request.symbol == symbol for request in self.pairs.values())
            value = 12.0 if crossed else 10.0
            result[symbol] = {"bid_price": value, "ask_price": value, "last_price": value}
            self.counts[f"broker_quote:{symbol}"] += 1
        return result

    async def list_account_positions(self, account):
        await asyncio.sleep(.01)
        self.read_at[account] = monotonic()
        self.counts[f"positions_wire:{account}"] += 1
        return [BrokerPositionSnapshot(account, symbol, self.total[symbol], Decimal(10),
                    as_of=self.clock()) for symbol, (_, owner) in CASES.items() if owner == account]

    def positions_wire_read_age_seconds(self, account):
        stamp = self.read_at.get(account)
        return None if stamp is None else monotonic() - stamp

    def report(self, request, event, *, filled=Decimal(0)):
        return ExecutionReport(event, request.client_order_id,
            broker_order_id="broker-" + request.client_order_id,
            broker_fill_id="fill-" + request.client_order_id if filled else None,
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, filled_quantity=filled,
            fill_price=Decimal(10) if filled else None, origin="broker",
            reported_at=self.clock(), metadata=dict(request.metadata))

    def assert_pending(self, request):
        # A fresh independent session observes the committed fence at FIRST wire.
        with self.factory() as unit:
            order = unit.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == request.client_order_id))
            intent = unit.get(TradeIntent, order.intent_id) if order else None
            assert order is not None and intent is not None
            account = unit.get(BrokerAccount, order.broker_account_id)
            strategy = unit.get(Strategy, order.strategy_id)
            assert account.name == request.broker_account_name
            assert strategy.code == request.strategy_code
            assert order.symbol == intent.symbol == request.symbol
            assert order.side == intent.side == "sell"
            assert order.quantity == intent.quantity == request.quantity == 10
            assert order.status == "pending" and order.submitted_at is None
            assert order.broker_order_id is None
            assert intent.status == "pending"
            assert (order.payload or {}).get("entry_order_id") == str(self.entries[request.symbol][0])
            if strategy.code == "schwab_1m_v2":
                assert request.metadata.get("managed_sell_prepared_at")
                assert (order.payload or {}).get("managed_sell_prepared_at")
        self.counts[f"independent_pending:{request.symbol}"] += 1

    async def submit_order(self, request):
        assert (request.strategy_code, request.broker_account_name) == CASES[request.symbol]
        assert request.side == "sell" and request.intent_type == "close" and request.quantity == 10
        if request.metadata.get("webull_exit_only_pair") == "true":
            assert request.broker_account_name == WEBULL
            assert request.client_order_id not in self.pairs
            with self.factory() as unit:
                entry = unit.get(BrokerOrder, self.entries[request.symbol][0])
                if request.strategy_code == "schwab_1m_v2":
                    attempt = entry.payload[wb.KEY]["attempts"][-1]
                    assert attempt["base"] == request.client_order_id
                else:
                    attempt = entry.payload[orb_country.PROTECTION]
                    assert attempt["base"] == request.client_order_id
                assert unit.get(TradeIntent, UUID(attempt["intent_id"])) is not None
            self.pairs[request.client_order_id] = request
            self.counts[f"attach_wire:{request.symbol}"] += 1
            await asyncio.sleep(.01)
            if request.symbol != "WACC":
                return [ExecutionReport("rejected", request.client_order_id,
                    symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
                    reported_at=self.clock(), origin="broker",
                    metadata={"webull_http_status": "417", "webull_error_code": "CONTROLLED_REFUSAL"})]
            return [self.report(request, "accepted")]
        assert request.symbol not in self.exits, "no duplicate seller or second wire"
        assert request.reason == (wb.REASON if request.symbol != "SOWN"
                                  else "oms_v2_managed_exit:CW_HARD_STOP")
        self.assert_pending(request)
        if request.symbol != "ORISK":
            owners = self.service.__dict__.get("_control_managed_exit_symbol_owners", {})
            assert (request.broker_account_name, request.symbol) in owners
        else:
            assert (WEBULL, "ORISK", str(self.entries["ORISK"][0])) in self.service._orb_exit_claims
        self.exits[request.symbol] = request
        # Hold the actual first submit while the quote pump continues; no fake claim.
        await asyncio.sleep(.2)
        assert self.total[request.symbol] == 1010
        self.total[request.symbol] -= request.quantity
        self.counts[f"own_exit_wire:{request.symbol}"] += 1
        return [self.report(request, "filled", filled=request.quantity)]

    async def confirm_exit_pair_terminal(self, **binding):
        base = binding["base_client_order_id"]
        request = self.pairs[base]
        assert request.symbol == binding["symbol"] and request.broker_account_name == binding["broker_account_name"]
        assert request.symbol != "WACC", "accepted protection must stay untouched"
        await asyncio.sleep(.01)
        self.counts[f"exact_child_absence:{request.symbol}"] += 1
        return ExitPairReleaseResult(outcome="released", reports=tuple(
            ExecutionReport("cancelled", WebullBrokerAdapter._combo_leg_coid(base, suffix),
                symbol=request.symbol, side="sell", intent_type="cancel", quantity=Decimal(0),
                reported_at=self.clock(), origin="broker", metadata={
                    "eod_exit_pair_confirmation": "true", "cancel_outcome": "already_absent"})
            for suffix in ("T", "S")))

    async def cancel_exit_pair(self, **binding):
        assert self.pairs[binding["base_client_order_id"]].symbol == binding["symbol"]
        self.counts[f"cancel_pair:{binding['symbol']}"] += 1
        await asyncio.sleep(.01)
        return []  # ACK supplies no terminal authority.

    async def fetch_order_update(self, request):
        await asyncio.sleep(.01)
        if request.side == "buy":
            _, original = self.entries[request.symbol]
            assert request.client_order_id == original.client_order_id
            return self.report(original, "filled", filled=Decimal(10))
        if request.symbol in self.exits:
            original = self.exits[request.symbol]
            if original.client_order_id == request.client_order_id:
                return self.report(original, "filled", filled=Decimal(10))
        raise AssertionError("unplanned child read must not borrow another episode's proof")

    async def release_native_oco_for_close(self, account, root):
        assert account == SCHWAB and root == "broker-entry-SOWN"
        await asyncio.sleep(.01)
        self.released_roots.add(root)
        self.counts["exact_schwab_root_release"] += 1
        return "released"  # Controlled adapter boundary, not actual SDK proof.

    async def fetch_oco_exit_fill(self, account, symbol, coid, *, entry_broker_order_id=None):
        assert (account, symbol) == (SCHWAB, "SOWN")
        assert coid == self.entries[symbol][1].client_order_id
        assert entry_broker_order_id in (None, "broker-entry-SOWN")
        await asyncio.sleep(.01)
        self.counts["schwab_native_read"] += 1
        return None  # Controlled native tree has no execution, never a fake fill.


@pytest.mark.timing
@pytest.mark.asyncio
async def test_actual_oms_quote_attach_and_durable_exit_owner_aggregate_60s(tmp_path, monkeypatch):
    expected = os.environ.get("WB_O14_RATE_EXPECTED_HEAD", "")
    assert len(expected) == 40 and all(c in "0123456789abcdef" for c in expected)
    assert os.environ.get("WB_O14_RATE_QUIET_ACK") == "main+I+J"
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
        cwd=Path(oms.__file__).parent, text=True).strip()).resolve()

    def verify_source():
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == expected
        assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True) == ""
        assert Path(__file__).resolve() == root / "tests/perf/test_wbattach1_o14_active_oms_rate.py"
        for module in (oms, wb, orb_country, orb_schwab_eod, managed_sell_prewire):
            assert Path(module.__file__).resolve().is_relative_to(root / "src")

    verify_source()
    counts, errors, tasks = Counter(), [], []
    started = None
    count, stall, callback_max = 0, 0.0, 0.0
    engine = None
    service = None
    monitor = None
    elapsed = None
    frozen_counts = None
    phase = "setup"
    origin = monotonic()
    baseline = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)

    def now():
        return baseline + timedelta(seconds=monotonic() - origin)

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            value = now()
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def denied(*_args, **_kwargs):
        raise AssertionError("network denied; controlled external boundary only")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    for module in (oms, wb, orb_country, orb_schwab_eod, store_module, models_module,
                   bot_control, bot_control_runtime):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(oms, "utcnow", now)
    monkeypatch.setattr(store_module, "utcnow", now)

    def observe(module, name, key):
        original = getattr(module, name)

        async def counted(*args, **kwargs):
            counts[key + ":entered"] += 1
            result = await original(*args, **kwargs)
            counts[key + ":returned"] += 1
            return result

        monkeypatch.setattr(module, name, counted)

    try:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'wb-o14-rate.sqlite'}",
            connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        settings = Settings(_env_file=None, bot_control_enabled=True,
            strategy_schwab_1m_v2_account_name=SCHWAB,
            strategy_schwab_1m_v2_broker_provider="schwab",
            strategy_schwab_1m_v2_webull_account_name=WEBULL,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            orb_webull_account_name=WEBULL, orb_country_routing_enabled=True,
            orb_live_schwab_orders_enabled=True,
            oms_v2_exit_management_enabled=True, oms_v2_exit_close_on_fill_enabled=True,
            oms_v2_exit_release_reservation_enabled=True)
        assert all(not hasattr(settings, name) for name in RETRY_NAMES)
        broker = ControlledBroker(factory, now, counts)
        service = oms.OmsRiskService(settings=settings, session_factory=factory,
            redis_client=BoundedRedis(), broker_adapter=broker)
        broker.service = service
        with factory.begin() as unit:
            for code in ("schwab_1m_v2", "orb_schwab"):
                service.store.ensure_strategy(unit, code, execution_mode="live")
            for account, provider in ((WEBULL, "webull"), (SCHWAB, "schwab")):
                service.store.ensure_broker_account(unit, account, provider=provider, environment="production")
        with factory() as unit:
            strategy_ids = list(unit.scalars(select(Strategy.id)))
        for strategy_id in strategy_ids:
            service._bot_control_runtime.store.seed(strategy_id)
        await service._bot_control_runtime.refresh()
        assert service._bot_control_runtime.boot_managed_ids == frozenset()
        assert service._bot_control_runtime.boot_orb_entry_ids == frozenset()

        events = {}
        with factory.begin() as unit:
            for symbol, (code, account) in CASES.items():
                strategy = unit.scalar(select(Strategy).where(Strategy.code == code))
                acct = unit.scalar(select(BrokerAccount).where(BrokerAccount.name == account))
                metadata = {"fanout_leg": "webull"} if account == WEBULL else {"native_oco_bracket": "true"}
                if code == "orb_schwab":
                    geometry = build_orb_schwab_bracket_metadata(Decimal(10))
                    metadata = {"orb_country_route": "true", "orb_country_broker": "webull",
                        "orb_country_size": "10", "orb_intended_break_level": "10",
                        "stop_price": geometry["stop_price"], "orb_target_price": geometry["bracket_target_price"],
                        "orb_protect_price": geometry["bracket_stop_price"]}
                event = TradeIntentEvent(source_service="CONTROLLED-WB-O14-RATE",
                    payload=TradeIntentPayload(strategy_code=code, broker_account_name=account,
                        symbol=symbol, side="buy", intent_type="open", quantity=Decimal(10),
                        reason="controlled unfilled entry fixture", metadata=metadata))
                intent = service.store.create_trade_intent(unit, strategy=strategy, broker_account=acct, event=event)
                intent.created_at = intent.updated_at = now()
                entry = service.store.get_or_create_order(unit, intent=intent, strategy_id=strategy.id,
                    broker_account_id=acct.id, client_order_id="entry-" + symbol,
                    broker_order_id="broker-entry-" + symbol, symbol=symbol, side="buy",
                    quantity=Decimal(10), metadata=metadata, status="accepted")
                entry.submitted_at = entry.updated_at = now()
                unit.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=acct.id,
                    symbol=symbol, quantity=Decimal(0), average_price=Decimal(10)))
                unit.add(AccountPosition(broker_account_id=acct.id, symbol=symbol,
                    quantity=Decimal(1000), average_price=Decimal(10), source_updated_at=now()))
                broker.entries[symbol] = (entry.id, OrderRequest(entry.client_order_id, account,
                    code, symbol, "buy", "open", Decimal(10), event.payload.reason, metadata))
                events[symbol] = event

        for module, name, key in (
            (wb, "attach_atr", "actual_atr_attach"),
            (wb, "strict_atr_handoff", "actual_atr_claimed_handoff"),
            (wb, "validate_atr_before_wire", "actual_atr_final_validator"),
            (managed_sell_prewire, "prepare_managed_sell", "actual_o14_prepare"),
            (orb_country, "reconcile_one", "actual_orb_reconcile"),
            (wb.OrbAttachExit, "validate_wire", "actual_orb_attach_context_wire"),
        ):
            observe(module, name, key)
        original_apply = service._apply_managed_position_after_fill

        def applied(**kwargs):
            result = original_apply(**kwargs)
            counts[f"actual_fill_producer:{kwargs['symbol']}:{kwargs['side']}"] += 1
            return result

        monkeypatch.setattr(service, "_apply_managed_position_after_fill", applied)
        original_spawn = service._spawn_webull_protection

        def spawned(**kwargs):
            task = original_spawn(**kwargs)
            assert task is not None
            tasks.append(task)
            counts[f"actual_attach_spawn:{kwargs['symbol']}"] += 1
            return task

        monkeypatch.setattr(service, "_spawn_webull_protection", spawned)
        original_emit = service._emit_v2_exit_on_loop

        async def emitted(*args, **kwargs):
            counts[f"actual_existing_emit:{args[1]}"] += 1
            result = await original_emit(*args, **kwargs)
            counts[f"actual_existing_emit_returned:{args[1]}"] += 1
            return result

        monkeypatch.setattr(service, "_emit_v2_exit_on_loop", emitted)
        setup_counts = dict(counts)
        assert not broker.pairs and not broker.exits
        assert not service._managed_v2_symbols

        async def lifecycle():
            for symbol, event in events.items():
                entry_id, request = broker.entries[symbol]
                broker.total[symbol] += 10
                with factory() as unit:
                    entry = unit.get(BrokerOrder, entry_id)
                    published = await service._record_order_reports(session=unit,
                        intent=unit.get(TradeIntent, entry.intent_id), strategy_id=entry.strategy_id,
                        broker_account_id=entry.broker_account_id, intent_event=event, request=request,
                        reports=[broker.report(request, "filled", filled=Decimal(10))])
                    unit.commit()
                counts[f"actual_entry_report_committed:{symbol}"] += 1
                for event_out in published:
                    await service._publish_order_event(event_out)
            # Actual recurring production reconciliation, not direct attach context.
            schwab_attempted = False
            while monotonic() - started < 60:
                await orb_country.reconcile_protection(service)
                if monotonic() - started >= 10 and not schwab_attempted:
                    schwab_attempted = True
                    snapshot = await service._run_db(lambda unit: service._read_v2_managed_snapshot(
                        unit, SCHWAB, "SOWN", True), commit=False)
                    assert snapshot is not None
                    await service._emit_v2_exit_on_loop(SCHWAB, "SOWN",
                        service._hydrate_v2_position(snapshot), snapshot.entry_price,
                        kind="CLOSE", reference_price=9, reason="oms_v2_managed_exit:CW_HARD_STOP",
                        bid=9, close_on_fill=True, expected_managed_row_id=snapshot.managed_row_id)
                await asyncio.sleep(1)

        phase = "measurement"
        started = monotonic()
        monitor = LoopStallMonitor(started)
        monitor_task = asyncio.create_task(monitor.run())
        tasks.append(monitor_task)
        await monitor.ready.wait()
        # The origin/deadline and readiness exist BEFORE any measured fill task.
        owner_task = asyncio.create_task(lifecycle())
        tasks.append(owner_task)
        while monotonic() - started < 60:
            due = started + count / 240
            if due > monotonic():
                await asyncio.sleep(due - monotonic())
            symbol = tuple(CASES)[count % len(CASES)]
            event = QuoteTickEvent(source_service="CONTROLLED-WB-O14-STREAM",
                produced_at=now(), payload=QuoteTickPayload(symbol=symbol,
                    bid_price=Decimal(10), ask_price=Decimal("10.01")))
            before = monotonic()
            await service._handle_quote_tick_event(event)
            callback_max = max(callback_max, monotonic() - before)
            counts[f"actual_quote_callback:{symbol}"] += 1
            count += 1
            if owner_task.done():
                owner_task.result()  # Fail on a real owner error, not throughput alone.
        ended = monotonic()
        elapsed = ended - started
        monitor.finish(ended)
        stall = monitor.max_stall
        frozen_counts = dict(counts)
        phase = "assertions"
        assert elapsed >= 60 and count / elapsed >= 200
        assert stall < .05 and callback_max < .05
        assert counts["actual_atr_attach:returned"] >= 2
        assert counts["actual_atr_claimed_handoff:returned"] >= 1
        assert counts["actual_atr_final_validator:returned"] == 1
        assert counts["actual_orb_attach_context_wire:returned"] == 1
        assert counts["actual_o14_prepare:returned"] >= 2
        for symbol in CASES:
            assert counts[f"actual_quote_callback:{symbol}"] >= 2900
            assert counts[f"actual_entry_report_committed:{symbol}"] == 1
        for symbol in ("WACC", "WRISK", "SOWN"):
            assert counts[f"actual_fill_producer:{symbol}:buy"] == 1
        for symbol in ("WRISK", "ORISK", "SOWN"):
            assert counts[f"own_exit_wire:{symbol}"] == 1
            assert counts[f"independent_pending:{symbol}"] == 1
            assert counts[f"actual_fill_producer:{symbol}:sell"] == 1
            assert broker.total[symbol] == 1000
        assert counts["actual_existing_emit_returned:WRISK"] == 1
        assert counts["actual_existing_emit_returned:SOWN"] == 1
        assert broker.total["WACC"] == 1010 and "WACC" not in broker.exits
        assert counts["exact_child_absence:WRISK"] >= 1
        assert counts["exact_child_absence:ORISK"] >= 1
        assert counts["exact_schwab_root_release"] == 1
        assert counts["actual_attach_spawn:WACC"] == counts["actual_attach_spawn:WRISK"] == 1
        with factory() as unit:
            for symbol, (code, account) in CASES.items():
                position = unit.scalar(select(VirtualPosition).join(Strategy).join(
                    BrokerAccount, BrokerAccount.id == VirtualPosition.broker_account_id).where(
                        Strategy.code == code, BrokerAccount.name == account, VirtualPosition.symbol == symbol))
                assert position.quantity == (10 if symbol == "WACC" else 0)
                bought = list(unit.scalars(select(Fill).where(
                    Fill.order_id == broker.entries[symbol][0], Fill.side == "buy")))
                assert len(bought) == 1 and bought[0].quantity == 10 and bought[0].price == 10
            assert len(list(unit.scalars(select(Fill).where(Fill.side == "sell")))) == 3
            assert len(list(unit.scalars(select(OmsManagedPosition)))) == 3
        verify_source()
        phase = "passed"
    except BaseException:
        errors.append(traceback.format_exc())
        raise
    finally:
        ended = monotonic()
        if monitor is not None and not monitor.stopped:
            monitor.finish(ended)
            stall = monitor.max_stall
        measured_seconds = elapsed if elapsed is not None else (ended - started if started is not None else 0.0)
        measured_counts = frozen_counts if frozen_counts is not None else dict(counts)
        # No post-window completion is credited to measured counters.
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                errors.extend(repr(result) for result in results
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError))
        except BaseException:
            errors.append("task cleanup: " + traceback.format_exc())
        try:
            if service is not None:
                await service._shutdown_symbol_tick_work()
        except BaseException:
            errors.append("symbol cleanup: " + traceback.format_exc())
        try:
            if engine is not None:
                engine.dispose()
        except BaseException:
            errors.append("engine cleanup: " + traceback.format_exc())
        sources = {}
        try:
            sources = {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                for module in (oms, wb, orb_country, orb_schwab_eod, managed_sell_prewire)}
            sources["test_file"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except BaseException:
            errors.append("source hashing: " + traceback.format_exc())
        if errors and phase == "passed":
            phase = "cleanup_failed"
        print("WB_O14_OWNER_RATE_RESULT=" + json.dumps({
            "expected_head": expected, "phase": phase, "seconds": measured_seconds,
            "aggregate_callbacks": count,
            "aggregate_per_second": count / measured_seconds if measured_seconds else 0.0,
            "max_stall_ms": stall * 1000, "max_callback_ms": callback_max * 1000,
            "measured_counts": measured_counts, "setup_counts": locals().get("setup_counts", {}),
            "errors": errors, "source_sha256": sources,
            "profile": "actual OMS callbacks/fill producers/claimed sellers; controlled RTH broker and Redis; SQLite",
            "limits": "240 aggregate not per case; separate broker and stream quotes; no PM/AH, SDK, PG, historical or PnL proof",
        }, sort_keys=True), flush=True)
    assert not errors
