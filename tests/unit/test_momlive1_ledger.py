from datetime import UTC, date, datetime
from decimal import Decimal as D
from uuid import uuid4

from sqlalchemy import select
import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import BrokerOrder, Fill
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.ledger import load_live_ledger
from project_mai_tai.services.control_plane import _render_momentum_ledger
from tests.unit.test_momlive1_persistence import setup

DAY = date(2026, 10, 14)


def filled_cycle(*, closed=True):
    factory, store, book, episode, request = setup()
    stamp = datetime(2026, 10, 14, 13, 53, 20, tzinfo=UTC)
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        store.record(session, request, [ExecutionReport("filled", request.client_order_id,
            broker_order_id="entry-own", filled_quantity=D(3), fill_price=D("3.99"),
            origin="broker", reported_at=stamp, metadata={"momlive_execution_time_source": "broker_execution_time"})])
        if closed:
            exit_request = OrderRequest("own-ocoexit", request.broker_account_name, STRATEGY_CODE,
                "SCNI", "sell", "close", D(3), "native own exit",
                {"episode_id": episode.episode_id, "exit_reason": "target_5pct"}, "limit", "day")
            store.prepare_order(session, episode.episode_id, exit_request, provider="schwab")
            store.record(session, exit_request, [ExecutionReport("filled", exit_request.client_order_id,
                side="sell", broker_order_id="own-child", filled_quantity=D(3), fill_price=D("4.19"),
                origin="broker", reported_at=stamp, metadata={"momlive_execution_time_source": "broker_execution_time"})])
            # A later duplicate close refusal cannot erase actual broker fills.
            refused = OrderRequest("later-refused", request.broker_account_name, STRATEGY_CODE,
                "SCNI", "sell", "close", D(3), "duplicate close", {}, "limit", "day")
            parent = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "own-ocoexit"))
            parent.payload = {**parent.payload, "limit_price": "9.99"}
            session.add(BrokerOrder(intent_id=parent.intent_id, strategy_id=parent.strategy_id,
                broker_account_id=parent.broker_account_id, client_order_id=refused.client_order_id,
                symbol="SCNI", side="sell", quantity=D(3), status="rejected", order_type="limit",
                time_in_force="day", payload={"episode_id": episode.episode_id, "limit_price": "9.99"}))
        session.commit()
    return factory


def test_native_exit_same_timestamp_cycle_uses_fill_vwap_not_order_limit():
    result = load_live_ledger(filled_cycle(), DAY)
    assert result["status"] == "READABLE" and D(result["daily_pnl"]) == D(".60")
    assert result["positions"] == [] and len(result["closed_today"]) == 1
    row = result["closed_today"][0]
    assert D(row["entry_price"]) == D("3.99") and D(row["exit_price"]) == D("4.19")
    assert D(row["pnl"]) == D(".60") and row["exit_reason"] == "target_5pct"


def test_partial_held_is_not_a_completed_trade_or_realized_day_pnl():
    result = load_live_ledger(filled_cycle(closed=False), DAY)
    assert result["closed_today"] == [] and result["daily_pnl"] == "0"
    assert D(result["positions"][0]["remaining_quantity"]) == 3


def test_empty_readable_fill_ledger_is_zero_not_missing_unknown():
    factory, *_ = setup()
    result = load_live_ledger(factory, DAY)
    assert result["status"] == "READABLE" and result["daily_pnl"] == "0" and result["fills_read"] == 0


def test_missing_broker_provenance_raises_unknown_not_healthy_zero():
    factory = filled_cycle()
    with factory() as session:
        row = session.scalars(select(Fill)).first()
        row.payload = {**row.payload, "momlive_fill_source": "UNKNOWN"}
        session.commit()
    with pytest.raises(Refusal, match="provenance_UNKNOWN"):
        load_live_ledger(factory, DAY)


def test_client_claimed_fill_cannot_enter_live_broker_ledger():
    factory, store, book, episode, request = setup()
    with factory() as session:
        store.prepare_order(session, episode.episode_id, request, provider="schwab")
        with pytest.raises(Refusal, match="broker_provenance"):
            store.record(session, request, [ExecutionReport("filled", request.client_order_id,
                filled_quantity=D(3), fill_price=D(3), origin="client")])
        assert session.scalars(select(Fill)).all() == []


def test_live_render_explicit_broker_trades_and_unreadable_unknown_total():
    rendered = _render_momentum_ledger(load_live_ledger(filled_cycle(), DAY), title="LIVE (real orders)",
                                     method="LIVE", other_rows=[], paper=False)
    assert "LIVE (real orders)" in rendered and "1 completed broker trades" in rendered
    assert "completed paper trades" not in rendered
    unreadable = _render_momentum_ledger({"status": "UNKNOWN"}, title="LIVE (real orders)",
                                        method="LIVE", other_rows=[], paper=False)
    assert "UNKNOWN. No total calculated." in unreadable and "$+0.00" not in unreadable


def mixed_first_time_ledger(*, webull_closed):
    factory = filled_cycle()
    from project_mai_tai.momentum_live.persistence import LivePersistence
    from project_mai_tai.oms.store import OmsStore
    from tests.unit.test_momlive1_admission import ledger, signal
    store = LivePersistence(OmsStore())
    book = ledger()
    episode, _ = book.claim(signal("DLXY"), now_ns=10)
    # A distinct owned episode/account: no symbol FIFO or account projection.
    identity = str(uuid4())
    data = {**book.snapshot()[0], "episode_id": identity}
    stamp = datetime(2026, 10, 14, 14, tzinfo=UTC)
    with factory() as session:
        store.claim(session, episode=data, account="live:orb", provider="webull", signal={})
        buy = OrderRequest("webull-unknown-first", "live:orb", STRATEGY_CODE,
            "DLXY", "buy", "open", D(2), "sealed", {"episode_id": identity}, "limit", "day")
        store.prepare_order(session, identity, buy, provider="webull")
        store.record(session, buy, [ExecutionReport("filled", buy.client_order_id,
            broker_order_id="webull-entry", filled_quantity=D(2), fill_price=D(10), origin="broker",
            reported_at=stamp, metadata={"momlive_first_execution_time_status": "UNKNOWN"})])
        buy_fill = session.scalar(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id).where(
            BrokerOrder.client_order_id == buy.client_order_id))
        # Must neither filter on this operational timestamp nor borrow it as FIRST.
        buy_fill.filled_at = datetime(2099, 1, 1, tzinfo=UTC)
        if webull_closed:
            sell = OrderRequest("webull-own-exit", "live:orb", STRATEGY_CODE,
                "DLXY", "sell", "close", D(2), "target", {"episode_id": identity}, "limit", "day")
            store.prepare_order(session, identity, sell, provider="webull")
            store.record(session, sell, [ExecutionReport("filled", sell.client_order_id,
                broker_order_id="webull-exit", filled_quantity=D(2), fill_price=D(11), origin="broker",
                reported_at=stamp, metadata={"momlive_execution_time_source": "broker_execution_time"})])
        session.commit()
    return factory


def test_unknown_webull_first_time_keeps_cash_cycle_and_known_schwab_pnl_without_fallback():
    result = load_live_ledger(mixed_first_time_ledger(webull_closed=True), DAY)
    assert result["status"] == "READABLE" and len(result["closed_today"]) == 2
    rows = {row["account_name"]: row for row in result["closed_today"]}
    webull = rows["live:orb"]
    assert webull["entry_time"] == "UNKNOWN" and webull["entry_time_status"] == "UNKNOWN"
    assert D(webull["quantity"]) == 2 and D(webull["entry_price"]) == 10
    assert D(webull["exit_price"]) == 11 and D(webull["pnl"]) == 2
    assert D(webull["pnl_pct"]) == 10
    assert D(rows["live:schwab_1m_v2"]["pnl"]) == D(".60")
    assert rows["live:schwab_1m_v2"]["entry_time"] != "UNKNOWN"
    assert D(result["daily_pnl"]) == D("2.60") and result["unknown_entry_time_episodes"] == 1
    rendered = _render_momentum_ledger(result, title="LIVE (real orders)", method="LIVE",
                                     other_rows=[], paper=False)
    assert "SCNI" in rendered and "DLXY" in rendered and "UNKNOWN" in rendered
    assert "2 completed broker trades" in rendered and "Day total: $+2.60" in rendered


def test_unknown_webull_first_time_keeps_owned_held_quantity_without_poll_time_or_pnl_invention():
    result = load_live_ledger(mixed_first_time_ledger(webull_closed=False), DAY)
    assert result["status"] == "READABLE" and len(result["positions"]) == 1
    held = result["positions"][0]
    assert held["entry_time"] == "UNKNOWN" and D(held["remaining_quantity"]) == 2
    assert D(held["entry_price"]) == 10 and D(result["daily_pnl"]) == D(".60")
    assert len(result["closed_today"]) == 1 and result["closed_today"][0]["ticker"] == "SCNI"
