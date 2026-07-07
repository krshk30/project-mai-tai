"""OMS SPOF fix — blocking-DB-on-the-event-loop resilience (the zombie cure).

Covers the four fixes from docs/oms-spof-blocking-db-fix-design.md:
  Fix 1  engine timeouts (statement/lock/connect/pool) — the universal backstop.
  Fix 2  `_run_db` executor offload of the hot pure-sync DB blocks.
  Fix 3  hard-stop protection decoupled from DB bookkeeping (the P2 gate).
  Fix 4  control-loop hardening — a bad intent / DB stall skip-continues, never
         exits the service, and the heartbeat keeps beating during a DB outage.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.db import session as db_session
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


def build_test_session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},  # _run_db uses a worker thread
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _bare_service() -> OmsRiskService:
    """A service instance with __init__ bypassed — set only what a test needs."""
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    return svc


# --------------------------------------------------------------------------- #
# Fix 1 — engine timeouts reach create_engine (and default stays byte-identical)
# --------------------------------------------------------------------------- #
def test_build_engine_default_is_byte_identical(monkeypatch):
    fake = Mock(return_value="ENGINE")
    monkeypatch.setattr(db_session, "create_engine", fake)
    db_session.build_engine.cache_clear()
    db_session.build_engine("postgresql+psycopg://u:p@h/db_default")
    db_session.build_engine.cache_clear()
    # No timeout kwargs supplied → exactly the historical call, no connect_args.
    fake.assert_called_once_with("postgresql+psycopg://u:p@h/db_default", pool_pre_ping=True)


def test_build_engine_applies_all_timeouts(monkeypatch):
    fake = Mock(return_value="ENGINE")
    monkeypatch.setattr(db_session, "create_engine", fake)
    db_session.build_engine.cache_clear()
    db_session.build_engine(
        "postgresql+psycopg://u:p@h/db_timed",
        connect_timeout_s=5,
        statement_timeout_ms=5000,
        lock_timeout_ms=3000,
        pool_timeout_s=5,
        pool_recycle_s=1800,
    )
    db_session.build_engine.cache_clear()
    _, kwargs = fake.call_args
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_timeout"] == 5
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["connect_args"]["connect_timeout"] == 5
    # statement_timeout AND lock_timeout both set via the libpq options string.
    assert "-c statement_timeout=5000" in kwargs["connect_args"]["options"]
    assert "-c lock_timeout=3000" in kwargs["connect_args"]["options"]


def test_oms_session_factory_honours_disable_flag(monkeypatch):
    """The rollback lever: MAI_TAI_OMS_DB_TIMEOUTS_ENABLED=false → untimed engine."""
    calls = []
    monkeypatch.setattr(db_session, "create_engine", lambda url, **kw: calls.append(kw) or "E")
    db_session.build_engine.cache_clear()
    db_session.build_oms_session_factory(Settings(oms_db_timeouts_enabled=False, database_url="postgresql+psycopg://u:p@h/off"))
    db_session.build_engine.cache_clear()
    assert calls == [{"pool_pre_ping": True}]  # no connect_args / pool_timeout


def test_oms_session_factory_applies_timeouts_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(db_session, "create_engine", lambda url, **kw: calls.append(kw) or "E")
    db_session.build_engine.cache_clear()
    db_session.build_oms_session_factory(
        Settings(oms_db_timeouts_enabled=True, database_url="postgresql+psycopg://u:p@h/on")
    )
    db_session.build_engine.cache_clear()
    assert "connect_args" in calls[0] and calls[0]["pool_timeout"] == 5


# --------------------------------------------------------------------------- #
# Fix 2 — _run_db runs off the event loop, commits, and rolls back on error
# --------------------------------------------------------------------------- #
class _RecordingSession:
    def __init__(self, log, *, fail=False):
        self._log = log
        self._fail = fail

    def __enter__(self):
        self._log.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, exc_type, *_):
        self._log.append(("exit", exc_type))
        return False  # never suppress

    def commit(self):
        self._log.append(("commit", None))

    def do_work(self):
        if self._fail:
            raise RuntimeError("db stalled/timed out")
        return "ok"


def test_run_db_executes_off_the_event_loop_and_commits():
    svc = _bare_service()
    log: list = []
    svc.session_factory = lambda: _RecordingSession(log)
    main_thread = threading.get_ident()

    async def go():
        return await svc._run_db(lambda s: s.do_work())

    result = asyncio.run(go())
    assert result == "ok"
    # ran on a DIFFERENT thread than the caller (the loop was never blocked)
    enter_thread = next(t for kind, t in log if kind == "enter")
    assert enter_thread != main_thread
    assert ("commit", None) in log


def test_run_db_no_commit_when_disabled():
    svc = _bare_service()
    log: list = []
    svc.session_factory = lambda: _RecordingSession(log)
    asyncio.run(svc._run_db(lambda s: s.do_work(), commit=False))
    assert all(kind != "commit" for kind, _ in log)


def test_run_db_propagates_and_rolls_back_on_exception():
    svc = _bare_service()
    log: list = []
    svc.session_factory = lambda: _RecordingSession(log, fail=True)

    with pytest.raises(RuntimeError):
        asyncio.run(svc._run_db(lambda s: s.do_work()))
    # context manager exited WITH the exception (rollback path); no commit happened
    assert any(kind == "exit" and exc is not None for kind, exc in log)
    assert all(kind != "commit" for kind, _ in log)


def test_sync_broker_positions_is_behaviour_identical_and_offloads_db():
    """The incident method: broker awaits on-loop, DB writes off-loop, same result."""
    session_factory = build_test_session_factory()
    with session_factory() as s:
        s.add(BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="live", is_active=True))
        s.commit()

    svc = _bare_service()
    svc.settings = Settings(redis_stream_prefix="test", oms_adapter="simulated")
    svc.session_factory = session_factory
    svc.store = OmsStore()
    loop_thread = {}

    class _Adapter:
        async def list_account_positions(self, name):
            loop_thread["broker"] = threading.get_ident()  # broker await runs on the loop thread
            return [BrokerPositionSnapshot(
                broker_account_name=name, symbol="KIDZ", quantity=Decimal("10"),
                average_price=Decimal("1.19"), market_value=None, as_of=None,
            )]

    svc.broker_adapter = _Adapter()

    async def go():
        loop_thread["main"] = threading.get_ident()
        return await svc.sync_broker_positions()

    summary = asyncio.run(go())
    assert summary == {"accounts": 1, "positions": 1}
    assert loop_thread["broker"] == loop_thread["main"]  # broker REST stayed on the loop
    # and the position was persisted by the off-loop write phase
    with session_factory() as s:
        acct = s.query(BrokerAccount).filter_by(name="live:schwab_1m_v2").one()
        pos = svc.store.get_account_position(s, broker_account_id=acct.id, symbol="KIDZ")
        assert pos is not None and pos.quantity == Decimal("10")


def test_has_active_native_stop_guard_order_uses_offloaded_db():
    session_factory = build_test_session_factory()
    svc = _bare_service()
    svc.session_factory = session_factory
    svc.store = OmsStore()
    # no strategy/account rows → returns False (and never touches the loop-blocking path)
    result = asyncio.run(svc._has_active_native_stop_guard_order(
        strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2", symbol="KIDZ",
    ))
    assert result is False


# --------------------------------------------------------------------------- #
# Fix 3 — the P2 gate: the hard stop fires even when the position-sync DB hangs
# --------------------------------------------------------------------------- #
def _armed_stop() -> ArmedHardStop:
    return ArmedHardStop(
        strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2", symbol="KIDZ",
        quantity=Decimal("10"), entry_price=Decimal("1.20"), stop_loss_pct=1.5,
        stop_price=Decimal("1.182"), quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
    )


def test_hard_stop_fires_when_preclose_position_sync_hangs(monkeypatch):
    """P2 PROOF (a): the pre-close native-guard check is a DB read on the stop
    path. If it HANGS/times out, the protective close must STILL be submitted —
    a DB stall can never abort real-money stop protection."""
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)

    svc = _bare_service()
    svc._armed_hard_stops = {}

    async def hung_position_sync(**_):
        raise TimeoutError("sync_account_positions -> session.flush() hung on psycopg wait")

    svc._has_active_native_stop_guard_order = hung_position_sync

    submitted: list = []

    async def fake_process_trade_intent(event):
        submitted.append(event)
        return [SimpleNamespace(payload=SimpleNamespace(status="filled", reason="HARD_STOP"))]

    svc.process_trade_intent = fake_process_trade_intent

    asyncio.run(svc._trigger_hard_stop(_armed_stop(), trigger_price=Decimal("1.18"), trigger_source="bid"))

    assert len(submitted) == 1, "the protective close was NOT submitted when the position-sync hung"
    assert submitted[0].payload.reason == "HARD_STOP"


def test_hard_stop_still_fires_when_native_guard_check_returns_false(monkeypatch):
    """Control case: healthy pre-check returning False → close submitted (unchanged)."""
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)
    svc = _bare_service()
    svc._armed_hard_stops = {}

    async def no_guard(**_):
        return False

    svc._has_active_native_stop_guard_order = no_guard
    submitted: list = []

    async def fake_pti(event):
        submitted.append(event)
        return [SimpleNamespace(payload=SimpleNamespace(status="filled", reason="HARD_STOP"))]

    svc.process_trade_intent = fake_pti
    asyncio.run(svc._trigger_hard_stop(_armed_stop(), trigger_price=Decimal("1.18"), trigger_source="bid"))
    assert len(submitted) == 1


def test_reconcile_after_intent_swallows_a_hung_sync():
    """P2 PROOF (b): the post-close reconcile is best-effort — a hung
    sync_broker_state must NOT propagate/unwind the already-submitted close."""
    svc = _bare_service()

    async def hung_sync(*, account_names=None):
        raise TimeoutError("post-close reconcile hung")

    svc.sync_broker_state = hung_sync
    # must return normally (no raise), leaving the submitted order intact
    asyncio.run(svc._reconcile_after_intent("live:schwab_1m_v2"))


# --------------------------------------------------------------------------- #
# Fix 4 — control loop survives a bad intent + DB stalls and keeps beating
# --------------------------------------------------------------------------- #
def _loop_settings():
    return SimpleNamespace(
        service_heartbeat_interval_seconds=1,
        oms_broker_sync_interval_seconds=5,
        oms_adapter_label="sim",
        active_broker_providers=["simulated"],
    )


class _ScriptedRedis:
    """xread returns one 'bad' intent, then keeps returning [] until told to stop."""
    def __init__(self, stop_after):
        self.n = 0
        self._stop_after = stop_after
        self.beats: list[str] = []

    async def xread(self, offsets, block=0, count=0):
        self.n += 1
        await asyncio.sleep(0.02)  # let wall-clock advance toward the 1s heartbeat
        if self.n == 1:
            return [("mai_tai:strategy-intents", [("1-0", {"data": "{not json"})])]
        return []

    async def xadd(self, *a, **k):
        return "1-0"


def test_control_loop_survives_bad_intent_and_db_outage_and_heartbeats():
    svc = _bare_service()
    svc.settings = _loop_settings()
    svc._intent_offsets = {"mai_tai:strategy-intents": "$"}
    stop_event = asyncio.Event()
    redis = _ScriptedRedis(stop_after=None)
    svc.redis = redis
    beats: list[str] = []

    async def bad_interval():
        raise TimeoutError("db stalled")  # fatal-gap #1 (was un-wrapped)

    async def bad_sync(*, account_names=None):
        raise TimeoutError("db stalled")  # periodic sync during DB outage

    async def boom_handle(fields):
        raise ValueError("malformed intent")  # fatal-gap #2 (was un-wrapped)

    async def record_heartbeat(status, details):
        beats.append(status)
        stop_event.set()  # end the test right after the first successful beat

    svc._broker_sync_interval_seconds = bad_interval
    svc.sync_broker_state = bad_sync
    svc._handle_stream_message = boom_handle
    svc._publish_heartbeat = record_heartbeat

    async def go():
        await asyncio.wait_for(svc._run_control_loop(stop_event), timeout=5)

    asyncio.run(go())
    # The loop neither raised nor exited when the intent handler AND both DB calls
    # failed — and the heartbeat still fired (watchdog stays informed → not a zombie).
    assert beats == ["healthy"]


# --------------------------------------------------------------------------- #
# PR-A — tick-path DB off-load (the two per-tick freeze drivers)
#   _evaluate_v2_managed_exit (read + no-exit/dedup write) and
#   _cancel_drifted_working_orders now run their DB off the event loop via _run_db,
#   keeping broker awaits + in-memory dict mutations on-loop. The mandatory
#   stop-decoupling proof: a stall in ANY off-loaded unit can never block/abort the
#   protective stop, and no dict-mutation race is introduced.
# --------------------------------------------------------------------------- #
def _v2_tick_settings() -> SimpleNamespace:
    return SimpleNamespace(
        oms_quote_drift_cancel_tolerance_cents=1.0,
        oms_v2_exit_management_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        oms_v2_exit_quote_max_age_ms=5000,
        oms_v2_exit_close_on_fill_enabled=True,
    )


def test_pra_armed_stop_fires_even_when_tickpath_db_units_stall(monkeypatch):
    """STOP-DECOUPLING PROOF (PR-A): with EVERY off-loaded DB unit stalling
    (TimeoutError — Fix-1 surfaces a hung psycopg connection this way), a quote that
    breaches the stop STILL submits the protective close. The stop is evaluated
    UPSTREAM of the off-loaded tick-path DB, the stalls run on worker threads, and both
    tick methods swallow-and-continue — so a DB stall can neither block nor abort
    real-money stop protection, nor skip the downstream v2 hard-stop eval."""
    import project_mai_tai.oms.service as svc_mod
    monkeypatch.setattr(svc_mod, "_is_regular_market_session", lambda *a, **k: True)

    svc = _bare_service()
    svc.settings = _v2_tick_settings()
    svc._latest_quotes_by_symbol = {}
    svc._latest_trades_by_symbol = {}
    stop = _armed_stop()  # KIDZ, stop_price 1.182
    svc._armed_hard_stops = {
        svc._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol): stop
    }
    svc._managed_v2_symbols = {stop.symbol}  # so step 4 (v2 exit) also runs and stalls

    async def hung_run_db(fn, *, commit=True):
        raise TimeoutError("tick-path DB unit hung on psycopg wait")

    svc._run_db = hung_run_db  # every off-loaded unit stalls (read/write, v2 + drift-cancel)

    submitted: list = []

    async def fake_process_trade_intent(event):
        submitted.append(event)
        return [SimpleNamespace(payload=SimpleNamespace(status="filled", reason="HARD_STOP"))]

    svc.process_trade_intent = fake_process_trade_intent

    event = QuoteTickEvent(
        source_service="market-data",
        payload=QuoteTickPayload(
            symbol=stop.symbol, bid_price=Decimal("1.18"), ask_price=Decimal("1.19")
        ),
    )
    # Must complete without raising (both tick methods swallow the stalled DB) ...
    asyncio.run(svc._handle_quote_tick_event(event))
    # ... and the protective close was still submitted.
    assert len(submitted) == 1, "protective close was NOT submitted when tick-path DB units stalled"
    assert submitted[0].payload.reason == "HARD_STOP"


def test_pra_v2_exit_db_runs_off_loop_while_dict_mutation_stays_on_loop():
    """OFF-LOAD + NO-RACE PROOF (PR-A): the v2 managed-exit DB unit executes on a WORKER
    thread (off the event loop), while the `_managed_v2_symbols` mutation stays on the
    LOOP thread — so no off-loaded unit ever races the stop path on a shared dict."""
    session_factory = build_test_session_factory()
    svc = _bare_service()
    svc.session_factory = session_factory
    svc.store = OmsStore()
    svc.settings = _v2_tick_settings()
    svc._latest_quotes_by_symbol = {
        "KIDZ": {"bid": 1.25, "ask": 1.26, "received_at": datetime.now(UTC)}
    }
    svc._latest_trades_by_symbol = {}

    main_ident = threading.get_ident()
    db_idents: list[int] = []
    real_run_db = svc._run_db  # bound real _run_db (thread-offloading)

    async def spy_run_db(fn, *, commit=True):
        def wrapped(session):
            db_idents.append(threading.get_ident())
            return fn(session)

        return await real_run_db(wrapped, commit=commit)

    svc._run_db = spy_run_db

    discard_idents: list[int] = []

    class _TrackSet(set):
        def discard(self, value):
            discard_idents.append(threading.get_ident())
            super().discard(value)

    svc._managed_v2_symbols = _TrackSet({"KIDZ"})

    # No managed row exists → the off-loop READ returns None → discard runs on-loop.
    asyncio.run(svc._evaluate_v2_managed_exit("KIDZ"))

    assert db_idents, "the v2 read unit did not go through _run_db"
    assert all(ident != main_ident for ident in db_idents), "DB unit ran ON the event-loop thread"
    assert discard_idents == [main_ident], "_managed_v2_symbols mutation left the loop thread (race!)"


def test_pra_drift_cancel_never_dies_on_a_db_stall():
    """PR-A never-die guard: a stalled DB unit in the drift-cancel path is swallowed
    (loop survives) so it can never propagate or skip the downstream v2 hard-stop eval
    that runs later in the same quote handler."""
    svc = _bare_service()
    svc.settings = SimpleNamespace(oms_quote_drift_cancel_tolerance_cents=1.0)
    svc._latest_quotes_by_symbol = {"KIDZ": {"bid": 1.20, "ask": 1.50}}

    async def hung_run_db(fn, *, commit=True):
        raise TimeoutError("drift-cancel DB unit hung on psycopg wait")

    svc._run_db = hung_run_db
    # Must return normally (guard swallows), never raise.
    assert asyncio.run(svc._cancel_drifted_working_orders("KIDZ")) is None
