"""Controlled ledger/HTTP isolation contracts; authored, not a CRMT native replay."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sql_event, insert, select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.ledger import load_live_ledger
from project_mai_tai.services import control_plane as cp
from tests.unit.test_momlive1_page_entry_time import cycle
from tests.unit.test_mompage1 import make_app
from tests.unit.test_mompage4_live_first import PageRedis, owner


DAY = date(2026, 10, 20)
ENTRY = datetime(2026, 10, 20, 19, 5, 28, tzinfo=UTC)
EXIT = datetime(2026, 10, 20, 19, 10, 28, tzinfo=UTC)


def seeded(monkeypatch, provider="schwab"):
    app, factory = make_app(monkeypatch, records=False)
    good = cycle(factory, "AIXI", [(ENTRY, "2", "3")], EXIT, "4", first=ENTRY)
    opened = cycle(factory, "GMEX", [(ENTRY, "5", "2")], EXIT, "3", first=ENTRY)
    bad = cycle(factory, "CRMT", [(ENTRY, "111", "1")], EXIT, "0.9151",
                provider=provider, first=ENTRY)
    with factory.begin() as session:
        # The open sibling has no SELL; its actual BUY remains readable.
        sell = session.scalar(select(BrokerOrder).where(
            BrokerOrder.symbol == "GMEX", BrokerOrder.side == "sell"))
        for fill in session.scalars(select(Fill).where(Fill.order_id == sell.id)):
            session.delete(fill)
        session.delete(sell)
        for account in session.scalars(select(BrokerAccount)):
            account.name = {"live:webull": "live:orb",
                            "live:schwab": "live:schwab_1m_v2"}[account.name]
    monkeypatch.setattr(cp, "utcnow", lambda: EXIT+timedelta(minutes=1))
    return app, factory, good, opened, bad


def damage(factory, kind):
    with factory.begin() as session:
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "CRMT")).all()
        for order in orders:
            fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
            if kind == "identity":
                order.payload = {**order.payload, "episode_id": "not-an-episode-uuid"}
                for fill in fills:
                    fill.payload = {**fill.payload, "metadata": deepcopy(order.payload)}
            if order.side != "sell":
                continue
            if kind == "missing_exit":
                order.payload = {k: v for k, v in order.payload.items() if k != "momlive_last_exit_fill_ns"}
            elif kind in {"malformed_time", "nonpositive_time", "future_time", "boolean_time"}:
                value = {"malformed_time": "not-nanoseconds", "nonpositive_time": "0",
                         "future_time": str(int(datetime(2099, 1, 1, tzinfo=UTC).timestamp()*1e9)),
                         "boolean_time": True}[kind]
                order.payload = {**order.payload, "momlive_last_exit_fill_ns": value}
            elif kind == "provenance":
                for fill in fills:
                    fill.payload = {**fill.payload, "momlive_fill_source": "client"}
            elif kind == "value":
                for fill in fills:
                    fill.payload = {**fill.payload, "momlive_execution_value_exact": "-1"}
            elif kind == "binding":
                # Both physical rows agree, but two symbols cannot share an episode.
                order.symbol = "ALIEN"
                for fill in fills:
                    fill.symbol = "ALIEN"
            elif kind == "side":
                for fill in fills:
                    fill.side = "other"
            elif kind == "balance":
                for fill in fills:
                    fill.quantity = Decimal(112)


def stored(factory):
    with factory() as session:
        return deepcopy([
            (model.__name__, tuple(getattr(row, column.name) for column in model.__table__.columns))
            for model in (BrokerOrder, Fill)
            for row in session.scalars(select(model).order_by(model.id))
        ])


def assert_partial(result, good, opened):
    assert result["status"] == "PARTIAL" and result["daily_pnl_partial"] is True
    assert Decimal(result["daily_pnl"]) == Decimal(2)
    assert [row["episode_id"] for row in result["closed_today"]] == [good]
    assert [row["episode_id"] for row in result["positions"]] == [opened]
    assert Decimal(result["positions"][0]["remaining_quantity"]) == 5
    assert result["unknown_episodes"]
    for row in result["unknown_episodes"]:
        assert row["reason"] and row["exit_time"] == row["pnl"] == "UNKNOWN"
    # Public JSON data, not a cloned ledger oracle or a new HTTP endpoint.
    assert json.loads(json.dumps(result))["status"] == "PARTIAL"


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_missing_exit_stamp_is_episode_local_and_preserves_readable_open_and_closed(monkeypatch, provider):
    _, factory, good, opened, bad = seeded(monkeypatch, provider)
    damage(factory, "missing_exit")
    before = stored(factory)
    statements = []
    engine = factory.kw["bind"]

    def read_only(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement

    sql_event.listen(engine, "before_cursor_execute", read_only)
    try:
        result = load_live_ledger(factory, DAY)
    finally:
        sql_event.remove(engine, "before_cursor_execute", read_only)
    assert statements and stored(factory) == before
    assert_partial(result, good, opened)
    row, = result["unknown_episodes"]
    assert row["episode_id"] == bad and row["symbol"] == "CRMT"
    assert row["account_name"] == ("live:schwab_1m_v2" if provider == "schwab" else "live:orb")
    assert row["reason"] == "live_fill_execution_time_UNKNOWN"


@pytest.mark.parametrize("kind", ["identity", "provenance", "value", "binding", "side", "balance",
                                  "malformed_time", "nonpositive_time", "future_time", "boolean_time"])
def test_bad_group_never_discards_good_siblings_or_contributes_pnl(monkeypatch, kind):
    _, factory, good, opened, _ = seeded(monkeypatch)
    damage(factory, kind)
    before = stored(factory)
    assert_partial(load_live_ledger(factory, DAY), good, opened)
    assert stored(factory) == before


def test_malformed_sell_metadata_quarantines_linked_valid_buy_episode_not_false_open(monkeypatch):
    _, factory, good, opened, bad = seeded(monkeypatch)
    with factory.begin() as session:
        sell = session.scalar(select(Fill).where(Fill.symbol == "CRMT", Fill.side == "sell"))
        order = session.get(BrokerOrder, sell.order_id)
        assert order.payload["episode_id"] == bad
        sell.payload = {**sell.payload, "metadata": {**sell.payload["metadata"],
                                                     "episode_id": "malformed"}}
    before = stored(factory)
    result = load_live_ledger(factory, DAY)
    assert_partial(result, good, opened)
    assert {row["episode_id"] for row in result["unknown_episodes"]} == {bad}
    assert bad not in {row["episode_id"] for row in result["positions"]+result["closed_today"]}
    assert stored(factory) == before


def test_conflicting_valid_fill_and_order_episode_ids_quarantine_both_not_unrelated_rows(monkeypatch):
    _, factory, good, opened, bad = seeded(monkeypatch)
    other = cycle(factory, "XCH", [(ENTRY, "3", "2")], EXIT, "4", first=ENTRY)
    with factory.begin() as session:
        sell = session.scalar(select(Fill).where(Fill.symbol == "CRMT", Fill.side == "sell"))
        assert session.get(BrokerOrder, sell.order_id).payload["episode_id"] == bad
        sell.payload = {**sell.payload, "metadata": {**sell.payload["metadata"], "episode_id": other}}
    before = stored(factory)
    result = load_live_ledger(factory, DAY)
    assert_partial(result, good, opened)
    assert {row["episode_id"] for row in result["unknown_episodes"]} == {bad, other}
    assert all(row["episode_id"] not in {bad, other} for row in result["positions"]+result["closed_today"])
    assert stored(factory) == before


def test_partial_actual_http_page_keeps_good_rows_and_explicit_unknown_not_flat(monkeypatch):
    app, factory, good, opened, bad = seeded(monkeypatch)
    damage(factory, "missing_exit")
    observed_ns = int((EXIT+timedelta(minutes=1)).timestamp()*1e9)
    monkeypatch.setattr(cp.time, "time_ns", lambda: observed_ns)
    redis = PageRedis(owner(observed_ns=observed_ns))
    captured = []
    render = cp._render_momentum_page

    def capture(data, **kwargs):
        captured.append(deepcopy(kwargs["live"]))
        return render(data, **kwargs)

    monkeypatch.setattr(cp, "_render_momentum_page", capture)
    before = stored(factory)
    with TestClient(app) as client:
        app.state.repository.redis = redis
        api_before = client.get("/api/bots").json()
        response = client.get("/bot/momentum")
        api_after = client.get("/api/bots").json()
    assert response.status_code == 200 and response.url.path == "/bot/momentum"
    assert len(captured) == 1
    assert_partial(captured[0], good, opened)
    html = response.text
    assert "partial" in html.lower() and "$+2.00" in html
    assert all(symbol in html for symbol in ("CRMT", "AIXI", "GMEX"))
    assert "live:schwab_1m_v2" in html and "live_fill_execution_time_UNKNOWN" in html
    assert "UNKNOWN" in html and "No open positions" not in html
    assert api_before["bots"] == api_after["bots"] and stored(factory) == before


def test_unknown_only_does_not_present_a_healthy_zero_or_flat(monkeypatch):
    _, factory = make_app(monkeypatch, records=False)
    cycle(factory, "CRMT", [(ENTRY, "111", "1")], EXIT, "0.9151", first=ENTRY)
    damage(factory, "missing_exit")
    result = load_live_ledger(factory, DAY)
    assert result["status"] == "PARTIAL" and result["daily_pnl_partial"] is True
    assert result["positions"] == result["closed_today"] == []
    assert result["unknown_episodes"] and Decimal(result["daily_pnl"]) == 0
    html = cp._render_momentum_page({"bots": [], "recent_orders": []}, live=result,
        warnings=[], owner={"status": "UNKNOWN"}, metrics={"status": "UNKNOWN"})
    assert "CRMT" in html and "partial" in html.lower() and "UNKNOWN" in html
    assert "No open positions" not in html


def test_history_8192_boundary_remains_global_not_partial(monkeypatch):
    _, factory = make_app(monkeypatch, records=False)
    cycle(factory, "BOUND", [(ENTRY, "1", "1")], EXIT, "1", first=ENTRY)
    with factory.begin() as session:
        sell = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell"))
        for row in session.scalars(select(Fill).where(Fill.order_id == sell.id)):
            session.delete(row)
        session.delete(sell)
        original = session.scalar(select(Fill))
        template = {column.name: getattr(original, column.name) for column in Fill.__table__.columns
                    if column.name not in {"id", "broker_fill_id"}}
        extra = [{**template, "id": uuid4(), "broker_fill_id": f"controlled-bound-{i}"}
                 for i in range(8191)]
        session.execute(insert(Fill), extra)
    assert load_live_ledger(factory, DAY)["fills_read"] == 8192
    with factory.begin() as session:
        session.execute(insert(Fill), [{**template, "id": uuid4(), "broker_fill_id": "controlled-over-bound"}])
    with pytest.raises(Refusal, match="live_fill_ledger_history_limit_UNKNOWN"):
        load_live_ledger(factory, DAY)
