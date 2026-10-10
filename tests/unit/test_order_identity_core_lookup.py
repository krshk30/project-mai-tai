"""Core ID lookup preserves the existing ORM identity and transaction fallback."""

from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from project_mai_tai.db.models import BrokerOrder
from tests.unit.test_intent_order_scalar_reads import database as controlled_database

database = controlled_database


def arguments(ids, coid="core-id-order"):
    return dict(intent=SimpleNamespace(id=None), strategy_id=ids[0], broker_account_id=ids[1],
        client_order_id=coid, symbol="MI", side="buy", quantity=Decimal(1))


@pytest.mark.parametrize("kind", ["missing", "cached", "unloaded", "expired"])
def test_clean_order_id_is_core_and_hydration_retains_exact_identity(database, kind):
    store, factory, ids = database
    args = arguments(ids)
    with factory() as session:
        saved = None
        if kind != "missing":
            saved = store.get_or_create_order(session, **args, metadata={"original": "yes"},
                status="accepted", broker_order_id="original-broker")
            session.commit()
            ident = saved.id
            if kind == "unloaded":
                session.expunge(saved)
            elif kind == "expired":
                session.expire(saved)
        statements, loaded = [], []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        event.listen(session, "loaded_as_persistent", lambda _session, row: loaded.append(row))
        actual = store.get_or_create_order(session, **args, metadata={"reason": "controlled"},
            status="rejected", reject_reason="controlled-reject")
        assert statements[0] is False
        assert actual.payload == {"reason": "controlled", "reject_reason": "controlled-reject"}
        assert actual.status == "rejected"
        if kind != "missing":
            assert actual.id == ident and actual.broker_order_id == "original-broker"
            assert (actual is saved) is (kind != "unloaded")
        assert len(loaded) == int(kind == "unloaded")
        assert len(session.scalars(select(BrokerOrder)).all()) == 1
        session.rollback()


@pytest.mark.parametrize("mode", ["autoflush", "disabled", "context"])
@pytest.mark.parametrize("kind", ["dirty", "pending", "rename"])
def test_nonclean_or_noautoflush_lookup_preserves_original_behavior(database, mode, kind):
    store, factory, ids = database
    args = arguments(ids)
    with factory() as session:
        saved = store.get_or_create_order(session, **args, metadata={},
            status="accepted", broker_order_id="original-broker")
        if kind != "pending":
            session.commit()
        else:
            session.rollback()
            saved = BrokerOrder(**{key: value for key, value in args.items() if key != "intent"},
                order_type="market", time_in_force="day", status="pending", payload={})
            session.add(saved)
        if kind == "dirty":
            saved.payload = {"uncommitted": "yes"}
        elif kind == "rename":
            saved.client_order_id = args["client_order_id"] = "renamed-core-order"
        if mode == "disabled":
            session.autoflush = False
        statements = []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        with session.no_autoflush if mode == "context" else nullcontext():
            if mode != "autoflush" and kind in {"pending", "rename"}:
                # The old ID query cannot see the unflushed coid either. Do not
                # replace its duplicate failure with an identity-map shortcut.
                with pytest.raises(IntegrityError):
                    store.get_or_create_order(session, **args, metadata={}, status="rejected")
            else:
                actual = store.get_or_create_order(session, **args, metadata={"updated": "yes"},
                    status="rejected")
                assert actual is saved and actual.payload == {"updated": "yes"}
                assert actual.status == "rejected"
        assert statements[0] is True
        session.rollback()
    with factory() as session:
        rows = session.scalars(select(BrokerOrder)).all()
        assert len(rows) == int(kind != "pending")
        if rows:
            assert rows[0].status == "accepted" and rows[0].payload == {}
            assert rows[0].client_order_id == "core-id-order"


@pytest.mark.parametrize("mode", ["autoflush", "disabled", "context"])
def test_deleted_order_lookup_retains_original_flush_and_identity(database, mode):
    store, factory, ids = database
    args = arguments(ids)
    with factory() as session:
        saved = store.get_or_create_order(session, **args, metadata={}, status="accepted")
        session.commit()
        ident = saved.id
        session.delete(saved)
        if mode == "disabled":
            session.autoflush = False
        statements = []
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.is_orm_statement))
        with session.no_autoflush if mode == "context" else nullcontext():
            actual = store.get_or_create_order(session, **args, metadata={}, status="rejected")
        assert statements[0] is True
        assert (actual is saved) is (mode != "autoflush")
        assert (actual.id == ident) is (mode != "autoflush")
        session.rollback()
    with factory() as session:
        rows = session.scalars(select(BrokerOrder)).all()
        assert len(rows) == 1 and rows[0].id == ident and rows[0].status == "accepted"


@pytest.mark.parametrize("existing", [False, True])
def test_clean_order_creation_or_update_rolls_back_without_extra_rows(database, existing):
    store, factory, ids = database
    args = arguments(ids)
    if existing:
        with factory() as session:
            store.get_or_create_order(session, **args, metadata={"original": "yes"}, status="accepted")
            session.commit()
    with factory() as session:
        store.get_or_create_order(session, **args, metadata={"new": "yes"}, status="rejected")
        session.rollback()
    with factory() as session:
        rows = session.scalars(select(BrokerOrder)).all()
        assert len(rows) == int(existing)
        if rows:
            assert rows[0].status == "accepted" and rows[0].payload == {"original": "yes"}
