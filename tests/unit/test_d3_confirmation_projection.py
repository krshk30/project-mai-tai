"""Installed D3 entity-read and closed-episode identity controls."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event, select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, VirtualPosition
from tests.unit.test_d3_held_working_buy_reads import book


@contextmanager
def loading_probe(factory):
    engine = factory.kw["bind"]
    observed = {"loaded": [], "sql": []}

    def loaded(session, instance):
        if session.get_bind() is engine:
            observed["loaded"].append(type(instance))

    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        observed["sql"].append(statement)

    event.listen(factory.class_, "loaded_as_persistent", loaded)
    event.listen(engine, "before_cursor_execute", sql)
    try:
        yield observed
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(factory.class_, "loaded_as_persistent", loaded)


def test_installed_entity_reads_check_working_buys_for_each_symbol():
    bot, entries, engine = book(("DHELD", "DUNKNOWN"))
    actual_factory = bot.session_factory
    try:
        with loading_probe(actual_factory) as projection:
            assert bot._closed_confirmation_entry_ids(entries) == set()
        # The installed path retains ORM entities and checks each symbol's
        # working orders, intents and handoffs, even when its own books hold it.
        assert len(projection["sql"]) == 9
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in projection["sql"])
        assert {Fill, BrokerOrder, BrokerAccount, OmsManagedPosition, VirtualPosition} <= set(projection["loaded"])
    finally:
        bot.session_factory = actual_factory
        engine.dispose()


@pytest.mark.parametrize("fault", ["none", "managed_open_zero", "managed_unknown", "vp_positive",
    "vp_missing", "managed_coid", "fill_quantity", "fill_native_id", "order_native_id", "provider"])
def test_entity_reads_preserve_positive_and_identity_refusals(fault):
    bot, entries, engine = book(("DFLAT",))
    actual_factory = bot.session_factory
    try:
        with actual_factory.begin() as session:
            managed = session.scalar(select(OmsManagedPosition).where(
                OmsManagedPosition.broker_account_name == entries[0].broker_account_name))
            source_order = session.get(BrokerOrder, entries[0].order_id)
            fill = session.get(Fill, entries[0].fill_id)
            if fault == "managed_open_zero":
                managed.status = "open"
            elif fault == "managed_unknown":
                managed.status = "unknown"
            elif fault in {"vp_positive", "vp_missing"}:
                mirror = session.scalar(select(VirtualPosition).join(BrokerAccount).where(
                    BrokerAccount.name == "live:webull"))
                if fault == "vp_missing":
                    session.delete(mirror)
                else:
                    mirror.quantity = 1
            elif fault == "managed_coid":
                managed.entry_client_order_id = "controlled-wrong-entry"
            elif fault == "fill_quantity":
                fill.quantity = 0
            elif fault == "fill_native_id":
                fill.broker_fill_id = "controlled-wrong-fill"
            elif fault == "order_native_id":
                source_order.broker_order_id = "controlled-wrong-order"
            elif fault == "provider":
                session.get(BrokerAccount, source_order.broker_account_id).provider = "controlled-unknown"
        with loading_probe(actual_factory) as probe:
            projected = bot._closed_confirmation_entry_ids(entries)
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in probe["sql"])
        assert projected == ({entries[0].order_id} if fault == "none" else set())
        # A second fresh session must make the same decision without relying on
        # detached entity state or a cached result.
        assert bot._closed_confirmation_entry_ids(entries) == projected
    finally:
        bot.session_factory = actual_factory
        engine.dispose()


def test_projection_reads_current_books_each_time_without_result_cache():
    bot, entries, engine = book(("DFLAT",))
    try:
        with loading_probe(bot.session_factory) as first:
            assert bot._closed_confirmation_entry_ids(entries) == {entries[0].order_id}
        with bot.session_factory.begin() as session:
            managed = session.scalar(select(OmsManagedPosition).where(
                OmsManagedPosition.broker_account_name == "live:webull"))
            managed.status, managed.current_quantity = "open", 1
        with loading_probe(bot.session_factory) as second:
            assert bot._closed_confirmation_entry_ids(entries) == set()
        assert len(first["sql"]) == len(second["sql"]) == 6
        assert all(statement.lstrip().upper().startswith("SELECT")
                   for probe in (first, second) for statement in probe["sql"])
        assert VirtualPosition in first["loaded"] and VirtualPosition in second["loaded"]
    finally:
        engine.dispose()
