"""Controlled flag-ON bot callbacks, not historical trades or broker execution proof.

The original HOTFIX1 retained-on gate is separate and unchanged. Performance
uses real monotonic time; only market-decision clocks/quotes are controlled.
"""
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import threading
import time

import pytest
from sqlalchemy import event as sql_event

from project_mai_tai.atr_country_route import AtrCountryRouteBook, AtrCountryRouteStore
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_countryroute_atr import country_router, native_strategy
from tests.unit.test_pmprint1_pmflip1 import armed


class BoundedRedis:
    def __init__(self):
        self.count = 0
        self.first = None

    async def xadd(self, stream, fields, **kwargs):
        self.count += 1
        if self.first is None:
            self.first = json.loads(fields["data"])
        assert self.count <= 8, "unexpected repeated entry publication"
        return f"{self.count}-0"


def install_clock(monkeypatch):
    active = [datetime(2026, 10, 15, 14, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return active[0].astimezone(tz) if tz is not None else active[0].replace(tzinfo=None)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(strategy_module, "datetime", Clock)
    original = SchwabV2BotService._country_before_publish

    async def counted_publish(bot, emitter, draft):
        allowed = await original(bot, emitter, draft)
        if draft.intent_type == "open" and allowed:
            bot._active_rate_counts["country_publish_allowed"] += 1
        return allowed

    monkeypatch.setattr(SchwabV2BotService, "_country_before_publish", counted_publish)
    return active


def make_case(monkeypatch, session, is_us):
    base = datetime(2026, 10, 15, 11 if session == "PM" else 14, 30, tzinfo=UTC)
    if session == "PM":
        strategy, state, clock = armed(
            ("TEST", base.isoformat(), 2.0, 2.0, 2.0),
            strategy_schwab_1m_v2_country_route_enabled=True,
            strategy_schwab_1m_v2_broker_provider="schwab",
        )
        strategy.configure_country_routing(AtrCountryRouteBook(
            strategy.settings, country_router(is_us).route_for))
    else:
        strategy, state, clock = native_strategy(
            is_us, now=base, strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False)
        state.cw_armed, state.cw_bars_waited = True, 2
        state.cw_segment_high, state.cw_flip_level = 2.0, 1.95
    counts = Counter()
    broker = "schwab" if is_us else "webull"
    book = strategy._country_route_book
    original_select = book.select
    original_guard = strategy._country_single_entry
    original_quote = strategy.on_quote

    def counted_select(*args, **kwargs):
        selected = original_select(*args, **kwargs)
        if selected is not None:
            counts["country_select_returned"] += 1
        return selected

    def counted_guard(current):
        result = original_guard(current)
        if result and current.atr_country_route.get("atr_country_broker") == broker:
            counts["sealed_country_guard"] += 1
        return result

    def counted_quote(*args, **kwargs):
        counts["strategy_quote_calls"] += 1
        return original_quote(*args, **kwargs)

    monkeypatch.setattr(book, "select", counted_select)
    monkeypatch.setattr(strategy, "_country_single_entry", counted_guard)
    monkeypatch.setattr(strategy, "on_quote", counted_quote)
    factory = build_test_session_factory()
    bot = SchwabV2BotService(strategy.settings, session_factory=factory)
    bot.strategy = strategy
    bot._active_rate_counts = counts
    bot._country_route_store = AtrCountryRouteStore(factory, strategy.settings)
    bot.redis = BoundedRedis()
    bot.intent_emitter = SchwabV2IntentEmitter(strategy.settings, bot.redis, "live:schwab_1m_v2")
    bot.webull_intent_emitter = SchwabV2IntentEmitter(strategy.settings, bot.redis, "live:orb")
    bot._watchlist = {"TEST"}
    return {"label": f"{session}-{broker}", "session": session, "broker": broker,
            "base": base, "clock": clock, "bot": bot, "counts": counts,
            "engine": factory.kw["bind"]}


async def deliver(case, active, elapsed):
    active[0] = case["base"] + timedelta(seconds=elapsed)
    stamp = int(active[0].timestamp() * 1000)
    case["clock"][0] = stamp
    pm = case["session"] == "PM"
    quote = Quote("TEST", 1.99 if pm else 2.0, 2.0 if pm else 2.02,
                  2.0 if pm else 2.01, stamp)
    await case["bot"]._handle_quote("TEST", quote)
    case["counts"]["completed_callbacks"] += 1


def assert_active_case(case):
    bot, counts = case["bot"], case["counts"]
    assert bot.settings.strategy_schwab_1m_v2_country_route_enabled is True
    assert counts["completed_callbacks"] == counts["strategy_quote_calls"]
    assert counts["completed_callbacks"] > 0
    assert counts["country_select_returned"] > 0
    assert counts["country_publish_allowed"] == 1
    assert counts["sealed_country_guard"] >= counts["completed_callbacks"] - 1
    assert bot.redis.count == 1
    payload = bot.redis.first["payload"]
    expected_account = "live:schwab_1m_v2" if case["broker"] == "schwab" else "live:orb"
    assert payload["broker_account_name"] == expected_account
    assert payload["metadata"]["atr_country_broker"] == case["broker"]
    assert payload["metadata"]["atr_country_notional_usd"] == (
        "600" if case["broker"] == "schwab" else "300")
    assert len(bot._country_route_store.restore()) == 1
    assert bot.strategy.drain_pending_intents() == []
    assert bot.strategy.drain_webull_direct_intents() == []
    assert bot.strategy.drain_webull_fanout_intents() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session,is_us", [("PM", True), ("PM", False),
                                           ("RTH", True), ("RTH", False)])
async def test_selected_route_actual_bot_callback_counter_contract(monkeypatch, session, is_us):
    active = install_clock(monkeypatch)
    case = make_case(monkeypatch, session, is_us)
    try:
        for index in range(8):
            await deliver(case, active, index / 240)
        assert_active_case(case)
    finally:
        case["engine"].dispose()


@pytest.mark.asyncio
async def test_selected_route_actual_bot_callbacks_240_per_second_60_seconds(monkeypatch):
    active = install_clock(monkeypatch)
    cases = [make_case(monkeypatch, session, is_us)
             for session in ("PM", "RTH") for is_us in (True, False)]
    sql_counts = Counter()
    loop_thread = threading.get_ident()

    def query(_connection, _cursor, _statement, _parameters, _context, _many):
        sql_counts["statements"] += 1
        if threading.get_ident() == loop_thread:
            sql_counts["on_loop"] += 1

    stop = asyncio.Event()
    watch_counts = Counter()
    max_stall = 0.0

    async def watch():
        nonlocal max_stall
        deadline = time.monotonic()
        while not stop.is_set():
            deadline += 0.005
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            max_stall = max(max_stall, time.monotonic() - deadline)
            watch_counts["samples"] += 1

    for case in cases:
        sql_event.listen(case["engine"], "before_cursor_execute", query)
    count, max_callback, started = 0, 0.0, time.monotonic()
    watcher = asyncio.create_task(watch())
    try:
        while time.monotonic() - started < 60:
            before = time.monotonic()
            await deliver(cases[count % len(cases)], active, before - started)
            max_callback = max(max_callback, time.monotonic() - before)
            count += 1
            await asyncio.sleep(max(0, started + count / 240 - time.monotonic()))
        elapsed = time.monotonic() - started
    except BaseException:
        for case in cases:
            case["engine"].dispose()
        raise
    finally:
        stop.set()
        await watcher
        for case in cases:
            sql_event.remove(case["engine"], "before_cursor_execute", query)
    try:
        receipt = {
            "scope": "controlled PM/RTH selected-route real bot callbacks; SQLite journal; Redis fake; no broker",
            "seconds": elapsed, "events": count, "events_per_second": count / elapsed,
            "rate_scope": "aggregate240/s; four round-robin cases each approximately60/s",
            "max_loop_stall_ms": max_stall * 1000,
            "max_awaited_callback_ms": max_callback * 1000,
            "watchdog_samples": watch_counts["samples"], "sql": dict(sql_counts),
            "cases": {case["label"]: dict(case["counts"]) for case in cases},
        }
        print("ATR_COUNTRY_ACTIVE_CALLBACK_RATE=" + json.dumps(receipt, sort_keys=True))
        assert elapsed >= 60 and count / elapsed >= 200
        assert watch_counts["samples"] >= 1000
        assert max_stall < 0.05
        assert sql_counts["statements"] > 0
        assert sql_counts["on_loop"] == 0
        for case in cases:
            assert case["counts"]["completed_callbacks"] >= count // 4
            assert_active_case(case)
    finally:
        for case in cases:
            case["engine"].dispose()
