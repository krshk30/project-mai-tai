"""ORBOWN1: primary v2 ownership must not import another strategy's shared-account shares."""
from __future__ import annotations

import inspect
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.services import schwab_1m_v2_bot as module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from tests.unit.test_v2_flip_owned_first_entry import _bar, _book, _place_first, _strategy

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/orbown1_cases.json").read_text())
PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"
V2, ORB = "schwab_1m_v2", "orb_schwab"


@pytest.fixture
def replay():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([
            Strategy(code=V2, name=V2, execution_mode="live"),
            Strategy(code=ORB, name=ORB, execution_mode="live"),
            BrokerAccount(name=PRIMARY, provider="schwab", environment="live"),
            BrokerAccount(name=WEBULL, provider="webull", environment="live"),
        ])
        session.commit()
    strategy, clock, _, writes = _strategy(dual=True)
    strategy._resting_trigger_offset_pct = float(FIXTURE["gixi_line_control"]["offset_pct"])
    strategy.settings.strategy_schwab_1m_v2_resting_buy_round_up_enabled = True
    bot = object.__new__(SchwabV2BotService)
    bot.settings, bot.session_factory, bot.strategy = strategy.settings, factory, strategy
    bot._exit_coverage = set()
    yield bot, factory, strategy, clock, writes
    engine.dispose()


def _position(factory, code, symbol, quantity, account=PRIMARY, price="6.33"):
    with factory() as session:
        sid = session.scalar(select(Strategy.id).where(Strategy.code == code))
        aid = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == account))
        row = session.scalar(select(VirtualPosition).where(
            VirtualPosition.strategy_id == sid, VirtualPosition.broker_account_id == aid,
            VirtualPosition.symbol == symbol))
        if row is None:
            row = VirtualPosition(strategy_id=sid, broker_account_id=aid, symbol=symbol)
            session.add(row)
        row.quantity, row.average_price = Decimal(str(quantity)), Decimal(price)
        session.commit()


def _intent(factory, code, symbol, account=PRIMARY, quantity=3, status="submitted"):
    with factory() as session:
        sid = session.scalar(select(Strategy.id).where(Strategy.code == code))
        aid = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == account))
        intent = TradeIntent(strategy_id=sid, broker_account_id=aid, symbol=symbol, side="buy",
                             intent_type="open", quantity=Decimal(quantity), status=status,
                             reason="ownership control", payload={})
        session.add(intent)
        session.commit()


def _apply_maps(bot, symbol):
    maps = bot._fetch_position_maps()
    assert maps is not None
    positions, held = maps
    bot.strategy.update_position(symbol, positions.get(symbol, 0), held_qty=held.get(symbol, 0))
    return maps


def test_primary_maps_exclude_orb_and_fanout_but_keep_owned_shares_and_intents(replay):
    bot, factory, _, _, _ = replay
    _position(factory, ORB, "ORBONLY", 2)
    _position(factory, ORB, "MIXED", 80)
    _position(factory, V2, "MIXED", 3)
    _position(factory, V2, "FANOUTONLY", 4, WEBULL)
    _intent(factory, ORB, "ORBWORK", quantity=8)
    _intent(factory, V2, "OWNWORK", quantity=5)
    _intent(factory, V2, "WEBULLWORK", WEBULL)
    assert bot._fetch_position_maps() == ({"MIXED": 3, "OWNWORK": 5}, {"MIXED": 3})
    assert bot._fetch_open_positions() == {"MIXED": 3, "OWNWORK": 5}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["symbol"])
def test_recorded_orb_poll_never_mints_unknown_or_consumes_v2_or_webull_claim(replay, case, caplog):
    bot, factory, strategy, clock, writes = replay
    symbol = case["symbol"]
    clock[0] = int(datetime.fromisoformat(case["poll_at"]).timestamp() * 1000)
    state = strategy.watchlist_state(symbol)
    state.bars.append(_bar(clock[0]))
    _position(factory, ORB, symbol, case["quantity"], price=case["entry_price"])
    assert _apply_maps(bot, symbol) == ({}, {})
    assert state.position_qty_held == 0 and state.position_qty == 0
    assert state.flip_owner_phase == "idle"
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert not state.fanout_webull_claimed and writes == []
    assert "[V2-FLIP-OWNER-UNKNOWN]" not in caplog.text
    _position(factory, ORB, symbol, 0)
    _apply_maps(bot, symbol)
    # Actual observed level; controlled admission, not a full historical execution replay.
    clock[0] = int(datetime.fromisoformat(FIXTURE["gixi_line_control"]["observed_at"]).timestamp() * 1000)
    _book(strategy, clock, symbol)
    state.bars.append(_bar(clock[0]))
    strategy._queue_resting_place(state, 6.0171, slot="first")
    drafts = strategy.drain_pending_intents()
    assert len(drafts) == 1 and drafts[0].metadata["cw_entry_slot"] == "first"
    raw_trigger = strategy._resting_trigger_for_line(6.0171)
    assert Decimal(str(raw_trigger)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == Decimal("6.05")
    assert drafts[0].metadata["resting_wire_stop_price"] == "6.0500"


def test_gixi_1126_branch_has_no_orb_consumed_slot(replay):
    bot, factory, strategy, clock, _ = replay
    _position(factory, ORB, "GIXI", 2)
    _apply_maps(bot, "GIXI")
    _position(factory, ORB, "GIXI", 0)
    _apply_maps(bot, "GIXI")
    control = FIXTURE["gixi_line_control"]
    clock[0] = int(datetime.fromisoformat(control["earlier_eligible_bar_at"]).timestamp() * 1000)
    state = strategy.watchlist_state("GIXI")
    state.bars.append(_bar(clock[0]))
    _book(strategy, clock, "GIXI")
    strategy._queue_resting_place(state, float(control["earlier_line"]), slot="first")
    drafts = strategy.drain_pending_intents()
    assert len(drafts) == 1 and drafts[0].metadata["cw_entry_slot"] == "first"


def test_actual_owned_fill_still_consumes_first_rest_and_binds_primary(replay):
    bot, factory, strategy, clock, _ = replay
    state, _ = _place_first(strategy, clock, "OWNFILL")
    _position(factory, ORB, "OWNFILL", 2)
    _position(factory, V2, "OWNFILL", 3)
    assert _apply_maps(bot, "OWNFILL") == ({"OWNFILL": 3}, {"OWNFILL": 3})
    assert state.cw_resting_taken
    assert state.position_qty_held == 3
    assert PRIMARY in state.flip_owner_fill_accounts
    assert state.flip_owner_phase != "unknown"


def test_fresh_boot_does_not_restore_an_orb_derived_owner_but_keeps_owned_unknown(replay):
    bot, factory, strategy, _, _ = replay
    store = FlipEntryOwnershipStore(factory)
    def persist(record, active, reason):
        store.record(record, active=active, reason=reason)
    strategy.configure_flip_entry_ownership(persist, active_segments={}, restore_readable=True)
    for symbol, code in (("GIXI", ORB), ("OWNUNBOUND", V2)):
        _position(factory, code, symbol, 2)
        _apply_maps(bot, symbol)
    restored = store.restore_active()
    assert "GIXI" not in restored
    assert restored["OWNUNBOUND"].phase == "unknown"
    fresh, _, _, _ = _strategy(dual=True)
    fresh.configure_flip_entry_ownership(persist, active_segments={}, restored=restored,
                                        restore_readable=True)
    assert fresh.watchlist_state("GIXI").flip_owner_phase == "idle"
    assert fresh.watchlist_state("OWNUNBOUND").flip_owner_phase == "unknown"


@pytest.mark.parametrize("row", FIXTURE["durable_unknown_rows"], ids=lambda row: row["payload"]["symbol"])
def test_real_old_unknown_is_session_scoped_not_silently_archived(replay, row):
    bot, factory, _, _, _ = replay
    created = datetime.fromisoformat(row["created_at"])
    symbol = row["payload"]["symbol"]
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type="v2_flip_entry_ownership",
                                      payload=row["payload"], created_at=created))
        session.commit()
    _position(factory, ORB, symbol, 2)
    assert bot._fetch_position_maps() == ({}, {})
    store = FlipEntryOwnershipStore(factory)
    # Scoped positions cannot retroactively identify a same-session unbound UNKNOWN.
    same_session = store.restore_active(now=created + timedelta(minutes=1))
    assert same_session[symbol].phase == "unknown"
    next_anchor = (created + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    assert symbol in store.restore_active(now=next_anchor - timedelta(microseconds=1))
    assert store.restore_active(now=next_anchor) == {}
    fresh, _, _, _ = _strategy(dual=True)
    fresh.configure_flip_entry_ownership(lambda *_args: None,
                                        restored=store.restore_active(now=next_anchor),
                                        active_segments={}, restore_readable=True)
    assert fresh.watchlist_state(symbol).flip_owner_phase == "idle"
    with factory() as session:
        snapshots = session.scalars(select(DashboardSnapshot)).all()
        assert len(snapshots) == 1 and snapshots[0].payload == row["payload"]


def _entry_fill(factory, code, symbol, now):
    with factory() as session:
        sid = session.scalar(select(Strategy.id).where(Strategy.code == code))
        aid = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == PRIMARY))
        md = {"cw_entry_slot": "first", "atr_variant": "cw-v2-resting", "resting_entry": "true"}
        intent = TradeIntent(strategy_id=sid, broker_account_id=aid, symbol=symbol, side="buy",
                             intent_type="open", quantity=Decimal("2"), status="filled",
                             reason="ownership control", payload={"metadata": md})
        session.add(intent)
        session.flush()
        order = BrokerOrder(strategy_id=sid, broker_account_id=aid, intent_id=intent.id,
                            client_order_id=f"{code}-{symbol}-{uuid4().hex[:8]}",
                            broker_order_id=f"LOCAL-{symbol}", symbol=symbol, side="buy",
                            quantity=Decimal("2"), status="filled", order_type="STOP_LIMIT",
                            time_in_force="day", submitted_at=now, payload=md)
        session.add(order)
        session.flush()
        session.add(Fill(order_id=order.id, strategy_id=sid, broker_account_id=aid,
                         broker_fill_id=f"LOCAL-{uuid4()}", symbol=symbol, side="buy",
                         quantity=Decimal("2"), price=Decimal("6.33"), filled_at=now, payload={}))
        session.commit()


def test_flip_book_and_confirmation_discovery_already_exclude_other_strategy(replay, monkeypatch):
    bot, factory, _, _, _ = replay
    now = datetime(2026, 10, 12, 13, 30, 21, tzinfo=UTC)
    monkeypatch.setattr(module, "_current_scanner_session_start_utc",
                        lambda: datetime(2026, 10, 12, 8, tzinfo=UTC))
    for code, symbol in ((V2, "OWN"), (ORB, "GIXI")):
        _entry_fill(factory, code, symbol, now)
        with factory() as session:
            session.add(OmsManagedPosition(strategy_code=code, broker_account_name=PRIMARY,
                                           symbol=symbol, current_quantity=2, original_quantity=2,
                                           entry_price=Decimal("6.33"), entry_time=now))
            session.commit()
    book = bot._fetch_flip_position_book()
    assert book.readable and set(book.legs_by_symbol) == {"OWN"}
    assert [entry.symbol for entry in bot._load_confirmation_entries()] == ["OWN"]


def test_account_wide_coverage_remains_subscription_only_and_cannot_claim_entry(replay):
    bot, factory, strategy, _, _ = replay
    with factory() as session:
        aid = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == PRIMARY))
        session.add(AccountPosition(broker_account_id=aid, symbol="GIXI", quantity=Decimal("2"),
                                    average_price=Decimal("6.33")))
        session.commit()
    assert "GIXI" in bot._fetch_managed_symbols()
    assert _apply_maps(bot, "GIXI") == ({}, {})
    assert strategy.watchlist_state("GIXI").flip_owner_phase == "idle"


def test_missing_v2_registration_and_db_failure_do_not_import_foreign_shares(replay):
    bot, factory, _, _, _ = replay
    _position(factory, ORB, "GIXI", 2)
    with factory() as session:
        session.delete(session.scalar(select(Strategy).where(Strategy.code == V2)))
        session.commit()
    assert bot._fetch_position_maps() == ({}, {})

    def unreadable():
        raise RuntimeError("controlled DB read failure")

    bot.session_factory = unreadable
    assert bot._fetch_position_maps() is None


@pytest.mark.asyncio
async def test_position_poll_stays_off_loop_at_five_seconds(replay, monkeypatch):
    bot, factory, strategy, _, _ = replay
    _position(factory, ORB, "GIXI", 2)
    bot._watchlist = {"GIXI"}
    bot._exit_coverage = set()
    bot._roll_stale_session_state = lambda *_args: None
    bot._release_entry_state_at_window_close = lambda: None
    bot._push_desired_symbols = lambda: None
    threads = []
    original = bot._fetch_position_maps

    def traced_read():
        threads.append(threading.get_ident())
        return original()

    monkeypatch.setattr(bot, "_fetch_position_maps", traced_read)

    async def noop():
        return None

    for name in ("_evaluate_gap_holds", "_fresh_sell_owner_release_poll", "_removed_wait_poll",
                 "_sync_confirmation_entries", "_drain_direct_strategy_intents",
                 "_drain_atr_sell_observations", "_sync_gateway_subscription"):
        monkeypatch.setattr(bot, name, noop)
    await bot._position_poll_pass()
    assert threads and all(t != threading.get_ident() for t in threads)
    assert strategy.watchlist_state("GIXI").flip_owner_phase == "idle"
    assert module.POSITION_POLL_INTERVAL_SECONDS == 5
    assert "asyncio.to_thread(self._fetch_position_maps)" in inspect.getsource(
        SchwabV2BotService._position_poll_pass)
