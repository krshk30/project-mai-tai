"""UNRUN exact D3 held-query gate; separate from the unchanged Sunday node.

Controlled LINE-OFF, constant-price/zero-volume callbacks (not bar-write cost),
real canonical books and original retirement loader. No provider connections.
"""

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import sys
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from tests.unit.test_confirmation_exit import _seed_confirmation_fill


@pytest.mark.timing
@pytest.mark.asyncio
async def test_prior_held_unknown_actual_live_callbacks_60s_200_each_under_50ms(monkeypatch, tmp_path, caplog):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    live_bar = ContextVar("groupd_d3_actual_live_bar", default=False)
    query_batch = ContextVar("groupd_d3_query_batch", default=None)
    counts = Counter()
    cardinalities = Counter()
    worker_threads = set()
    errors = []
    pending_workers = set()
    worker_lock = threading.Lock()
    clock = [datetime(2026, 10, 15, 14, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(strategy_module, "datetime", Clock)

    class ObservedResult:
        def __init__(self, actual, batch):
            self.actual, self.batch = actual, batch

        def all(self):
            rows = self.actual.all()
            if len(self.batch["loaded"]) == 1:
                self.batch["book_accounts"] = {name for _, name in rows}
            self.batch["loaded"].append(len(rows))
            return rows

        def __iter__(self):
            count = 0
            for row in self.actual:
                count += 1
                yield row
            self.batch["loaded"].append(count)

        def __getattr__(self, name):
            return getattr(self.actual, name)

    class ObservedSession(Session):
        def execute(self, *args, **kwargs):
            actual = super().execute(*args, **kwargs)
            batch = query_batch.get()
            return ObservedResult(actual, batch) if batch is not None else actual

        def scalars(self, *args, **kwargs):
            actual = super().scalars(*args, **kwargs)
            batch = query_batch.get()
            return ObservedResult(actual, batch) if batch is not None else actual

    engine = create_engine("sqlite:///" + str(tmp_path / "d3-held-rate.sqlite"),
                           connect_args={"check_same_thread": False})
    factory = sessionmaker(engine, class_=ObservedSession, expire_on_commit=False)
    watcher = None
    result = {"status": "NOT_COMPLETED", "setup": "LINE_OFF_real_books",
              "scope": "controlled zero-volume callbacks; bar persistence NOT EXERCISED; not box-size SQL proof"}
    cleanup_errors = []

    def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
        batch = query_batch.get()
        if batch is not None:
            assert threading.get_ident() != loop_thread, "D3 SQL ran on callback loop"
            assert statement.lstrip().upper().startswith("SELECT"), "unexpected D3 write"
            batch["selects"] += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        Base.metadata.create_all(engine)
        names = {"schwab": "live:schwab_1m_v2", "webull": "live:webull"}
        with factory.begin() as session:
            strategy = Strategy(id=uuid4(), code="schwab_1m_v2", name="controlled D3", execution_mode="live")
            session.add(strategy)
            accounts = {}
            for provider, name in names.items():
                account = BrokerAccount(id=uuid4(), name=name, provider=provider, environment="production")
                session.add(account)
                accounts[provider] = account.id
            strategy_id = strategy.id
        stamp = clock[0]-timedelta(seconds=30)
        for symbol in ("DHELD", "DUNKNOWN"):
            for provider, name in names.items():
                order_id, _ = _seed_confirmation_fill(factory, strategy_id=strategy_id,
                    account_id=accounts[provider], provider=provider, symbol=symbol,
                    slot_id="controlled-shared-"+symbol, filled_at=stamp)
                with factory.begin() as session:
                    order = session.get(BrokerOrder, order_id)
                    held = symbol == "DHELD"
                    session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=name,
                        symbol=symbol, entry_order_id=order.id, entry_client_order_id=order.client_order_id,
                        entry_price=Decimal(10), original_quantity=1, current_quantity=int(held),
                        status="open" if held else "closed"))
                    if symbol != "DUNKNOWN" or provider != "webull":
                        session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=accounts[provider],
                            symbol=symbol, quantity=Decimal(int(held)), average_price=Decimal(10 if held else 0)))

        settings = Settings(_env_file=None,
            strategy_schwab_1m_v2_line_chart_restoration_enabled=False,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=False,
            strategy_schwab_1m_v2_atr_flip_enabled=True,
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_confirmation_account_neutral_discovery_enabled=True,
            strategy_schwab_1m_v2_account_name=names["schwab"],
            strategy_schwab_1m_v2_webull_account_name=names["webull"])
        bot = bot_module.SchwabV2BotService(settings, session_factory=factory)
        for logger in {bot_module.logger, strategy_module.logger}:
            monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
        assert bot._line_restoration_enabled is False  # Constructor contract, never mutated.
        entries = bot._load_confirmation_entries()
        assert len(entries) == 2 and {entry.symbol for entry in entries} == {"DHELD", "DUNKNOWN"}
        assert all(entry.fanout_slot_id == "controlled-shared-"+entry.symbol for entry in entries)
        assert len({entry.fanout_slot_id for entry in entries}) == 2
        for entry in entries:
            assert bot._confirmation_exit.add(entry)
        original_ids = {(entry.order_id, entry.fill_id, entry.fanout_slot_id) for entry in entries}
        clock[0] += timedelta(days=1)
        prior_boundary = bot_module._current_scanner_session_start_utc()
        assert all(entry.filled_at < prior_boundary for entry in entries)
        bot._watchlist = {"HOT"}
        bot.strategy.watchlist_state("HOT")

        class RedisBoundary:
            async def xadd(self, _stream, _fields, **_kwargs):
                counts["redis_publications"] += 1
                assert counts["redis_publications"] <= 16, "unexpected repeated intent publication"
                return "controlled-rate-stream-id"

        redis = RedisBoundary()
        bot.intent_emitter = SchwabV2IntentEmitter(settings, redis, names["schwab"])
        bot.webull_intent_emitter = SchwabV2IntentEmitter(settings, redis, names["webull"])
        actual_loader = bot._closed_confirmation_entry_ids

        def observe_original_loader(pending):
            assert live_bar.get(), "target queries ran outside actual measured live-bar call"
            worker_threads.add(threading.get_ident())
            assert threading.get_ident() != loop_thread
            batch = {"selects": 0, "loaded": []}
            done = threading.Event()
            with worker_lock:
                pending_workers.add(done)
            token = query_batch.set(batch)
            try:
                flat = actual_loader(pending)
                assert not flat
                assert {(entry.order_id, entry.fill_id, entry.fanout_slot_id) for entry in pending} == original_ids
                assert batch["selects"] == 3
                # One closed source candidate; three own VP rows; two HELD managed rows.
                assert batch["loaded"] == [1, 3, 2]
                assert batch["book_accounts"] == set(names.values())
                cardinalities[(len(pending), *batch["loaded"])] += 1
                counts["evidence_batches"] += 1
                counts["d3_selects"] += batch["selects"]
                return flat
            except BaseException as exc:
                errors.append(type(exc).__name__+": "+str(exc))
                raise
            finally:
                query_batch.reset(token)
                done.set()
                with worker_lock:
                    pending_workers.discard(done)

        monkeypatch.setattr(bot, "_closed_confirmation_entry_ids", observe_original_loader)
        stalls, quote_ms, bar_ms = [], [], []
        ready = asyncio.Event()
        start = loop.time()
        stop = start+60.0

        async def heartbeat():
            deadline = loop.time()+0.002
            ready.set()
            while loop.time() < stop:
                await asyncio.sleep(max(0, deadline-loop.time()))
                stalls.append(max(0, loop.time()-deadline)*1000)
                deadline = loop.time()+0.002

        watcher = asyncio.create_task(heartbeat())
        await ready.wait()
        origin = clock[0]
        delivered = 0
        while loop.time() < stop:
            clock[0] = origin+timedelta(seconds=loop.time()-start)
            now_ms = int(clock[0].timestamp()*1000)
            bar_ms_stamp = now_ms//60_000*60_000-60_000
            quote = Quote("HOT", 1.99, 2.01, 2.0, now_ms, trade_time_ms=now_ms)
            # A legitimate no-trade bar still uses the real callback/strategy.
            # Original _persist_bar's zero-volume policy remains unchanged.
            bar = ChartBar("HOT", 2.0, 2.0, 2.0, 2.0, 0, bar_ms_stamp)
            before = loop.time()
            await bot._handle_quote("HOT", quote)
            quote_ms.append((loop.time()-before)*1000)
            before = loop.time()
            token = live_bar.set(True)
            try:
                await bot._handle_bar("HOT", bar, observation_phase="live")
            finally:
                live_bar.reset(token)
            bar_ms.append((loop.time()-before)*1000)
            delivered += 1
            await asyncio.sleep(max(0, start+delivered/225-loop.time()))
        duration = loop.time()-start
        await watcher
        result.update(duration_s=duration, each_events=delivered, aggregate_events=2*delivered,
            each_events_s=delivered/duration, max_stall_ms=max(stalls), max_quote_ms=max(quote_ms),
            max_bar_ms=max(bar_ms), counts=dict(counts),
            cardinalities=[{"pending_candidates_vp_held_rows": list(key), "batches": value}
                           for key, value in cardinalities.items()])
        assert duration >= 60.0 and delivered/duration >= 200
        assert max(stalls) < 50 and max(quote_ms) < 50 and max(bar_ms) < 50
        assert counts["evidence_batches"] == delivered and counts["d3_selects"] == 3*delivered
        assert worker_threads and loop_thread not in worker_threads
        assert not errors and not any(record.exc_info for record in caplog.records)
        retained = bot._confirmation_exit.prior_session_entries(prior_boundary)
        assert {(entry.order_id, entry.fill_id, entry.fanout_slot_id) for entry in retained} == original_ids
        assert bot._confirmation_exit.pending_count == 2
        with factory() as session:
            managed = list(session.scalars(select(OmsManagedPosition)))
            assert len(managed) == 4
            assert all(row.status == "open" and row.current_quantity == 1
                       for row in managed if row.symbol == "DHELD")
            assert all(row.status == "closed" and row.current_quantity == 0
                       for row in managed if row.symbol == "DUNKNOWN")
            books = list(session.scalars(select(VirtualPosition)))
            assert len(books) == 3
            assert sum(vp.quantity for vp in books if vp.symbol == "DHELD") == 2
            assert session.scalar(select(VirtualPosition).where(
                VirtualPosition.symbol == "DUNKNOWN", VirtualPosition.broker_account_id == accounts["webull"])) is None
        result["status"] = "ASSERTIONS_PASSED"
    finally:
        primary = sys.exc_info()[1]
        if watcher is not None:
            if not watcher.done():
                watcher.cancel()
            outcomes = await asyncio.gather(watcher, return_exceptions=True)
            cleanup_errors.extend(repr(item) for item in outcomes
                                  if isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError))
        # Cancellation of an awaiting callback does not stop its DB worker.
        # Drain only this workload's already-started workers before disposal.
        with worker_lock:
            remaining_workers = tuple(pending_workers)
        for done in remaining_workers:
            try:
                if not await asyncio.to_thread(done.wait, 5):
                    cleanup_errors.append("D3 evidence worker did not finish within cleanup bound")
            except BaseException as exc:
                cleanup_errors.append(repr(exc))
        for cleanup in (lambda: event.remove(engine, "before_cursor_execute", count_sql), engine.dispose):
            try:
                cleanup()
            except BaseException as exc:
                cleanup_errors.append(repr(exc))
        if primary is not None or errors or cleanup_errors:
            result["status"] = "FAILED"
        result.update(worker_errors=errors, cleanup_errors=cleanup_errors)
        print("GROUPD-D3-HELD-QUERY-RATE "+json.dumps(result, sort_keys=True))
        if (errors or cleanup_errors) and primary is None:
            raise AssertionError("D3 rate worker/cleanup failed: "+repr(errors+cleanup_errors))
