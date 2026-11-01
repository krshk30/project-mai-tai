"""UNRUN standalone enabled-ORB OMS owner rate; not the original edgeprot gate.

Run only at an explicitly granted quiet slot against an exact clean composed
head. Broker responses/timestamps are controlled; OMS callbacks, ownership,
native attribution, SQLite journal/receipt worker and STOP owner are real.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import subprocess
from time import monotonic
import traceback
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, TradeIntent, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.oms import bot_control, orb_country, orb_schwab_eod, service as oms_module
from project_mai_tai.oms.bot_control_journal import CleanupJournal
from project_mai_tai.oms.bot_control_orb import OrbStopContext
from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from project_mai_tai.positions_read_receipt import PositionsReadReceiptWriter, SNAPSHOT_TYPE
from tests.unit.test_killsw1_orb_owner import ControlSettings
from tests.unit.test_orbroute1_oms_chain import SCHWAB, WEBULL, seed_filled_webull, setup as setup


async def prime_actual_positions_receipts(service, writer, writer_task, accounts, clock):
    """Warm steady-state lifecycle only; never fabricate a receipt or terminal proof."""
    assert len(accounts) == len(set(accounts)) and accounts
    async with asyncio.timeout(5):
        await service.sync_broker_positions(account_names=list(accounts))
        await writer.queue.join()
    assert not writer_task.done(), "real receipt writer stopped during preflight"

    def inspect(session):
        for account in accounts:
            row = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["broker_account_name"].as_string() == account,
            ).order_by(DashboardSnapshot.created_at.desc()).limit(1))
            payload = row.payload if row is not None else None
            assert isinstance(payload, dict) and payload.get("writer") == "oms.sync_broker_positions"
            assert payload.get("broker_account_name") == account
            assert type(payload.get("position_count")) is int and payload["position_count"] == 1
            read_at = datetime.fromisoformat(payload["read_at"])
            assert read_at.tzinfo is not None and 0 <= (clock() - read_at).total_seconds() <= 30
    await service._run_db(inspect, commit=False)


@pytest.mark.timing
@pytest.mark.asyncio
async def test_enabled_orb_actual_oms_quote_and_owned_protection_aggregate_60s(setup, monkeypatch, tmp_path):
    expected = os.environ.get("ORB_OWNER_RATE_EXPECTED_HEAD", "")
    assert len(expected) == 40 and all(ch in "0123456789abcdef" for ch in expected)
    assert os.environ.get("ORB_OWNER_RATE_QUIET_ACK") == "main+I", "Actual acknowledgments required outside this test"
    module_file = Path(oms_module.__file__).resolve()
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
        cwd=module_file.parent, text=True).strip()).resolve()
    assert module_file == root / "src/project_mai_tai/oms/service.py"
    for module in (oms_module, orb_country, orb_schwab_eod, bot_control):
        assert Path(module.__file__).resolve().is_relative_to(root / "src")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert head == expected
    assert subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root).returncode == 0

    old_service, factory, old_broker = setup
    service = OmsRiskService(settings=ControlSettings(old_service.settings), redis_client=old_service.redis,
        session_factory=factory, broker_adapter=old_broker)
    control = service._bot_control_runtime
    assert control is not None
    await control.refresh()  # Capture EMPTY inventory before either controlled entry exists.
    assert control.boot_orb_entry_ids == frozenset()
    wb_entry_id, _ = seed_filled_webull((service, factory, old_broker), monkeypatch)
    with factory.begin() as session:
        entry = session.get(BrokerOrder, wb_entry_id)
        session.get(BrokerAccount, entry.broker_account_id).environment = "live"
        wb_target = orb_schwab_eod._Target(entry.id, entry.strategy_id, entry.broker_account_id, WEBULL,
            entry.symbol, entry.broker_order_id, entry.client_order_id, Decimal(10), Decimal(10), True)
    control.store.seed(wb_target.strategy_id)
    await control.refresh()
    await orb_country.reconcile_one(service, wb_entry_id)
    baseline = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    origin = [monotonic()]
    def now():
        return baseline + timedelta(seconds=monotonic() - origin[0])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = now()
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(oms_module, "utcnow", now)
    monkeypatch.setattr(bot_control, "datetime", Clock)
    monkeypatch.setattr(orb_country, "datetime", Clock)

    with factory.begin() as session:
        strategy = session.get(TradeIntent, session.get(BrokerOrder, wb_target.entry_id).intent_id)
        strategy.status = "filled"
    countries = CountryStore(factory)
    countries.put(CountryRecord("XYZ", "0000000002", "NY", "DE", True,
        baseline - timedelta(days=1), "READY", baseline.date()))
    router = CountryRouter(countries)
    router._records = countries.load()
    selection = select_orb_country_route(router, "XYZ", baseline.replace(hour=13, minute=28),
        schwab_account=SCHWAB, webull_account=WEBULL)
    event = build_orb_schwab_open_intent(old_service.settings, "XYZ", Decimal("5.5284"), selection=selection)
    with factory.begin() as session:
        strategy = session.get(oms_module.Strategy, wb_target.strategy_id)
        account = old_service.store.ensure_broker_account(session, SCHWAB, provider="schwab", environment="live")
        intent = old_service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.status = "filled"
        row = old_service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="orb-rate-schwab-entry", broker_order_id="schwab-native",
            symbol="XYZ", side="buy", quantity=Decimal(10), metadata=event.payload.metadata, status="filled")
        session.add(Fill(order_id=row.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="controlled-schwab-entry-fill", symbol="XYZ", side="buy", quantity=Decimal(10),
            price=Decimal("5.53"), filled_at=baseline, payload={"time_source": "controlled_fixture"}))
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id, symbol="XYZ",
            quantity=Decimal(10), average_price=Decimal("5.53"), opened_at=baseline))
        sc_target = orb_schwab_eod._Target(row.id, strategy.id, account.id, SCHWAB, "XYZ", row.broker_order_id,
            row.client_order_id, Decimal(10), Decimal(10), True)
        wb_entry = session.get(BrokerOrder, wb_target.entry_id)
        native_base = wb_entry.payload["orbroute1_protection"]["base"]
    targets = (wb_target, sc_target)
    counters, errors = Counter(), []
    class Facade:
        """Controlled broker boundary only: no adapter/REST acceptance claim."""
        def __init__(self):
            self.sent, self.released = [], False
            self.wire_at = {}

        async def list_account_positions(self, account):
            target = next(target for target in targets if target.account == account)
            counters[f"positions:{account}"] += 1
            self.wire_at[account] = monotonic()
            return [BrokerPositionSnapshot(account, target.symbol, Decimal(10), Decimal("5.53"), as_of=now())]

        def positions_wire_read_age_seconds(self, account):
            stamp = self.wire_at.get(account)
            return None if stamp is None else monotonic() - stamp

        async def fetch_order_update(self, request):
            target = next(target for target in targets if target.account == request.broker_account_name)
            counters[f"detail:{target.account}"] += 1
            if request.side == "buy":
                assert request.client_order_id == target.client_id
                return ExecutionReport("filled", request.client_order_id, broker_order_id=target.broker_id,
                    broker_fill_id=f"controlled-{target.account}-entry", symbol=target.symbol, side="buy",
                    intent_type="open", quantity=Decimal(10), filled_quantity=Decimal(10),
                    fill_price=Decimal("5.53"), reported_at=baseline, origin="broker")
            exact = {WebullBrokerAdapter._combo_leg_coid(native_base, suffix) for suffix in ("T", "S")}
            assert target.account == WEBULL and request.client_order_id in exact
            return ExecutionReport("cancelled" if self.released else "accepted", request.client_order_id,
                broker_order_id=f"native-{request.client_order_id}", symbol=target.symbol, side="sell",
                intent_type="close", quantity=Decimal(10), reported_at=now(), origin="broker",
                metadata=dict(request.metadata))

        async def fetch_oco_exit_fill(self, account, symbol, coid, *, entry_broker_order_id):
            assert (account, symbol, coid, entry_broker_order_id) == (
                SCHWAB, sc_target.symbol, sc_target.client_id, sc_target.broker_id)
            counters[f"native_poll:{account}"] += 1
            return None  # Held native tree has no executions in this controlled case.

        async def cancel_exit_pair(self, **binding):
            assert binding == {"broker_account_name": WEBULL, "symbol": wb_target.symbol,
                               "base_client_order_id": native_base}
            counters[f"release:{WEBULL}"] += 1
            self.released = True
            return []  # ACK never supplies the subsequent child's terminal proof.

        async def release_native_oco_for_close(self, account, root):
            assert (account, root) == (SCHWAB, sc_target.broker_id)
            counters[f"release:{SCHWAB}"] += 1
            return "released"  # Controlled exact-parent facade, not a real adapter proof.

        async def submit_order(self, request):
            assert request.reason == orb_schwab_eod.STOP_REASON and request.strategy_code == "orb_schwab"
            assert request.quantity == 10 and request.side == "sell" and request.intent_type == "close"
            self.sent.append(request)
            counters[f"stop_wire:{request.broker_account_name}"] += 1
            return [ExecutionReport("accepted", request.client_order_id, broker_order_id=f"soft-{len(self.sent)}",
                symbol=request.symbol, side="sell", intent_type="close", quantity=request.quantity,
                metadata=dict(request.metadata), reported_at=now(), origin="broker")]
    facade = Facade()
    from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
    service.broker_adapter = ControlledBrokerAdapter(facade, control)
    assert service.settings.orb_country_routing_enabled and service.settings.orb_live_schwab_orders_enabled
    assert service.settings.bot_control_enabled and service._bot_control_runtime is not None
    assert service._bot_control_runtime is control and control.boot_orb_entry_ids == frozenset()
    command = uuid4()
    control.store.request(strategy_id=wb_target.strategy_id, command_id=command, request_id=uuid4(),
        expected_generation=0, action="STOP", account_ids=tuple(t.account_id for t in targets),
        actor_reference="controlled-local-rate", reason="controlled fixture STOP", scope_complete=True)
    lease = control.store.claim_stop(command, "controlled-local-rate", now=now())
    assert lease is not None
    journal = CleanupJournal(control.store)
    await control.refresh()
    for target in targets:
        stop = ArmedHardStop("orb_schwab", target.account, target.symbol, Decimal(10), Decimal("5.53"),
            8.0, Decimal("5.09"), 2000, 0.0)
        service._armed_hard_stops[service._hard_stop_key("orb_schwab", target.account, target.symbol)] = stop
    original_resolve, original_probe = service._resolve_hard_stop_trigger_price, orb_country._probe
    def counted_resolve(stop):
        counters[f"actual_resolve:{stop.broker_account_name}"] += 1
        return original_resolve(stop)
    async def counted_probe(*args, **kwargs):
        counters[f"actual_native_probe:{args[2]}"] += 1
        return await original_probe(*args, **kwargs)
    monkeypatch.setattr(service, "_resolve_hard_stop_trigger_price", counted_resolve)
    monkeypatch.setattr(orb_country, "_probe", counted_probe)
    def denied(*_args, **_kwargs):
        raise AssertionError("network denied in standalone replay")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    writer = PositionsReadReceiptWriter(factory)
    service._positions_read_receipt_writer = writer
    receipt_task = asyncio.create_task(writer.run())
    try:
        await prime_actual_positions_receipts(service, writer, receipt_task,
            tuple(target.account for target in targets), now)
    except BaseException:
        receipt_task.cancel()
        await asyncio.gather(receipt_task, return_exceptions=True)
        raise
    stop = asyncio.Event()
    max_stall, callback_max = [0.0], [0.0]
    measurement_origin = monotonic()  # The wire clock's origin is never rewound.

    async def monitor():
        while not stop.is_set():
            expected_at = monotonic() + .005
            await asyncio.sleep(.005)
            max_stall[0] = max(max_stall[0], monotonic() - expected_at)

    async def owners():
        started_stop = False
        while not stop.is_set():
            try:
                await service._poll_orb_schwab_child_exits(entry_id=sc_target.entry_id)
                counters["actual_owned_poll_pass"] += 1
                await asyncio.to_thread(control.store.renew_stop, lease, now=now())
                if not started_stop and monotonic() - measurement_origin >= 20:
                    started_stop = True
                    for target in targets:
                        context = OrbStopContext(service, lease, journal, now)
                        assert await orb_schwab_eod._attempt(service, target, uuid4(), now,
                            reason=orb_schwab_eod.STOP_REASON, stop_context=context)
            except Exception:
                errors.append(traceback.format_exc())
                return
            await asyncio.sleep(1)
    tasks = [asyncio.create_task(monitor()), asyncio.create_task(owners())]
    count = 0
    try:
        while monotonic() - measurement_origin < 60:
            due = measurement_origin + count / 250
            if due > monotonic():
                await asyncio.sleep(due - monotonic())
            target = targets[count % len(targets)]
            event = QuoteTickEvent(source_service="orb-controlled-rate", produced_at=now(),
                payload=QuoteTickPayload(symbol=target.symbol, bid_price=Decimal("5.64"), ask_price=Decimal("5.65")))
            began = monotonic()
            await service._handle_quote_tick_event(event)
            callback_max[0] = max(callback_max[0], monotonic() - began)
            counters[f"actual_quote_callback:{target.account}"] += 1
            count += 1
        elapsed = monotonic() - measurement_origin
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        receipt_task.cancel()
        await asyncio.gather(receipt_task, return_exceptions=True)
    result = {"expected_head": expected, "seconds": elapsed, "aggregate_events": count,
        "aggregate_per_second": count / elapsed, "max_stall_ms": max_stall[0] * 1000,
        "max_callback_ms": callback_max[0] * 1000, "counters": dict(counters), "errors": errors,
        "profile": "ONE actual OMS; ORB+C6 enabled; controlled broker facade, clock, held-position fixtures; SQLite journal",
        "limits": "Not real provider/PG/entry throughput. PM/RTH STOP controls have separate focused XML. Not 200 each."}
    print("ORB_OWNER_RATE_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
    assert elapsed >= 60 and count / elapsed >= 200
    assert max_stall[0] < .05 and callback_max[0] < .05
    assert not errors
    assert counters["actual_owned_poll_pass"] >= 10
    for target in targets:
        assert counters[f"actual_quote_callback:{target.account}"] >= 6000
        assert counters[f"actual_resolve:{target.account}"] >= 1000
        assert counters[f"release:{target.account}"] == counters[f"stop_wire:{target.account}"] == 1
    assert counters[f"actual_native_probe:{WEBULL}"] >= 10
    assert counters[f"native_poll:{SCHWAB}"] >= 10
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == expected
    for module in (oms_module, orb_country, orb_schwab_eod, bot_control):
        assert Path(module.__file__).resolve().is_relative_to(root / "src")
    assert subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root).returncode == 0
