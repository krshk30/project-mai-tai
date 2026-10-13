"""Native strategy/DB controls. Broker acceptance and fills here are counterfactual."""

import ast
import json
import os
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base, BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent,
)
from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

ET = ZoneInfo("America/New_York")
ORIGINAL = json.loads((Path(__file__).parents[1] / "fixtures/eh2rth1_tcrt_original.json").read_text())


def recorded_bar(row):
    return OHLCVBar(int(datetime.fromisoformat(row["bar_time"]).timestamp() * 1000),
        row["open"], row["high"], row["low"], row["close"], row["volume"])


def ms(hour, minute, second=0):
    return int(datetime(2026, 10, 13, hour, minute, second, tzinfo=ET).timestamp() * 1000)


def setup(*, mirror=False, **extra):
    settings = Settings(_env_file=None,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=mirror,
        strategy_schwab_1m_v2_entry_notional_usd=0,
        strategy_schwab_1m_v2_webull_entry_notional_usd=0,
        strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct=0.5,
        strategy_schwab_1m_v2_resting_buy_round_up_enabled=True,
        **extra)
    strategy = SchwabV2Strategy(settings)
    clock = [ms(9, 19)]
    strategy._now_ms = lambda: clock[0]
    eh, window = strategy._resting_session_is_eh, strategy._resting_in_window
    strategy._resting_session_is_eh = lambda now=None: eh(datetime.fromtimestamp(clock[0] / 1000, UTC))
    strategy._resting_in_window = lambda now=None: window(datetime.fromtimestamp(clock[0] / 1000, UTC))
    state = strategy.watchlist_state("TCRT")
    state.atr_state, state.atr_state_age, state.atr_trail = "short", 10, 1.9048
    state.atr_short_flip_bar_ts = ms(9, 19)
    state.bars.append(OHLCVBar(ms(9, 18), 1.89, 1.90, 1.88, 1.89, 25_000))
    strategy._cw_v2_resting_track(state, None)
    assert state.resting_active and not state.resting_is_broker_order
    assert strategy.drain_pending_intents() == []
    clock[0] = ms(9, 30)
    state.bars.append(OHLCVBar(ms(9, 29), 1.89, 1.90, 1.88, 1.89, 25_000))
    return strategy, state, clock


def quote(state, clock, price=1.90):
    return Quote(state.symbol, price - 0.001, price + 0.001, price, clock[0], 0)


def prove(strategy, state, clock, **updates):
    proof = {"broker_order_id": "CONTROLLED-ACCEPTED-TCRT", "status": "working",
        "segment_id": state.fanout_segment_id, "slot": "resting",
        "slot_id": fanout_slot_id(strategy_code="schwab_1m_v2", symbol=state.symbol,
                                  segment_id=state.fanout_segment_id, slot="resting"),
        "entry_slot": state.last_resting_placed_slot, "submit_response_at_ms": 0, **updates}
    strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders={
        (state.symbol, state.resting_schwab_generation): proof,
    })


def test_tcrt_native_boundary_places_even_with_unchanged_line_then_proven_pair_cross(record_property):
    strategy, state, clock = setup()
    row = ORIGINAL["boundary_quote"]
    clock[0] = int(datetime.fromisoformat(row["received_at"]).timestamp() * 1000)
    state.bars.clear()
    state.bars.append(recorded_bar(ORIGINAL["bars"][0]))
    assert state.bars[-1].timestamp_ms + 60_000 <= clock[0]
    assert datetime.fromisoformat(ORIGINAL["bars"][0]["created_at"]).timestamp() * 1000 <= clock[0]
    strategy.on_quote(state.symbol, Quote(state.symbol, row["bid"], row["ask"], row["last"],
        int(datetime.fromisoformat(row["event_ts"]).timestamp() * 1000), 0))
    orders = strategy.drain_pending_intents()
    assert len(orders) == 1 and orders[0].metadata["order_type"] == "STOP_LIMIT"
    assert orders[0].metadata["cw_flip_level"] == "1.9048"
    assert float(orders[0].metadata["stop_price"]) == pytest.approx(1.92, abs=0.0001)
    assert state.resting_is_broker_order
    assert strategy.drain_webull_fanout_intents() == []
    clock[0] = int(datetime.fromisoformat(ORIGINAL["recorded_log_cross"]["time"]).timestamp() * 1000)
    state.bars.append(recorded_bar(ORIGINAL["bars"][1]))
    assert state.bars[-1].timestamp_ms == ms(9, 30)
    assert state.bars[-1].timestamp_ms + 60_000 <= clock[0]
    assert datetime.fromisoformat(ORIGINAL["bars"][1]["created_at"]).timestamp() * 1000 <= clock[0]
    prove(strategy, state, clock)
    strategy.on_quote(state.symbol, quote(state, clock, 1.925))
    legs = strategy.drain_webull_fanout_intents()
    assert len(legs) == 1 and legs[0].metadata["fanout_source"] == "rth_resting"
    assert legs[0].metadata["fanout_segment_id"] == orders[0].metadata["fanout_segment_id"]
    # Injected accepted/fill evidence proves lifecycle handling, NOT executable broker fills.
    metadata = legs[0].metadata
    strategy.apply_fanout_outcome(FanoutOutcome(
        record_id=uuid4(), created_at=datetime.fromtimestamp(clock[0] / 1000, UTC),
        symbol=state.symbol, segment_id=int(metadata["fanout_segment_id"]),
        slot=metadata["fanout_slot"], slot_id=metadata["fanout_slot_id"],
        attempt_id=metadata.get("fanout_attempt_id", ""), outcome="filled",
        evidence_id="CONTROLLED-WEBULL-FILL-093135", event_source="broker",
    ))
    strategy.update_position(state.symbol, 1, held_qty=1)
    assert state.cw_resting_taken and state.fanout_webull_resting_taken
    assert state.position_qty_held == 1 and not state.resting_active
    record_property("counterfactual", "Recorded completed09:29/09:30 bars, received-at boundary quote projected into REST callback; CONTROLLED ACCEPTED primary09:30:06 + assumed-fresh log-price cross09:31:35 and injected both-fill evidence; no actual REST snapshot/broker calls/executability claim")


def test_mirror_handover_queues_two_resting_legs_no_market_duplicate():
    strategy, state, clock = setup(mirror=True)
    strategy.on_quote(state.symbol, quote(state, clock))
    primary, mirror = strategy.drain_pending_intents(), strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == 1
    assert primary[0].metadata["order_type"] == mirror[0].metadata["order_type"] == "STOP_LIMIT"
    assert primary[0].metadata["rpg_resting_generation"] == mirror[0].metadata["rpg_resting_generation"]
    strategy.on_quote(state.symbol, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("kind", ["absent", "stale", "future", "wrong_generation", "empty_id", "rejected", "unknown", "soft"])
def test_rth_market_cross_requires_fresh_exact_primary_broker_proof(kind):
    strategy, state, clock = setup()
    strategy.on_quote(state.symbol, quote(state, clock))
    strategy.drain_pending_intents()
    prove(strategy, state, clock)
    if kind == "absent":
        strategy.apply_primary_resting_book(observed_at_ms=clock[0], orders={})
    elif kind == "stale":
        clock[0] += strategy_module.PRIMARY_RESTING_BOOK_MAX_AGE_MS + 1
    elif kind == "future":
        clock[0] -= 1
    elif kind == "wrong_generation":
        state.resting_schwab_generation = "newer"
    elif kind == "soft":
        state.resting_is_broker_order = False
    else:
        prove(strategy, state, clock, **({"broker_order_id": ""} if kind == "empty_id" else {"status": kind}))
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("gate", ["boot", "thin", "stale_bar", "stale_quote", "above_stop", "consumed", "unsettled_short", "inflight", "flip_wait"])
def test_handover_never_bypasses_existing_first_placement_gates(gate):
    strategy, state, clock = setup()
    q = quote(state, clock)
    if gate == "boot":
        strategy._entries_held = True
    elif gate == "thin":
        strategy._liquidity_floor_ok = lambda state: False
    elif gate == "stale_bar":
        clock[0] += strategy._resting_max_bar_age_ms + 60_001
        q = quote(state, clock)
    elif gate == "stale_quote":
        q = Quote(state.symbol, 1.899, 1.901, 1.90, clock[0] - 60_000, 0)
    elif gate == "above_stop":
        q = quote(state, clock, 1.925)
    elif gate == "consumed":
        state.cw_resting_taken = True
    elif gate == "unsettled_short":
        state.atr_state_age = 0
    elif gate == "inflight":
        state.position_qty = 1
    else:
        state.resting_flip_ms = clock[0]
    strategy.on_quote(state.symbol, q)
    assert not [d for d in strategy.drain_pending_intents() if d.intent_type == "open"]
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("pm_reprice", [False, True])
def test_boundary_bar_handover_independent_of_pm_reprice_flag(pm_reprice):
    strategy, state, clock = setup(strategy_schwab_1m_v2_pm_rest_reprice_enabled=pm_reprice)
    state.last_quote = quote(state, clock)
    strategy._cw_v2_resting_track(state, None)
    assert len(strategy.drain_pending_intents()) == 1
    strategy._cw_v2_resting_track(state, None)
    assert strategy.drain_pending_intents() == []


NATIVE_AT = datetime(2026, 10, 13, 13, 30, tzinfo=UTC)
NATIVE_METADATA = {"source": "schwab_1m_v2", "resting_entry": "true",
    "rpg_resting_generation": "GEN", "fanout_segment_id": str(ms(9, 19)),
    "fanout_slot": "resting", "cw_entry_slot": "first",
    "fanout_slot_id": fanout_slot_id(strategy_code="schwab_1m_v2", symbol="TCRT",
                                     segment_id=ms(9, 19), slot="resting")}


def native_expected(*, status="working", broker_id="CONTROLLED-ID", submit_at=0):
    return {"broker_order_id": broker_id, "status": status,
        "segment_id": ms(9, 19), "slot": "resting", "entry_slot": "first",
        "slot_id": NATIVE_METADATA["fanout_slot_id"], "submit_response_at_ms": submit_at}


@pytest.fixture
def native_book(monkeypatch):
    observed = [NATIVE_AT]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed[0] if tz else observed[0].replace(tzinfo=None)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", Clock)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_account_name="live:schwab_1m_v2")
    with factory() as session:
        account = BrokerAccount(name=settings.strategy_schwab_1m_v2_account_name, provider="schwab", environment="paper")
        strategy = Strategy(code="schwab_1m_v2", name="controlled")
        session.add_all([account, strategy])
        session.flush()
        metadata = dict(NATIVE_METADATA)
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="TCRT",
            side="buy", intent_type="open", quantity=Decimal(1), reason="controlled", payload={"metadata": metadata})
        session.add(intent)
        session.flush()
        order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id, intent_id=intent.id,
            client_order_id=str(uuid4()), broker_order_id="CONTROLLED-ID", symbol="TCRT", side="buy",
            order_type="STOP_LIMIT", time_in_force="DAY", quantity=Decimal(1), status="working", payload=metadata)
        session.add(order)
        session.flush()
        event = BrokerOrderEvent(order_id=order.id, event_type="working", event_source="broker",
            event_at=NATIVE_AT, payload={"client_order_id": order.client_order_id,
                "broker_order_id": order.broker_order_id, "metadata": metadata})
        session.add(event)
        session.commit()
        ids = (order.id, event.id)
    bot = SimpleNamespace(session_factory=factory, settings=settings, observed=observed)
    yield bot, factory, ids
    engine.dispose()


@pytest.mark.parametrize("wire_type", ["STOP_LIMIT", "stop_limit"])
def test_native_db_primary_working_receipt(native_book, wire_type):
    bot, factory, (order_id, _) = native_book
    with factory() as session:
        session.get(BrokerOrder, order_id).order_type = wire_type
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {
        ("TCRT", "GEN"): native_expected()}


@pytest.mark.parametrize("bad", ["missing_id", "client_event", "unknown_event", "rejected", "wrong_account", "wrong_type", "missing_generation", "zero_quantity", "not_open", "newer_unknown"])
def test_native_db_no_local_bool_or_wrong_broker_receipt(native_book, bad):
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        event = session.get(BrokerOrderEvent, event_id)
        if bad == "missing_id":
            order.broker_order_id = None
        elif bad in {"client_event", "unknown_event"}:
            event.event_source = "client" if bad == "client_event" else "unknown"
        elif bad == "rejected":
            order.status = "rejected"
        elif bad == "wrong_account":
            bot.settings = Settings(_env_file=None, strategy_schwab_1m_v2_account_name="other")
        elif bad == "wrong_type":
            order.order_type = "MARKET"
        elif bad == "zero_quantity":
            order.quantity = 0
        elif bad == "not_open":
            session.get(TradeIntent, order.intent_id).intent_type = "cancel"
        elif bad == "newer_unknown":
            session.add(BrokerOrderEvent(order_id=order_id, event_type="could_not_tell",
                event_source="unknown", event_at=datetime(2099, 1, 1, tzinfo=UTC)))
        else:
            order.payload = {}
            session.get(TradeIntent, order.intent_id).payload = {}
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}


def test_native_db_failure_is_unknown():
    bot = SimpleNamespace(session_factory=lambda: (_ for _ in ()).throw(RuntimeError("controlled DB outage")))
    assert SchwabV2BotService._fetch_primary_resting_book(bot) is None


def test_native_db_duplicate_generation_is_ambiguous(native_book):
    bot, factory, (order_id, _) = native_book
    with factory() as session:
        old = session.get(BrokerOrder, order_id)
        other = BrokerOrder(strategy_id=old.strategy_id, broker_account_id=old.broker_account_id,
            intent_id=old.intent_id, client_order_id=str(uuid4()), broker_order_id="CONTROLLED-ID-2",
            symbol=old.symbol, side=old.side, order_type=old.order_type,
            time_in_force=old.time_in_force, quantity=old.quantity, status=old.status, payload=old.payload)
        session.add(other)
        session.flush()
        session.add(BrokerOrderEvent(order_id=other.id, event_type="working", event_source="broker",
                                    event_at=NATIVE_AT))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {("TCRT", "GEN"): None}


def test_reclaim_handover_uses_own_slot_and_segment_level():
    strategy, state, clock = setup()
    state.resting_slot = "reclaim"
    state.cw_armed, state.cw_bars_waited, state.cw_segment_high = True, 3, 2.0
    strategy.on_quote(state.symbol, quote(state, clock))
    draft = strategy.drain_pending_intents()[0]
    assert draft.metadata["cw_entry_slot"] == "reclaim"
    assert draft.metadata["cw_flip_level"] == "2.0000"


def test_unproven_broker_boolean_does_not_resubmit_or_fanout():
    strategy, state, clock = setup()
    state.resting_is_broker_order = True
    strategy.on_quote(state.symbol, quote(state, clock, 1.925))
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_fanout_intents() == []


def test_actual_no_0930_quote_can_only_fail_closed_after_the_stop_has_crossed():
    strategy, state, clock = setup()
    clock[0] = ms(9, 31, 35)
    state.bars.append(recorded_bar(ORIGINAL["bars"][1]))
    strategy.on_quote(state.symbol, quote(state, clock, 1.925))
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("gate", ["pm_flip", "fanout_claim", "union_position", "fill_settling"])
def test_quote_boundary_gated_handover_never_runs_bar_manager_or_counts_thin_bar(gate, record_property):
    strategy, state, clock = setup(strategy_schwab_1m_v2_keep_rest_after_buy_enabled=True)
    state.bars[-1] = OHLCVBar(ms(9, 29), 1.7, 1.8, 1.6, 1.7, 1)
    state.resting_below_floor_bars = 1
    if gate == "pm_flip":
        state.pm_resting_flip_seen_ms = clock[0] - 1000
    elif gate == "fanout_claim":
        state.fanout_webull_claimed = True
    elif gate == "union_position":
        state.position_qty = 1
    else:
        state.resting_flip_ms = clock[0] - 1000
    native_track = strategy._cw_v2_resting_track
    calls = []
    def track(*args):
        calls.append(1)
        return native_track(*args)
    strategy._cw_v2_resting_track = track
    def invoke_quote(symbol, q):
        return strategy.on_quote(symbol, q)
    if os.environ.get("EH2RTH1_BASELINE_QUOTES") == "1":
        source = subprocess.check_output(["git", "show",
            "bf5f4e3b6e4ab5c68385b16549a290e465272d62:src/project_mai_tai/strategy_core/schwab_1m_v2.py"], text=True)
        cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
                   and node.name == "SchwabV2Strategy")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "on_quote")
        namespace = dict(vars(strategy_module))
        exec("from __future__ import annotations\n" + textwrap.dedent(ast.get_source_segment(source, method)), namespace)
        def invoke_quote(symbol, q):
            return namespace["on_quote"](strategy, symbol, q)
    for _ in range(20):
        invoke_quote(state.symbol, quote(state, clock))
    record_property("same_completed_bar_after_20_quotes", json.dumps({
        "gate": gate, "bar_manager_calls": len(calls), "resting_active": state.resting_active,
        "thin_count": state.resting_below_floor_bars,
        "native_bar_ms": state.bars[-1].timestamp_ms,
    }))
    assert calls == []
    assert state.resting_active and not state.resting_is_broker_order
    assert state.resting_below_floor_bars == 1
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_fanout_intents() == []


def test_reviewer_contract_accepts_initial_real_submit_unknown_ack(native_book):
    # Ruling12:0x explicitly accepts this initial real-submit shape, not arbitrary unknowns.
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        session.get(BrokerOrder, order_id).status = "accepted"
        event = session.get(BrokerOrderEvent, event_id)
        event.event_type, event.event_source = "accepted", "unknown"
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {
        ("TCRT", "GEN"): native_expected(status="accepted", submit_at=int(NATIVE_AT.timestamp()*1000))}


def test_recorded_completed_boundary_with_mirror_on_queues_pair_not_webull_only(record_property):
    strategy, state, clock = setup(mirror=True)
    row = ORIGINAL["boundary_quote"]
    clock[0] = int(datetime.fromisoformat(row["received_at"]).timestamp() * 1000)
    state.bars.clear()
    state.bars.append(recorded_bar(ORIGINAL["bars"][0]))
    strategy.on_quote(state.symbol, Quote(state.symbol, row["bid"], row["ask"], row["last"],
        int(datetime.fromisoformat(row["event_ts"]).timestamp() * 1000), 0))
    primary = strategy.drain_pending_intents()
    mirror = strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == 1
    assert primary[0].metadata["stop_price"] == mirror[0].metadata["stop_price"] == "1.9200"
    assert primary[0].metadata["rpg_resting_generation"] == mirror[0].metadata["rpg_resting_generation"]
    assert primary[0].metadata["fanout_slot_id"] == mirror[0].metadata["fanout_slot_id"]
    assert strategy.drain_webull_fanout_intents() == []
    clock[0] = int(datetime.fromisoformat(ORIGINAL["recorded_log_cross"]["time"]).timestamp() * 1000)
    state.bars.append(recorded_bar(ORIGINAL["bars"][1]))
    strategy.on_quote(state.symbol, quote(state, clock, ORIGINAL["recorded_log_cross"]["price"]))
    assert strategy.drain_webull_fanout_intents() == []
    record_property("counterfactual", "Recorded completed bars + LEVELONE boundary quote projected into REST callback; mirrorON queues primary/mirror STOP_LIMIT1.92 at09:30:06; both submissions/acceptance/fills and full initial owner state remain CONTROLLED/UNKNOWN, not actual broker execution")


class PgCompileSQLiteReadControl:
    """PG compilation/SET ordering control; SELECT executes on private SQLite, not PG."""

    def __init__(self, factory, *, fail=None, overflow=False):
        self.session = factory()
        self.commands = []
        self.fail = fail
        self.overflow = overflow
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()
        self.closed = True

    def get_bind(self):
        return SimpleNamespace(dialect=postgresql.dialect())

    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.commands.append(sql)
        stage = "lock" if sql.startswith("SET LOCAL lock") else "statement" if sql.startswith("SET LOCAL statement") else "select"
        if self.fail == stage:
            raise TimeoutError(f"CONTROLLED {stage} timeout/failure; no PG server")
        if stage != "select":
            return None  # Only ordering/compilation is controlled; no claimed PG SET execution.
        if self.overflow:
            return SimpleNamespace(all=lambda: [None] * 1001)
        return self.session.execute(statement)


def test_primary_book_pg_compile_bounds_before_functional_sqlite_select(native_book, record_property):
    bot, factory, _ = native_book
    control = PgCompileSQLiteReadControl(factory)
    bot.session_factory = lambda: control
    result = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert result == {("TCRT", "GEN"): native_expected()}
    assert control.commands[:2] == ["SET LOCAL lock_timeout = '250ms'", "SET LOCAL statement_timeout = '2s'"]
    assert len(control.commands) == 3
    sql = control.commands[-1]
    assert "LIMIT 1001" in sql and "LIMIT 1" in sql
    assert "broker_order_events" in sql and "trade_intents" in sql
    assert "lower(broker_orders.order_type) = 'stop_limit'" in sql
    assert control.closed
    record_property("database_control", "PostgreSQL dialect compilation and controlled SET ordering; full SELECT executed on private native SQLite. No actual PG server, timeout timing or production query claim.")


@pytest.mark.parametrize("stage", ["lock", "statement", "select"])
def test_primary_book_pg_control_failure_remains_unknown(native_book, stage):
    bot, factory, _ = native_book
    control = PgCompileSQLiteReadControl(factory, fail=stage)
    bot.session_factory = lambda: control
    assert SchwabV2BotService._fetch_primary_resting_book(bot) is None
    assert control.closed
    assert len(control.commands) == {"lock": 1, "statement": 2, "select": 3}[stage]


def test_primary_book_pg_compile_row_overflow_remains_unknown(native_book):
    bot, factory, _ = native_book
    control = PgCompileSQLiteReadControl(factory, overflow=True)
    bot.session_factory = lambda: control
    assert SchwabV2BotService._fetch_primary_resting_book(bot) is None
    assert control.closed


def test_primary_book_real_sqlite_does_not_receive_pg_settings(native_book):
    bot, factory, _ = native_book
    session = factory()
    execute = session.execute
    commands = []
    def capture(statement, *args, **kwargs):
        commands.append(str(statement))
        return execute(statement, *args, **kwargs)
    session.execute = capture
    bot.session_factory = lambda: session
    assert SchwabV2BotService._fetch_primary_resting_book(bot)[("TCRT", "GEN")]["status"] == "working"
    assert len(commands) == 1 and not any(command.startswith("SET ") for command in commands)


@pytest.mark.parametrize("bad", ["source", "segment", "slot", "slot_id", "entry_slot",
    "intent_identity", "intent_account", "intent_symbol", "intent_side", "order_side", "provider", "strategy"])
def test_ruling_a_requires_exact_own_v2_identity(native_book, bad):
    bot, factory, (order_id, _) = native_book
    with factory() as session:
        order = session.get(BrokerOrder, order_id)
        intent = session.get(TradeIntent, order.intent_id)
        metadata = dict(order.payload)
        if bad in {"source", "segment", "slot", "slot_id", "entry_slot"}:
            key = {"segment": "fanout_segment_id", "slot": "fanout_slot",
                   "slot_id": "fanout_slot_id", "entry_slot": "cw_entry_slot"}.get(bad, bad)
            metadata[key] = {"source": "manual", "segment": "0", "slot": "invalid",
                             "slot_id": "OTHER", "entry_slot": "invalid"}[bad]
            order.payload = metadata
            intent.payload = {"metadata": metadata}
        elif bad == "intent_identity":
            intent.payload = {"metadata": {**metadata, "fanout_segment_id": "123"}}
        elif bad == "intent_account":
            intent.broker_account_id = uuid4()
        elif bad == "intent_symbol":
            intent.symbol = "OTHER"
        elif bad == "intent_side":
            intent.side = "sell"
        elif bad == "order_side":
            order.side = "sell"
        elif bad == "provider":
            session.get(BrokerAccount, order.broker_account_id).provider = "webull"
        else:
            session.get(Strategy, order.strategy_id).code = "orb"
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}


@pytest.mark.parametrize("bad", ["submitted", "client", "missing_body", "wrong_coid",
    "wrong_response_identity", "later_unknown"])
def test_ruling_b_unknown_only_initial_real_submit(native_book, bad):
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status = "accepted"
        event.event_type, event.event_source = "accepted", "unknown"
        if bad == "submitted":
            order.status = event.event_type = "submitted"
        elif bad == "client":
            event.event_source = "client"
        elif bad == "missing_body":
            event.payload = {}
        elif bad == "wrong_coid":
            event.payload = {**event.payload, "client_order_id": "not-ours"}
        elif bad == "wrong_response_identity":
            event.payload = {**event.payload, "metadata": {**NATIVE_METADATA, "fanout_slot_id": "OTHER"}}
        else:
            session.add(BrokerOrderEvent(order_id=order_id, event_type="working",
                event_source="broker", event_at=NATIVE_AT.replace(second=0, minute=29)))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}


@pytest.mark.parametrize("terminal", ["filled", "cancelled", "canceled", "rejected", "expired"])
@pytest.mark.parametrize("revived", [False, True])
def test_ruling_c_terminal_event_veto_even_if_row_still_accepted(native_book, terminal, revived):
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status = "accepted"
        event.event_type, event.event_source = "accepted", "unknown"
        event.event_at = NATIVE_AT.replace(minute=29, second=57)
        session.add(BrokerOrderEvent(order_id=order_id, event_type=terminal,
            event_source="broker", event_at=NATIVE_AT.replace(minute=29, second=58)))
        if revived:
            session.add(BrokerOrderEvent(order_id=order_id, event_type="accepted",
                event_source="broker", event_at=NATIVE_AT.replace(minute=29, second=59)))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {}


@pytest.mark.parametrize("age_ms,broker_id,allowed", [
    (0, None, True), (14999, None, True), (15000, None, False), (15001, None, False),
    (-1, None, False), (15001, "CONTROLLED-ID", True), (15001, " ", False)])
def test_ruling_d_real_submit_without_id_has_exact_fifteen_second_bound(native_book, age_ms, broker_id, allowed):
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status, order.broker_order_id = "accepted", broker_id
        event.event_type, event.event_source = "accepted", "unknown"
        event.event_at = NATIVE_AT - timedelta(milliseconds=age_ms)
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert bool(book) is allowed
    if allowed:
        assert book[("TCRT", "GEN")]["submit_response_at_ms"] == int((NATIVE_AT-timedelta(milliseconds=age_ms)).timestamp()*1000)


def test_later_native_working_read_maps_to_accepted_and_retains_submit_receipt(native_book):
    bot, factory, (order_id, event_id) = native_book
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        order.status, order.broker_order_id = "accepted", None
        event.event_type, event.event_source = "accepted", "unknown"
        event.event_at = NATIVE_AT.replace(minute=29, second=59)
        session.add(BrokerOrderEvent(order_id=order_id, event_type="accepted", event_source="broker",
                                    event_at=NATIVE_AT))
        session.commit()
    assert SchwabV2BotService._fetch_primary_resting_book(bot) == {
        ("TCRT", "GEN"): native_expected(status="accepted", broker_id="",
            submit_at=int(NATIVE_AT.replace(minute=29, second=59).timestamp()*1000))}


@pytest.mark.parametrize("bad", ["segment", "slot", "entry_slot", "slot_id", "submitted"])
def test_memory_primary_proof_never_crosses_another_economic_identity(bad):
    strategy, state, clock = setup()
    strategy.on_quote(state.symbol, quote(state, clock))
    strategy.drain_pending_intents()
    updates = {"segment": {"segment_id": state.fanout_segment_id+1}, "slot": {"slot": "reclaim"},
        "entry_slot": {"entry_slot": "reclaim"}, "slot_id": {"slot_id": "OTHER"},
        "submitted": {"status": "submitted"}}[bad]
    prove(strategy, state, clock, **updates)
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("age,allowed", [(14999, True), (15000, False), (15001, False), (-1, False)])
def test_memory_idless_ack_ages_on_tick_without_db_or_primary_resubmit(age, allowed):
    strategy, state, clock = setup()
    strategy.on_quote(state.symbol, quote(state, clock))
    primary, = strategy.drain_pending_intents()
    prove(strategy, state, clock, broker_order_id="", status="accepted",
          submit_response_at_ms=clock[0]-age)
    strategy._fanout_rth_resting_cross(state, quote(state, clock, 1.925))
    assert bool(strategy.drain_webull_fanout_intents()) is allowed
    assert strategy.drain_pending_intents() == []  # Do not duplicate the already queued primary.
    assert primary.metadata["order_type"] == "STOP_LIMIT"


@pytest.mark.parametrize("terminal", [False, True])
def test_retained_native_unknown_submit_shape_with_actual_terminal_veto(native_book, terminal, record_property):
    packet = json.loads((Path(__file__).parents[1] / "fixtures/eh2rth1_native_submit_ack.json").read_text())
    bot, factory, (order_id, event_id) = native_book
    at = datetime.fromisoformat(packet["event_at"])
    bot.observed[0] = at + timedelta(seconds=1)
    with factory() as session:
        order, event = session.get(BrokerOrder, order_id), session.get(BrokerOrderEvent, event_id)
        intent = session.get(TradeIntent, order.intent_id)
        order.symbol = intent.symbol = packet["symbol"]
        order.client_order_id = packet["payload"]["client_order_id"]
        order.broker_order_id = packet["payload"]["broker_order_id"]
        order.status = "accepted"  # Explicit controlled pre-terminal row projection.
        order.payload = packet["payload"]["metadata"]
        intent.payload = {"metadata": packet["payload"]["metadata"]}
        event.event_type, event.event_source, event.event_at = packet["event_type"], packet["event_source"], at
        event.payload = packet["payload"]
        if terminal:
            row = packet["retained_terminal"]
            session.add(BrokerOrderEvent(order_id=order_id, event_type=row["event_type"],
                event_source=row["event_source"], event_at=datetime.fromisoformat(row["event_at"])))
        session.commit()
    book = SchwabV2BotService._fetch_primary_resting_book(bot)
    assert bool(book) is not terminal
    if not terminal:
        proof = book[(packet["symbol"], packet["payload"]["metadata"]["rpg_resting_generation"])]
        assert proof["broker_order_id"] == packet["payload"]["broker_order_id"]
        assert proof["status"] == "accepted"
    record_property("native_shape_provenance", packet["provenance"] + " sourceSHA=" + packet["source_sha256"])
