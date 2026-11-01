"""Source-authored D3 query controls; execution belongs to the serial executor."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from project_mai_tai import cancelack1
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from tests.unit.test_cancelack1 import synthetic_case
from tests.unit.test_cancelack1_four_sites import confirmation_case, fault_history
from tests.unit.test_confirmation_exit import _confirmation_store, _seed_confirmation_fill
from tests.unit.test_v2_fresh_sell_owner_release import MIRROR, PRIMARY, refresh


def book(symbols):
    factory, strategy_id, accounts = _confirmation_store()
    names = {"schwab": "live:schwab_1m_v2", "webull": "live:webull"}
    stamp = datetime.now(UTC)
    for symbol in symbols:
        for provider, name in names.items():
            order_id, _ = _seed_confirmation_fill(factory, strategy_id=strategy_id,
                account_id=accounts[provider], provider=provider, symbol=symbol,
                slot_id="controlled-shared-" + symbol, filled_at=stamp)
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
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_confirmation_account_neutral_discovery_enabled=True,
        strategy_schwab_1m_v2_account_name=names["schwab"],
        strategy_schwab_1m_v2_webull_account_name=names["webull"])
    bot = SchwabV2BotService(settings, session_factory=factory)
    entries = bot._load_confirmation_entries()
    assert {entry.symbol for entry in entries} == set(symbols)
    return bot, entries, factory.kw["bind"]


def test_held_and_unknown_books_still_check_working_buys_for_each_symbol():
    bot, entries, engine = book(("DHELD", "DUNKNOWN"))
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    try:
        event.listen(engine, "before_cursor_execute", count)
        assert bot._closed_confirmation_entry_ids(entries) == set()
        # Three own-book reads plus three working-BUY evidence reads per symbol.
        assert len(statements) == 9
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        event.remove(engine, "before_cursor_execute", count)
        with bot.session_factory() as session:
            rows = list(session.scalars(select(OmsManagedPosition)))
            assert len(rows) == 4
            assert all(row.status == "open" and row.current_quantity == 1
                       for row in rows if row.symbol == "DHELD")
            assert all(row.status == "closed" and row.current_quantity == 0
                       for row in rows if row.symbol == "DUNKNOWN")
            assert len(list(session.scalars(select(VirtualPosition)))) == 3
    finally:
        engine.dispose()


def test_mixed_books_read_working_buys_once_for_every_symbol(monkeypatch):
    bot, entries, engine = book(("DHELD", "DUNKNOWN", "DFLAT"))
    actual = cancelack1.working_buy_present
    checked = []

    def observe(session, symbol, **kwargs):
        checked.append(symbol)
        return actual(session, symbol, **kwargs)

    monkeypatch.setattr(cancelack1, "working_buy_present", observe)
    try:
        flat = bot._closed_confirmation_entry_ids(entries)
        assert flat == {entry.order_id for entry in entries if entry.symbol == "DFLAT"}
        assert set(checked) == {"DFLAT", "DHELD", "DUNKNOWN"}
        assert len(checked) == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("status", ["accepted", "unknown"])
def test_complete_flat_book_still_checks_foreign_account_and_strategy(provider, status):
    bot, entries, engine = book(("DFLAT",))
    try:
        assert bot._closed_confirmation_entry_ids(entries) == {entry.order_id for entry in entries}
        with bot.session_factory.begin() as session:
            foreign = Strategy(code="orb_schwab", name="controlled foreign owner")
            account = BrokerAccount(name="controlled:outside-confirmation", provider=provider,
                                    environment="production")
            session.add_all([foreign, account])
            session.flush()
            session.add(BrokerOrder(strategy_id=foreign.id, broker_account_id=account.id,
                symbol="DFLAT", side="buy", quantity=1, status=status,
                client_order_id="controlled-working-" + uuid4().hex, order_type="limit", time_in_force="day"))
        assert bot._closed_confirmation_entry_ids(entries) == set()
    finally:
        engine.dispose()


@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,)])
@pytest.mark.parametrize("fault", ["none", "missing_history", "other_orb_working", "stale_positions",
                                   "missing_actual_vp", "held_actual_vp", "historical_other_leg"])
def test_missing_row_obligation_keeps_actual_proof_guards(monkeypatch, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, fault)
        clock[0] += 24 * 60 * 60_000
        refresh(bot, clock)
        fault_history(bot, request, clock, fault)
        assert (entry.order_id in bot._closed_confirmation_entry_ids([entry])) is (fault == "none")
        assert writes == [] and state.flip_owner_phase == "bound"
    finally:
        engine.dispose()


def test_guard_read_failure_cannot_certify_flat(monkeypatch):
    bot, entries, engine = book(("DFLAT",))

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("controlled working-BUY evidence unreadable")

    monkeypatch.setattr(cancelack1, "working_buy_present", unreadable)
    try:
        with pytest.raises(RuntimeError, match="evidence unreadable"):
            bot._closed_confirmation_entry_ids(entries)
    finally:
        engine.dispose()
