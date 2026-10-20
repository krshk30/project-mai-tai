"""O17 source controls: local SQLite/FakeRedis only, no broker/provider work."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, Strategy
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import (
    FakeLegacyClient, FakeRedis, build_test_session_factory, make_streams,
)


def case(monkeypatch, *, all_on=False, hour=14, bad_event=None, bad_rows=False):
    now = datetime(2026, 10, 19, hour, 30, tzinfo=UTC)
    monkeypatch.setattr(cp, "utcnow", lambda: now)
    factory = build_test_session_factory()
    settings = Settings(_env_file=None, redis_stream_prefix="test", trade_coach_enabled=all_on,
        dashboard_trade_forensics_enabled=True, dashboard_trade_forensics_cache_ttl_seconds=0,
        schwab_token_refresher_enabled=False, bot_control_enabled=all_on,
        strategy_schwab_1m_v2_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_country_route_enabled=all_on,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        orb_enabled=True, orb_live_schwab_orders_enabled=True,
        orb_country_routing_enabled=all_on, orb_webull_account_name="live:orb")
    ids = {}
    with factory() as session:
        accounts = [BrokerAccount(name=name, provider=provider, environment="test", is_active=True)
                    for name, provider in (("paper:macd_30s", "alpaca"),
                        ("live:schwab_1m_v2", "schwab"), ("live:orb", "webull"))]
        strategies = [Strategy(code=code, name=code, is_enabled=True, metadata_json={})
                      for code in ("macd_30s", "schwab_1m_v2", "orb_schwab")]
        session.add_all([*accounts, *strategies])
        session.flush()
        for index, symbol in enumerate(("GOOD", "YMAT", "ORBGOOD")):
            strategy = strategies[index]
            account = accounts[(0, 2, 1)[index]]
            order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id=f"controlled-{symbol}-buy", broker_order_id=f"native-{symbol}",
                symbol=symbol, side="buy", quantity=Decimal(10), order_type="LIMIT",
                time_in_force="day", status="filled", payload={},
                submitted_at=now-timedelta(seconds=30), updated_at=now)
            session.add(order)
            session.flush()
            fill = Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                broker_fill_id=f"native-{symbol}:10", symbol=symbol, side="buy", quantity=Decimal(10),
                price=Decimal("1.86"), filled_at=now-timedelta(seconds=20), payload={})
            session.add(fill)
            session.flush()
            ids[symbol] = (order.id, fill.id)
            if symbol == "YMAT" and bad_event is not None:
                payload = ({"fill_price": "1.8602", "private_payload": "DO_NOT_LOG_O17"}
                           if bad_event == "string_price" else ["invalid event object", "DO_NOT_LOG_O17"])
                row = BrokerOrderEvent(order_id=order.id, event_type="filled", event_source="broker",
                    event_at=now, payload=payload)
                session.add(row)
                session.flush()
                ids["event"] = row.id
        if bad_rows:
            order = BrokerOrder(strategy_id=strategies[0].id, broker_account_id=accounts[0].id,
                client_order_id="controlled-bad-order", symbol="BADORDER", side="buy",
                quantity=Decimal("8.12345678"), order_type="LIMIT", time_in_force="day",
                status="filled", payload=["invalid object"], submitted_at=now, updated_at=now)
            session.add(order)
            session.flush()
            fill = Fill(order_id=order.id, strategy_id=strategies[0].id, broker_account_id=accounts[0].id,
                broker_fill_id="controlled-bad-fill", symbol="BADFILL", side="buy", quantity=Decimal(1),
                price=Decimal("9.87654321"), filled_at=now, payload={})
            session.add(fill)
            session.flush()
            ids["bad_order"], ids["bad_fill"] = order.id, fill.id
        session.commit()
    app = cp.build_app(settings=settings, session_factory=factory,
        redis_client=FakeRedis(make_streams("test")), legacy_client=FakeLegacyClient())
    app.state.repository = cp.ControlPlaneRepository(settings, session_factory=factory,
        redis=FakeRedis(make_streams("test")), legacy_client=FakeLegacyClient())
    return app, factory, ids, now


@pytest.mark.parametrize("all_on", [False, True])
@pytest.mark.parametrize("hour", [8, 11, 14, 21], ids=["PM0430", "PM0730", "RTH", "after1600"])
@pytest.mark.parametrize("bad_event", ["string_price", "object_shape"])
def test_bad_event_keeps_orders_fills_and_visible_warning_on_pages_and_api(
        monkeypatch, caplog, all_on, hour, bad_event):
    app, factory, ids, now = case(monkeypatch, all_on=all_on, hour=hour, bad_event=bad_event)
    warnings = {}
    with factory() as session:
        kwargs = dict(session=session, strategy_code="schwab_1m_v2", broker_account_name="live:orb",
            window_start=now-timedelta(hours=1), window_end=now+timedelta(seconds=1), row_warnings=warnings)
        assert len(app.state.repository._load_trade_forensics_orders(**kwargs)) == 1
        assert len(app.state.repository._load_trade_forensics_fills(**kwargs)) == 1
    assert set(warnings) == {("broker_order_events", str(ids["event"]))}
    with TestClient(app) as client:
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        data = overview.json()
        assert {row["symbol"] for row in data["recent_orders"]} == {"GOOD", "YMAT", "ORBGOOD"}
        assert {row["symbol"] for row in data["recent_fills"]} == {"GOOD", "YMAT", "ORBGOOD"}
        assert data["counts"]["skipped_database_rows"] == 1
        assert data["status"] == "degraded"
        assert data["row_conversion_warnings"][0]["row_id"] == str(ids["event"])
        assert not any(error.startswith("database:") for error in data["errors"])
        bots = client.get("/api/bots").json()
        assert bots["skipped_database_rows"] == 1 and bots["status"] == "degraded"
        assert bots["row_conversion_warnings"][0]["row_id"] == str(ids["event"])
        for path in ("/", "/scanner/dashboard", "/bot/30s", "/bot/1m-schwab-v2", "/bot/orb"):
            page = client.get(path)
            assert page.status_code == 200
            assert "Database row warning: skipped 1 malformed rows" in page.text
            assert str(ids["event"]) in page.text
    assert "CONTROL-PLANE-ROW-SKIPPED" in caplog.text and str(ids["event"]) in caplog.text
    assert "DO_NOT_LOG_O17" not in caplog.text


def test_historical_forensics_only_warning_propagates_to_bot_api_and_page(monkeypatch):
    app, factory, ids, now = case(monkeypatch, all_on=True)
    with factory() as session:
        row = BrokerOrderEvent(order_id=ids["GOOD"][0], event_type="filled", event_source="broker",
            event_at=now-timedelta(days=1), payload={"fill_price": "1.8602"})
        session.add(row)
        session.flush()
        ident = str(row.id)
        session.commit()
    with TestClient(app) as client:
        assert client.get("/api/overview").json()["row_conversion_warnings"] == []
        bots = client.get("/api/bots").json()
        assert bots["status"] == "degraded" and bots["skipped_database_rows"] == 1
        assert bots["row_conversion_warnings"][0]["row_id"] == ident
        single = client.get("/bot").json()
        assert single["skipped_database_rows"] == 1
        page = client.get("/bot/30s")
        assert page.status_code == 200 and ident in page.text
        assert "Database row warning" in page.text


def test_bad_order_and_fill_conversion_are_isolated_in_dashboard_and_forensics(monkeypatch):
    app, factory, ids, now = case(monkeypatch, all_on=True, bad_rows=True)
    original = cp._decimal_str
    def fail_only_controlled_values(value):
        if value in {Decimal("8.12345678"), Decimal("9.87654321")}:
            raise ValueError("DO_NOT_LOG_O17")
        return original(value)
    monkeypatch.setattr(cp, "_decimal_str", fail_only_controlled_values)
    repository = app.state.repository
    for lightweight in (False, True):
        result = repository._load_database_state(lightweight=lightweight)
        assert {row["symbol"] for row in result["recent_orders"]} == {"GOOD", "YMAT", "ORBGOOD"}
        assert {row["symbol"] for row in result["recent_fills"]} == {"GOOD", "YMAT", "ORBGOOD"}
        assert result["counts"]["skipped_database_rows"] == 2
        assert {row["row_id"] for row in result["row_conversion_warnings"]} == {
            str(ids["bad_order"]), str(ids["bad_fill"])}
    warnings = {}
    with factory() as session:
        kwargs = dict(session=session, strategy_code="macd_30s", broker_account_name="paper:macd_30s",
                      window_start=now-timedelta(hours=1), window_end=now+timedelta(seconds=1),
                      row_warnings=warnings)
        orders = repository._load_trade_forensics_orders(**kwargs)
        fills = repository._load_trade_forensics_fills(**kwargs)
    assert [row["symbol"] for row in orders] == ["GOOD"]
    assert [row["symbol"] for row in fills] == ["GOOD"]
    assert set(warnings) == {("broker_orders", str(ids["bad_order"])), ("fills", str(ids["bad_fill"]))}


@pytest.mark.parametrize("location", ["query", "conversion"])
def test_database_failures_remain_database_errors_not_skipped_rows(monkeypatch, location):
    app, factory, _, now = case(monkeypatch, all_on=True)
    if location == "query":
        def fail_query(_conn, _cursor, statement, _parameters, _context, _many):
            if "FROM fills" in statement:
                raise SQLAlchemyError("CONTROLLED_DATABASE_FAILURE")
        engine = factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", fail_query)
    else:
        def fail_conversion(_value):
            raise SQLAlchemyError("CONTROLLED_DATABASE_FAILURE")
        monkeypatch.setattr(cp, "_decimal_str", fail_conversion)
    try:
        result = app.state.repository._load_database_state()
        assert any(error.startswith("database:") for error in result["errors"])
        assert not result["row_conversion_warnings"] and result["counts"]["skipped_database_rows"] == 0
        with factory() as session, pytest.raises(SQLAlchemyError):
            app.state.repository._load_trade_forensics_fills(session=session,
                strategy_code="macd_30s", broker_account_name="paper:macd_30s",
                window_start=now-timedelta(hours=1), window_end=now+timedelta(seconds=1))
    finally:
        if location == "query":
            event.remove(engine, "before_cursor_execute", fail_query)


@pytest.mark.parametrize("all_on", [False, True])
def test_clean_rows_have_no_warning_or_changed_projection(monkeypatch, all_on):
    app, _, _, _ = case(monkeypatch, all_on=all_on)
    result = app.state.repository._load_database_state()
    assert not result["row_conversion_warnings"] and not result["errors"]
    assert result["counts"]["skipped_database_rows"] == 0
    assert len(result["recent_orders"]) == len(result["recent_fills"]) == 3
    assert {row["price"] for row in result["recent_fills"]} == {"1.86"}
    assert cp._render_database_row_warnings(result) == ""


def test_row_warning_escapes_identifiers_and_discloses_truncation():
    warnings = [{"table": "fills", "row_id": "<script>bad</script>", "error_type": "ValueError"}]
    html = cp._render_database_row_warnings({"row_conversion_warnings": warnings * 21})
    assert "skipped 21 malformed rows" in html and "1 additional row IDs" in html
    assert "<script>bad</script>" not in html and "&lt;script&gt;bad&lt;/script&gt;" in html
