"""Narrow rollover controls; DB absence is not complete publication inventory."""
import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base, BrokerAccount, BrokerOrder, DashboardSnapshot, OmsManagedPosition, Strategy, TradeIntent,
)
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, TradeIntentDraft
from project_mai_tai.v2_removed_wait import (
    RemovedWait, RemovedWaitProof, RemovedWaitStore, assess_removed_wait, prior_session_request,
)

PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"
ACCOUNTS = {PRIMARY, WEBULL}
NOW = datetime(2026, 10, 8, 13, 13, 42, tzinfo=UTC)
OLD = datetime(2026, 10, 7, 18, 14, tzinfo=UTC)
RAW = json.loads((Path(__file__).parents[1] / "fixtures/clearwait1_rollover.json").read_text())


def ms(at):
    return int(at.timestamp() * 1000)


def request(purpose="scanner_removal"):
    return RemovedWait("DKI", ms(OLD), "exact-token", ms(OLD) + 1000, (PRIMARY, WEBULL), purpose)


@pytest.fixture
def db(monkeypatch):
    import project_mai_tai.services.schwab_1m_v2_bot as service_module
    retire = RemovedWaitStore.retire_prior_sessions
    monkeypatch.setattr(RemovedWaitStore, "retire_prior_sessions",
        lambda self, *args, **kw: retire(self, *args, **{"now": NOW, **kw}))
    monkeypatch.setattr(service_module, "current_session_anchor", lambda: current_session_anchor(NOW))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    columns = (BrokerOrder.__table__.c.status, TradeIntent.__table__.c.status)
    nullable = [c.nullable for c in columns]
    try:
        for column in columns:
            column.nullable = True
        Base.metadata.create_all(engine)
    finally:
        for column, original in zip(columns, nullable):
            column.nullable = original
    sessions = sessionmaker(engine)
    ids = {name: uuid4() for name in ACCOUNTS}
    strategy_id = uuid4()
    with sessions() as session:
        session.add(Strategy(id=strategy_id, code="schwab_1m_v2", name="controlled"))
        for name, ident in ids.items():
            session.add(BrokerAccount(id=ident, name=name, environment="live", external_account_id=name,
                                      provider="webull" if name == WEBULL else "schwab"))
        session.commit()
    yield RemovedWaitStore(sessions), sessions, ids, strategy_id
    engine.dispose()


def strategy(req):
    strat = SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_removed_wait_clear_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL))
    strat._now_ms = lambda: ms(NOW)
    strat._removed_wait_requests = {req.symbol: req}
    return strat


def seed(db, req, receipts=True):
    """Controlled positive canonical receipts, never inserted into recorded replay."""
    store, sessions, ids, strategy_id = db
    at = datetime.fromtimestamp(req.requested_at_ms / 1000, UTC)
    with sessions() as session:
        session.add(DashboardSnapshot(snapshot_type="v2_removed_wait", created_at=at,
                                      payload=req.payload(active=True)))
        if receipts:
            session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch", created_at=at,
                payload={"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": req.symbol,
                         "opportunity_id": str(req.opportunity_id), "kind": "begin",
                         "account_names": list(req.account_names)}))
            for name in req.account_names:
                session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=ids[name],
                    symbol=req.symbol, side="buy", intent_type="cancel", quantity=100,
                    reason="controlled canonical receipt", status="rejected", created_at=at, updated_at=at,
                    payload={"refusal_origin": "skipped_before_submit", "refusal_code": "cancel_target_not_found",
                             "metadata": {"clearwait_removal_token": req.token,
                                          "clearwait_opportunity_id": str(req.opportunity_id),
                                          "clearwait_buy_only": "true",
                                          "reason": "retry_budget_exhausted",
                                          "fanout_segment_id": str(req.opportunity_id),
                                          "fanout_slot": "resting",
                                          "fanout_slot_id": fanout_slot_id(strategy_code="schwab_1m_v2",
                                              symbol=req.symbol, segment_id=req.opportunity_id, slot="resting")}}))
        session.commit()


@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted"])
def test_prior_session_durable_retirement_and_memory(db, purpose):
    store, sessions, _, _ = db
    req = request(purpose)
    seed(db, req)
    strat = strategy(req)
    proof, = store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)
    assert proof.clear and proof.reason == "session_rollover"
    strat.apply_removed_wait_rollover([proof])
    assert not strat._removed_wait_gate_closed("DKI") and store.restore() == {}
    with sessions() as session:
        rows = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_removed_wait").order_by(DashboardSnapshot.created_at)).all()
    assert rows[0].payload == req.payload(active=True)
    assert rows[-1].payload["active"] is False and rows[-1].payload["reason"] == "session_rollover"


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("kind", ["order", "managed", "open", "cancel"])
@pytest.mark.parametrize("status", ["submitting", None])
def test_working_other_generation_and_strategy_keeps(db, account, kind, status):
    store, sessions, ids, strategy_id = db
    req = request()
    seed(db, req)
    with sessions() as session:
        if kind == "order":
            session.add(BrokerOrder(strategy_id=uuid4(), broker_account_id=ids[account], symbol="DKI",
                side="sell", order_type="limit", time_in_force="day", quantity=100,
                status=status, client_order_id=str(uuid4()), payload={"fanout_segment_id": "OTHER"}))
        elif kind == "managed":
            session.add(OmsManagedPosition(strategy_code="orb", broker_account_name=account,
                symbol="DKI", entry_price=5, original_quantity=100, current_quantity=0, status="open"))
        else:
            session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=ids[account], symbol="DKI",
                side="buy", intent_type=kind, quantity=100, status=status, reason="controlled pending"))
        session.commit()
        if status is None and kind != "managed":
            model = BrokerOrder if kind == "order" else TradeIntent
            session.execute(update(model).where(model.status == "pending").values(status=None))
            session.commit()
    proof, = store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)
    assert not proof.clear and store.restore() == {"DKI": req}


@pytest.mark.parametrize("opportunity", [0, ms(NOW), ms(NOW + timedelta(days=1))])
@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted"])
def test_current_unknown_future_segment_unchanged(db, opportunity, purpose):
    store, _, _, _ = db
    req = replace(request(purpose), opportunity_id=opportunity)
    seed(db, req, receipts=False)
    assert not store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)[0].clear
    assert store.restore() == {"DKI": req}


def test_exact_04_boundary_and_current_rewrite():
    boundary = datetime(2026, 10, 8, 8, tzinfo=UTC)
    req = replace(request(), opportunity_id=ms(boundary) - 1, requested_at_ms=ms(boundary) + 1)
    assert not prior_session_request(req, boundary - timedelta(milliseconds=1))
    assert prior_session_request(req, boundary)
    assert not prior_session_request(replace(req, opportunity_id=ms(boundary)), NOW)
    winter = datetime(2026, 11, 3, 9, tzinfo=UTC)
    assert current_session_anchor(winter) == winter


@pytest.mark.parametrize("names", [{PRIMARY}, {PRIMARY, WEBULL, "unknown"}, set()])
def test_bound_accounts_no_fallback(db, names):
    store, _, _, _ = db
    req = request()
    seed(db, req)
    proof, = store.retire_prior_sessions((req,), names, now=NOW)
    assert not proof.clear and proof.reason == "account_binding_unknown"


def test_cas_newer_token_stale_proof_and_failed_commit(db, monkeypatch):
    store, _, _, _ = db
    req = request()
    seed(db, req)
    newer = replace(req, token="new-token")
    store.record(newer, True)
    assert not store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)[0].clear
    strat = strategy(newer)
    strat.apply_removed_wait_rollover([RemovedWaitProof(req, ms(NOW), True, "session_rollover")])
    strat.apply_removed_wait_rollover([RemovedWaitProof(newer, ms(NOW) - 15_001, True, "session_rollover")])
    assert strat._removed_wait_requests == {"DKI": newer}
    store.record(req, True)
    monkeypatch.setattr(Session, "commit", lambda *_: (_ for _ in ()).throw(RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)
    assert store.restore() == {"DKI": req}


@pytest.mark.parametrize("fault", ["missing_receipts", "unreadable", "stale", "pending_retry"])
def test_empty_db_or_unknown_proof_never_grants_rollover(db, monkeypatch, fault):
    import project_mai_tai.v2_removed_wait as module
    store, sessions, _, _ = db
    req = request()
    seed(db, req, receipts=fault != "missing_receipts")
    if fault == "unreadable":
        monkeypatch.setattr(store, "proofs", lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError("unreadable")))
    elif fault == "stale":
        clock = iter([0, 31])
        monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    elif fault == "pending_retry":
        with sessions() as session:
            session.add(DashboardSnapshot(snapshot_type="oms_webull_mirror_retained_hold",
                                          payload={"event": {"payload": {"symbol": "DKI"}}}))
            session.commit()
    assert not store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)[0].clear
    assert store.restore() == {"DKI": req}


def test_gate_logs_state_changes_only_zero_sql(db, caplog):
    store, sessions, _, _ = db
    req = request("retry_exhausted")
    strat = strategy(req)
    event.listen(sessions.kw["bind"], "before_cursor_execute",
                 lambda *_: pytest.fail("hot gate SQL"))
    with caplog.at_level("INFO"):
        for _ in range(100):
            assert strat._removed_wait_gate_closed("DKI")
        strat._removed_wait_requests.clear()
        assert not strat._removed_wait_gate_closed("DKI")
        strat._removed_wait_requests["DKI"] = req
        assert strat._removed_wait_gate_closed("DKI")
    logs = [r.message for r in caplog.records if "[V2-REMOVED-WAIT-GATE]" in r.message]
    assert len(logs) == 2 and all("reason=retry_exhausted" in r and "age_s=" in r for r in logs)


def service(strat, store):
    result = object.__new__(SchwabV2BotService)
    result.strategy, result.settings = strat, strat.settings
    result.session_factory, result._removed_wait_store = store.session_factory, store
    result._clearwait_open_emits = {}
    result._removed_wait_roll_anchor = current_session_anchor(OLD)
    return result


@pytest.mark.asyncio
async def test_boot_off_loop_retire_before_barriers_and_unknown_keeps(db, monkeypatch):
    store, _, _, _ = db
    req = request()
    seed(db, req)
    strat = strategy(req)
    bot = service(strat, store)
    parent_thread = threading.get_ident()
    retire = RemovedWaitStore.retire_prior_sessions

    def checked(self, *args):
        assert threading.get_ident() != parent_thread
        return retire(self, *args)

    monkeypatch.setattr(RemovedWaitStore, "retire_prior_sessions", checked)
    await bot._configure_removed_wait_store()
    assert not strat._removed_wait_requests and not strat._pending_intents
    current = replace(req, opportunity_id=0, token="current-zero", requested_at_ms=ms(NOW))
    store.record(current, True)
    await bot._configure_removed_wait_store()
    assert strat._removed_wait_requests == {"DKI": current}
    assert strat._pending_intents and strat._pending_webull_direct_intents


@pytest.mark.asyncio
async def test_reset_new_token_and_inflight_after_await_keep(db, monkeypatch):
    import project_mai_tai.services.schwab_1m_v2_bot as module
    store, _, _, _ = db
    req = request()
    strat = strategy(req)
    bot = service(strat, store)
    bot._clearwait_open_emits["DKI"] = 1
    threaded = AsyncMock()
    monkeypatch.setattr(module.asyncio, "to_thread", threaded)
    await bot._removed_wait_poll()
    threaded.assert_not_awaited()
    bot._clearwait_open_emits.clear()

    async def changed(*_):
        strat._removed_wait_requests["DKI"] = replace(req, token="new")
        bot._clearwait_open_emits["DKI"] = 1
        return (RemovedWaitProof(req, ms(NOW), True, "session_rollover"),)

    threaded.side_effect = changed
    await bot._removed_wait_poll()
    assert strat._removed_wait_requests["DKI"].token == "new"
    threaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_stalled_reset_quote_memory_only_and_cancel_exit_not_delayed(db):
    import project_mai_tai.services.schwab_1m_v2_bot as module
    store, _, _, _ = db
    req = request()
    strat = strategy(req)
    bot = service(strat, store)
    entered, release = threading.Event(), threading.Event()

    def paused(*_):
        entered.set()
        assert release.wait(5)
        return ()

    store.retire_prior_sessions = paused
    store.proofs = lambda *_: ()
    bot._last_tick_at, bot._last_quote_at_ms, bot._last_quote_by_symbol = {}, {}, {}
    bot._gap_hold_enabled = bot._line_restoration_enabled = False
    bot._observe_line_trade = bot._observe_halt_from_quote = lambda *_: None
    bot.intent_emitter = bot.webull_intent_emitter = None
    bot._emit_webull_fanout_legs = bot._maybe_emit = AsyncMock()
    task = asyncio.create_task(bot._removed_wait_poll())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(bot._handle_quote("DKI", module.Quote("DKI", 4.78, 4.79, 4.7871, ms(NOW))), .25)
        assert not task.done() and strat._removed_wait_gate_closed("DKI")
        emitter = SimpleNamespace(emit=AsyncMock())
        for symbol, side, kind in [("AIXI", "buy", "cancel"), ("DKI", "sell", "close")]:
            draft = TradeIntentDraft(symbol, side, kind, Decimal(100), "controlled protection", {})
            assert await asyncio.wait_for(bot._emit_removal_tracked(emitter, draft), .25)
        buy = TradeIntentDraft("DKI", "buy", "open", Decimal(100), "delayed draft", {})
        bot._line_draft_allowed = lambda *_: True
        assert not await bot._emit_removal_tracked(emitter, buy)
        assert emitter.emit.await_count == 2
    finally:
        release.set()
        await asyncio.wait_for(task, 5)


def recorded_request(symbol):
    p = next(r["payload"] for r in RAW["removal"] if r["payload"]["symbol"] == symbol and r["payload"]["active"])
    return RemovedWait(symbol, int(p["opportunity_id"]), p["token"], int(p["requested_at_ms"]),
                       tuple(p["account_names"]), p.get("purpose", "scanner_removal"))


@pytest.mark.parametrize("symbol", ["AIXI", "SBFM", "DKI"])
def test_recorded_same_day_four_outcome_receipts(symbol):
    req = recorded_request(symbol)
    at = datetime.fromtimestamp(req.requested_at_ms / 1000, UTC)
    receipts = [r for r in RAW["removal"] if r["payload"]["symbol"] == symbol and not r["payload"]["active"]]
    observed = datetime.fromisoformat(receipts[0]["created_at"]) if receipts else datetime.fromisoformat(RAW["current_read_at_utc"])
    intents = [SimpleNamespace(**{**r, "broker_account_id": r["account"],
        "created_at": datetime.fromisoformat(r["created_at"]), "updated_at": datetime.fromisoformat(r["updated_at"])})
        for r in RAW["intents"] if r["symbol"] == symbol and datetime.fromisoformat(r["created_at"]) <= observed]
    orders = [SimpleNamespace(**{**r, "broker_account_id": r["account"], "updated_at": datetime.fromisoformat(r["updated_at"])})
        for r in RAW["orders"] if r["symbol"] == symbol and datetime.fromisoformat(r["submitted_at"]) <= observed]
    snapshots = [SimpleNamespace(snapshot_type="v2_removed_wait", payload=req.payload(active=True))]
    snapshots += [SimpleNamespace(snapshot_type="v2_wait_dispatch", payload=r["payload"])
                  for r in RAW["dispatch"] if r["payload"]["symbol"] == symbol]
    kwargs = dict(accounts={a: a for a in ACCOUNTS}, intents=intents, orders=orders,
                  filled_order_ids={o.id for o in orders if o.status == "filled"}, order_events=[],
                  snapshots=snapshots, has_position=False)
    assert not assess_removed_wait(req, now=at, **kwargs).clear
    proof = assess_removed_wait(req, now=observed, **kwargs)
    assert proof.clear is (symbol != "DKI")
    if symbol == "SBFM":
        assert 20 <= (observed - at).total_seconds() < 21
    if symbol == "AIXI":
        assert proof.reason == "retry_leftovers_cancelled_owner_kept"
        strat = strategy(req)
        strat._now_ms = lambda: ms(observed)
        strat._removed_wait_persist = lambda *_: None
        state = strat.watchlist_state(symbol)
        state.flip_owner_phase = "consumed"
        state.flip_owner_opportunity_id = state.fanout_segment_id = req.opportunity_id
        state.flip_owner_retry_closes_at_place = state.retry_one_closes_in_segment = 1
        state.flip_owner_fill_accounts.add(WEBULL)
        strat.apply_removed_wait_proofs([proof])
        assert state.flip_owner_phase == "consumed" and state.fanout_segment_id == req.opportunity_id
        assert state.flip_owner_retry_closes_at_place == state.retry_one_closes_in_segment == 1
        assert state.flip_owner_fill_accounts == {WEBULL}


def test_recorded_17_transitions_canonical_unknown_not_synthetic_clear(db):
    store, sessions, _, _ = db
    assert len(RAW["archive"]) == 17
    assert all(not v for v in RAW["archive_proof"].values())
    with sessions() as session:
        for raw in RAW["archive"]:
            session.add(DashboardSnapshot(id=UUID(raw["id"]), snapshot_type="v2_removed_wait",
                created_at=datetime.fromisoformat(raw["created_at"]), payload=raw["payload"]))
        session.commit()
    restored = store.restore()
    assert len(restored) == 7
    proofs = store.retire_prior_sessions(tuple(restored.values()), ACCOUNTS,
                                       now=datetime.fromisoformat(RAW["read_at_utc"]))
    assert not any(p.clear for p in proofs)
    assert set(store.restore()) == {"OLOX", "BIYA", "DKI", "LPCN", "MTEN", "LGCL", "FFR"}
    assert {r.symbol for r in restored.values() if prior_session_request(r, NOW)} == {"OLOX", "BIYA", "DKI"}


@pytest.mark.asyncio
async def test_pending_and_inflight_generic_cancel_keep_reset_closed(db):
    store, _, _, _ = db
    strat = strategy(request())
    bot = service(strat, store)
    entered, release = asyncio.Event(), asyncio.Event()
    draft = TradeIntentDraft("DKI", "buy", "cancel", Decimal(100), "untagged protection", {})
    strat._pending_intents.append(draft)
    assert bot._removed_wait_rollover_busy()
    strat._pending_intents.clear()

    async def paused(*_):
        entered.set()
        await asyncio.wait_for(release.wait(), 2)

    emitter = SimpleNamespace(emit=AsyncMock(side_effect=paused))
    task = asyncio.create_task(bot._emit_removal_tracked(emitter, draft))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert bot._removed_wait_emits_inflight()
        store.retire_prior_sessions = lambda *_: pytest.fail("inflight reset must not query")
        await bot._removed_wait_poll()
        assert strat._removed_wait_requests
    finally:
        release.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_unreadable_boot_preserves_global_gate(db, monkeypatch):
    store, _, _, _ = db
    strat = strategy(request())
    bot = service(strat, store)
    monkeypatch.setattr(RemovedWaitStore, "restore", lambda *_: (_ for _ in ()).throw(RuntimeError("unreadable")))
    await bot._configure_removed_wait_store()
    assert not strat._removed_wait_restore_readable and strat._removed_wait_gate_closed("OTHER")
