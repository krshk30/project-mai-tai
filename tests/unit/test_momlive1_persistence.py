from datetime import UTC, datetime
from decimal import Decimal as D
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, TradeIntent, VirtualPosition
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.persistence import LivePersistence
from project_mai_tai.oms.store import OmsStore
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_momlive1_admission import ledger, signal


def setup():
    factory = build_test_session_factory()
    store = LivePersistence(OmsStore())
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    data = book.snapshot()[0]
    with factory() as session:
        assert store.claim(session, episode=data, account="live:schwab_1m_v2", provider="schwab", signal={})
        session.commit()
    request = OrderRequest(f"momentum_warn_live-SCNI-open-{episode.episode_id[:12]}",
        "live:schwab_1m_v2", STRATEGY_CODE, "SCNI", "buy", "open", D(3), "MOMLIVE1", {"episode_id": episode.episode_id}, "limit", "day")
    return factory, store, book, episode, request


def test_durable_idempotent_claim_restore_and_capacity():
    factory, store, book, episode, _ = setup()
    with factory() as session:
        assert not store.claim(session, episode=book.snapshot()[0], account="live:schwab_1m_v2", provider="schwab", signal={})
        assert len(session.scalars(select(TradeIntent)).all()) == 1
        recovered = store.restore(session, datetime(2026, 10, 14, tzinfo=UTC))
        assert recovered[0]["episode_id"] == episode.episode_id
        second, _ = book.claim(signal("DLXY"), now_ns=10)
        assert store.claim(session, episode=book.snapshot()[1], account="live:schwab_1m_v2", provider="schwab", signal={})
        session.commit()
        third = dict(book.snapshot()[1], episode_id=str(uuid4()), symbol="THIRD")
        with pytest.raises(Refusal, match="reservations_full"):
            store.claim(session, episode=third, account="live:schwab_1m_v2", provider="schwab", signal={})
        assert second.episode_id != episode.episode_id


def test_parent_persisted_before_order_and_no_resubmit():
    factory, store, _, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        session.commit()
        with pytest.raises(Refusal, match="no_resubmit"):
            store.prepare_order(session, episode.episode_id, request, provider="schwab")
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


def test_partial_fill_delta_precision_idempotency_and_late_foreign_owner_exit_bound():
    factory, store, _, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        report = ExecutionReport("partially_filled", request.client_order_id,
            broker_order_id="own-broker", broker_fill_id="fill1", filled_quantity=D(1),
            fill_price=D("3.0012"), reported_at=datetime(2026, 10, 14, 13, tzinfo=UTC), origin="broker")
        first = store.record(session, request, [report, report])
        assert first["quantity"] == 1 and first["price"] == D("3.0012")
        assert len(session.scalars(select(Fill)).all()) == 1
        parent = session.get(TradeIntent, UUID(episode.episode_id))
        atr = store.store.ensure_strategy(session, "schwab_1m_v2", name="ATR", execution_mode="live")
        session.add(VirtualPosition(strategy_id=atr.id, broker_account_id=parent.broker_account_id,
            symbol="SCNI", quantity=D(100), average_price=D(9)))
        net = session.scalar(select(AccountPosition).where(
            AccountPosition.broker_account_id == parent.broker_account_id, AccountPosition.symbol == "SCNI"))
        net.quantity = D(101)
        session.flush()
        own_exit = OrderRequest("momentum_warn_live-SCNI-close-own", request.broker_account_name,
            STRATEGY_CODE, "SCNI", "sell", "close", D(1), "MOMLIVE1_EXIT", {}, "limit", "day")
        store.prepare_order(session, episode.episode_id, own_exit, provider="schwab")
        with pytest.raises(Refusal, match="owned_quantity"):
            oversized = OrderRequest("oversized", request.broker_account_name, STRATEGY_CODE,
                "SCNI", "sell", "close", D(2), "MOMLIVE1_EXIT", {}, "limit", "day")
            store.prepare_order(session, episode.episode_id, oversized, provider="schwab")
        assert session.scalar(select(VirtualPosition.quantity).where(VirtualPosition.strategy_id == atr.id)) == 100


def test_cumulative_vwap_keeps_cash_not_order_price():
    factory, store, _, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        stamp = datetime(2026, 10, 14, 13, tzinfo=UTC)
        store.record(session, request, [ExecutionReport("partially_filled", request.client_order_id,
            broker_fill_id="partial1", filled_quantity=D(1), fill_price=D("3.0012"), reported_at=stamp, origin="broker")])
        final = store.record(session, request, [ExecutionReport("filled", request.client_order_id,
            broker_fill_id="partial2", filled_quantity=D(3), fill_price=D("3.01234567"), reported_at=stamp, origin="broker")])
        assert final["quantity"] == 3
        assert final["price"] == D("3.01234567")
        fills = session.scalars(select(Fill)).all()
        assert sum(store.fill_value(row) for row in fills) == D("9.03703701")
        assert abs(sum(row.quantity*row.price for row in fills)-D("9.03703701")) <= D(".00000001")


def test_unknown_transport_reject_does_not_terminalize_or_book_fill():
    factory, store, _, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        result = store.record(session, request, [ExecutionReport("rejected", request.client_order_id,
            reason="timeout", origin="client")])
        assert result["status"] == "pending" and result["quantity"] == 0
        assert not session.scalars(select(Fill)).all()


def test_earlier_trusted_execution_time_updates_durable_order_without_new_quantity():
    factory, store, _, episode, request = setup()
    later = datetime(2026, 10, 14, 13, 0, 30, tzinfo=UTC)
    earlier = datetime(2026, 10, 14, 13, 0, 20, tzinfo=UTC)
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        for stamp in (later, earlier):
            store.record(session, request, [ExecutionReport("filled", request.client_order_id,
                broker_order_id="own-entry", broker_fill_id="same-real-fill", filled_quantity=D(3),
                fill_price=D("3.01234567"), origin="broker", reported_at=stamp,
                metadata={"momlive_execution_time_source": "broker_execution_time"})])
        session.commit()
        snapshot = store.inspect_episode(session, episode.episode_id)
        assert snapshot["first_fill_ns"] == int(earlier.timestamp()*1e9)
        assert snapshot["quantity"] == 3 and snapshot["price"] == D("3.01234567")
        assert len(session.scalars(select(Fill)).all()) == 1


def test_foreign_report_id_rejected_without_fill():
    factory, store, _, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        with pytest.raises(Refusal, match="identity_mismatch"):
            store.record(session, request, [ExecutionReport("filled", "foreign", filled_quantity=D(3), fill_price=D(3))])
        assert not session.scalars(select(Fill)).all()
