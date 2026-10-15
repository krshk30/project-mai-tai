"""Recorded-fill display controls; no provider access or execution-clock changes."""
from datetime import UTC, date, datetime
from decimal import Decimal as D
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent
from project_mai_tai.momentum_live.admission import STRATEGY_CODE
from project_mai_tai.momentum_live.ledger import _recorded_fill_time, load_live_ledger
from project_mai_tai.services import control_plane as cp
from tests.unit.test_mompage1 import make_app

DAY = date(2026, 10, 15)


def cycle(factory, symbol, buys, exit_at, exit_price, *, provider="webull", first=None):
    identity = uuid4()
    with factory.begin() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == STRATEGY_CODE))
        if strategy is None:
            strategy = Strategy(code=STRATEGY_CODE, name="Controlled LIVE", execution_mode="live")
            session.add(strategy)
            session.flush()
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == f"live:{provider}"))
        if account is None:
            account = BrokerAccount(name=f"live:{provider}", provider=provider, environment="production")
            session.add(account)
            session.flush()
        qty = sum((D(q) for _, q, _ in buys), D(0))
        parent = TradeIntent(id=identity, strategy_id=strategy.id, broker_account_id=account.id,
            symbol=symbol, side="buy", intent_type="open", quantity=qty, reason="controlled", status="filled",
            payload={"momlive_parent": True, "momlive_episode": {"episode_id": str(identity)}})
        session.add(parent)
        session.flush()
        for side in ("buy", "sell"):
            metadata = {"episode_id": str(identity)}
            if side == "sell":
                metadata["momlive_last_exit_fill_ns"] = str(int(exit_at.timestamp()*1e9))
            elif first is not None:
                metadata["momlive_first_fill_ns"] = str(int(first.timestamp()*1e9))
            order = BrokerOrder(intent_id=identity, strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id=f"controlled-{identity}-{side}", broker_order_id=f"native-{identity}-{side}",
                symbol=symbol, side=side, quantity=qty, status="filled", order_type="limit",
                time_in_force="day", payload=metadata)
            session.add(order)
            session.flush()
            parts = buys if side == "buy" else [(exit_at, qty, exit_price)]
            for index, (stamp, amount, price) in enumerate(parts):
                session.add(Fill(id=uuid4(), order_id=order.id,
                    strategy_id=strategy.id, broker_account_id=account.id,
                    broker_fill_id=f"{identity}-{side}-{index}", symbol=symbol, side=side,
                    quantity=D(amount), price=D(price), filled_at=stamp,
                    payload={"metadata": metadata, "momlive_fill_source": "broker"}))
    return str(identity)


def bbci_mlec(factory):
    # Amounts/times from retained BBCI export and reviewer-supplied MLEC control.
    cycle(factory, "BBCI", [(datetime(2026, 10, 15, 13, 31, 10, 419000, UTC), "25", "3.68")],
          datetime(2026, 10, 15, 13, 32, 26, 520000, UTC), "3.87")
    cycle(factory, "MLEC", [(datetime(2026, 10, 15, 15, 56, 19, 593000, UTC), "35", "2.76")],
          datetime(2026, 10, 15, 15, 56, 25, 487000, UTC), "2.241")


@pytest.mark.parametrize("route", ["/bot/momentum-30", "/bot/momentum-60"])
def test_actual_boards_recorded_bbci_mlec_times_and_existing_api_contract(monkeypatch, route):
    app, factory = make_app(monkeypatch, records=False)
    bbci_mlec(factory)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 15, 20, tzinfo=UTC))
    captured = []
    original = cp._render_bot_detail_page

    def render(data, strategy_code, **kwargs):
        captured.append(cp._find_bot_view(data, strategy_code)["momentum_live"])
        return original(data, strategy_code, **kwargs)

    monkeypatch.setattr(cp, "_render_bot_detail_page", render)
    with TestClient(app) as client:
        before = client.get("/api/bots").json()
        response = client.get(route)
        assert response.status_code == 200
        live_html = response.text.split("<h3>LIVE (real orders)</h3>", 1)[1]
        assert "09:31:10 AM ET" in live_html and "11:56:19 AM ET" in live_html
        assert "BBCI" in live_html and "MLEC" in live_html
        assert "Day total: $-13.41" in live_html and "2 completed broker trades" in live_html
        assert "$+4.75" in live_html and "$-18.16" in live_html
        after = client.get("/api/bots").json()
    rows = {row["symbol"]: row for row in captured[0]["closed_today"]}
    assert rows["BBCI"]["entry_time"] == "2026-10-15T09:31:10.419000-04:00"
    assert rows["MLEC"]["entry_time"] == "2026-10-15T11:56:19.593000-04:00"
    assert all(row["entry_time_status"] == "RECORDED_FILL_TIME" for row in rows.values())
    assert all(row["entry_time_source"] == "owned_buy_fill.filled_at" for row in rows.values())
    assert all(row["first_execution_time_status"] == "UNKNOWN" for row in rows.values())
    assert D(captured[0]["daily_pnl"]) == D("-13.415")
    # Existing HTTP API is the cached bot snapshot, not the page-local LIVE ledger.
    assert before["bots"] == after["bots"]
    assert all("momentum_live" not in row for row in after["bots"])


@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("hour", [8, 14, 21])
def test_earliest_own_buy_recorded_time_across_sessions_and_known_first(monkeypatch, provider, hour):
    _, factory = make_app(monkeypatch, records=False)
    early = datetime(2026, 10, 14, hour, 1, tzinfo=UTC)
    late = early.replace(second=30)
    identity = cycle(factory, "SAME", [(late, "2", "4"), (early, "3", "4")],
                     late.replace(minute=2), "5", provider=provider, first=early)
    other = cycle(factory, "SAME", [(late, "1", "10")], late.replace(minute=3), "9", provider=provider)
    result = load_live_ledger(factory, date(2026, 10, 14))
    rows = {row["episode_id"]: row for row in result["closed_today"]}
    assert datetime.fromisoformat(rows[identity]["entry_time"]).astimezone(UTC) == early
    assert datetime.fromisoformat(rows[other]["entry_time"]).astimezone(UTC) == late
    assert rows[identity]["first_execution_time_status"] == "BROKER_EXECUTION"
    assert rows[other]["first_execution_time_status"] == "UNKNOWN"
    assert D(rows[identity]["pnl"]) == 5 and D(rows[other]["pnl"]) == -1


@pytest.mark.parametrize("stamp", [datetime(2099, 1, 1, tzinfo=UTC), datetime(1969, 1, 1, tzinfo=UTC)])
def test_invalid_recorded_time_unknown_does_not_borrow_known_first(monkeypatch, stamp):
    _, factory = make_app(monkeypatch, records=False)
    first = datetime(2026, 10, 15, 13, tzinfo=UTC)
    cycle(factory, "INVALID", [(first, "1", "4")], first.replace(minute=2), "5", first=first)
    with factory.begin() as session:
        fill = session.scalar(select(Fill).where(Fill.side == "buy"))
        fill.filled_at = stamp
    row = load_live_ledger(factory, DAY)["closed_today"][0]
    assert row["entry_time"] == "UNKNOWN" and row["entry_time_status"] == "UNKNOWN"
    assert row["first_execution_time_status"] == "BROKER_EXECUTION"
    assert D(row["pnl"]) == 1


@pytest.mark.parametrize("value", [None, "not-a-timestamp", 0])
def test_missing_or_invalid_timestamp_is_unknown_at_validation_boundary(value):
    assert _recorded_fill_time(value, datetime(2026, 10, 15, tzinfo=UTC)) is None


def test_recorded_entry_crosses_et_day_without_changing_exit_day_selection(monkeypatch):
    _, factory = make_app(monkeypatch, records=False)
    cycle(factory, "OVERNIGHT", [(datetime(2026, 10, 15, 3, 59, tzinfo=UTC), "1", "4")],
          datetime(2026, 10, 15, 4, 1, tzinfo=UTC), "5")
    row = load_live_ledger(factory, DAY)["closed_today"][0]
    assert row["entry_time"] == "2026-10-14T23:59:00-04:00"
    assert row["exit_time"] == "2026-10-15T00:01:00-04:00"
    assert not load_live_ledger(factory, date(2026, 10, 14))["closed_today"]


@pytest.mark.parametrize("hour,offset", [(5, "-04:00"), (6, "-05:00")])
def test_recorded_time_preserves_dst_fold_offset(monkeypatch, hour, offset):
    _, factory = make_app(monkeypatch, records=False)
    stamp = datetime(2025, 11, 2, hour, 30, tzinfo=UTC)
    cycle(factory, "DST", [(stamp, "1", "4")], stamp.replace(minute=31), "5")
    row = load_live_ledger(factory, date(2025, 11, 2))["closed_today"][0]
    assert row["entry_time"] == f"2025-11-02T01:30:00{offset}"


def test_one_invalid_buy_time_keeps_episode_unknown_and_reader_never_updates_rows(monkeypatch):
    _, factory = make_app(monkeypatch, records=False)
    stamp = datetime(2026, 10, 15, 13, tzinfo=UTC)
    cycle(factory, "MIXED", [(stamp, "1", "4"), (datetime(2099, 1, 1, tzinfo=UTC), "1", "4")],
          stamp.replace(minute=2), "5")

    def stored():
        with factory() as session:
            orders = [(str(row.id), row.status, row.payload) for row in session.scalars(select(BrokerOrder))]
            fills = [(str(row.id), row.quantity, row.price, row.filled_at, row.payload)
                     for row in session.scalars(select(Fill))]
            return orders, fills

    before = stored()
    result = load_live_ledger(factory, DAY)
    assert result["closed_today"][0]["entry_time"] == "UNKNOWN"
    assert result["unknown_entry_time_episodes"] == 1
    assert D(result["daily_pnl"]) == 2
    assert stored() == before
