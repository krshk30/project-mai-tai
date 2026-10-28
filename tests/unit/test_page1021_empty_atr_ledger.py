"""PAGE1021 local read-status controls; no provider or runtime actions."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_atrpnl1 import fill
from tests.unit.test_control_plane import build_test_session_factory


def repository_case(monkeypatch, count=0):
    now = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(cp, "utcnow", lambda: now)
    factory = build_test_session_factory()
    repository = cp.ControlPlaneRepository(
        Settings(_env_file=None, legacy_api_base_url="", trade_coach_enabled=False,
                 strategy_schwab_1m_v2_enabled=True),
        session_factory=factory, redis=None,
    )
    if count:
        with factory() as session:
            strategy = Strategy(code="orb_schwab", name="controlled non-ATR", metadata_json={})
            account = BrokerAccount(name="controlled:orb", provider="schwab", environment="test")
            session.add_all([strategy, account])
            session.flush()
            order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id="controlled-other-entry", symbol="TEST", side="buy",
                quantity=Decimal(count), order_type="LIMIT", time_in_force="day",
                status="filled", payload={}, submitted_at=now, updated_at=now)
            session.add(order)
            session.flush()
            session.add_all([
                Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                     broker_fill_id=f"controlled-{index}", symbol="TEST", side="buy",
                     quantity=Decimal(1), price=Decimal("9.87654321"), filled_at=now, payload={})
                for index in range(count)
            ])
            session.commit()
    return repository, factory


def atr_view(repository, rows, status):
    views = repository._build_bot_views(
        strategy_runtime={"bots": {"schwab_1m_v2": {"daily_pnl": -999}}},
        legacy_shadow={}, recent_intents=[], recent_orders=[], recent_fills=rows,
        fills_read_status=status, recent_trade_coach_reviews=[], recent_bar_decisions=[],
        open_orders=[], persisted_snapshots={},
    )
    return next(row for row in views if row["strategy_code"] == "schwab_1m_v2")


@pytest.mark.parametrize("rows", [[], [{"strategy_code": "orb_schwab"}]])
def test_empty_atr_requires_explicit_successful_read(rows):
    assert cp._atr_episode_fill_ledger(rows, fills_read_status="READABLE") == ([], 0.0)
    assert cp._atr_episode_fill_ledger(rows) is None
    assert cp._atr_episode_fill_ledger(rows, fills_read_status="UNKNOWN") is None
    assert cp._atr_episode_fill_ledger(rows, fills_read_status="FAILED") is None


def test_explicit_unknown_blocks_even_nonempty_legacy_compatible_evidence():
    rows = [fill("buy", 1, 4, "entry"), fill("sell", 1, 5, "entry-ocoexit-native")]
    assert cp._atr_episode_fill_ledger(rows) == cp._atr_episode_fill_ledger(
        rows, fills_read_status="READABLE")
    assert cp._atr_episode_fill_ledger(rows, fills_read_status="UNKNOWN") is None


@pytest.mark.parametrize("lightweight", [False, True])
def test_actual_successful_empty_database_read_is_readable_zero(monkeypatch, lightweight):
    repository, _ = repository_case(monkeypatch)
    state = repository._load_database_state(lightweight=lightweight)
    assert state["errors"] == []
    assert state["recent_fills_raw_count"] == 0
    assert state["recent_fills_read_status"] == "READABLE"
    view = atr_view(repository, state["recent_fills"], state["recent_fills_read_status"])
    assert view["daily_pnl"] == 0.0 and view["closed_today"] == []
    assert view["closed_today_source"] == "ATTRIBUTED_EPISODE_FILLS"
    assert view["daily_pnl_partial"] is False
    data = {"bots": [view], "recent_fills": [], "recent_orders": [],
            "account_positions": [], "virtual_positions": []}
    payload = cp._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] == 0.0 and payload["closed_today"] == []
    html = cp._render_bot_detail_page(data, "schwab_1m_v2")
    assert "UNKNOWN: entry-episode fill coverage is incomplete" not in html
    assert "$-999.00" not in html


@pytest.mark.parametrize("lightweight", [False, True])
@pytest.mark.parametrize("failure", ["connection", "fills_query"])
def test_actual_database_failure_never_turns_empty_into_readable(monkeypatch, lightweight, failure):
    repository, factory = repository_case(monkeypatch)
    engine = factory.kw["bind"]

    def fail_read(connection, cursor, statement, parameters, context, executemany):
        if failure == "connection" or ("FROM fills" in statement and "count(" not in statement):
            raise SQLAlchemyError("CONTROLLED PAGE1021 read failure")

    event.listen(engine, "before_cursor_execute", fail_read)
    try:
        state = repository._load_database_state(lightweight=lightweight)
    finally:
        event.remove(engine, "before_cursor_execute", fail_read)
    assert state["recent_fills"] == []
    assert state["recent_fills_raw_count"] is None
    assert state["recent_fills_read_status"] == "UNKNOWN"
    assert any("CONTROLLED PAGE1021 read failure" in error for error in state["errors"])
    view = atr_view(repository, state["recent_fills"], state["recent_fills_read_status"])
    assert view["daily_pnl"] is None and view["closed_today"] == []
    assert view["closed_today_source"] == "UNKNOWN_FILL_EPISODE_COVERAGE"


@pytest.mark.parametrize("lightweight", [False, True])
@pytest.mark.parametrize("count, expected", [(999, "READABLE"), (1000, "UNKNOWN")])
@pytest.mark.parametrize("hidden", [False, True])
def test_raw_fill_cap_survives_non_atr_rows_and_hidden_filter(
        monkeypatch, lightweight, count, expected, hidden):
    repository, _ = repository_case(monkeypatch, count)
    if hidden:
        monkeypatch.setattr(repository, "_filter_symbol_rows", lambda rows: [])
    state = repository._load_database_state(lightweight=lightweight)
    assert state["errors"] == []
    assert state["recent_fills_raw_count"] == count
    assert len(state["recent_fills"]) == (0 if hidden else count)
    assert state["recent_fills_read_status"] == expected
    view = atr_view(repository, state["recent_fills"], state["recent_fills_read_status"])
    assert view["daily_pnl"] == (0.0 if expected == "READABLE" else None)
    assert view["closed_today_source"] == (
        "ATTRIBUTED_EPISODE_FILLS" if expected == "READABLE" else "UNKNOWN_FILL_EPISODE_COVERAGE")


@pytest.mark.parametrize("lightweight", [False, True])
def test_dropped_fill_conversion_cannot_prove_empty_readable(monkeypatch, lightweight):
    repository, _ = repository_case(monkeypatch, 1)
    original = cp._decimal_str

    def reject_controlled_price(value):
        if value == Decimal("9.87654321"):
            raise ValueError("CONTROLLED malformed fill projection")
        return original(value)

    monkeypatch.setattr(cp, "_decimal_str", reject_controlled_price)
    state = repository._load_database_state(lightweight=lightweight)
    assert state["recent_fills_raw_count"] == 1 and state["recent_fills"] == []
    assert state["recent_fills_read_status"] == "UNKNOWN"
    assert [warning["table"] for warning in state["row_conversion_warnings"]] == ["fills"]
    view = atr_view(repository, state["recent_fills"], state["recent_fills_read_status"])
    assert view["daily_pnl"] is None


def test_atr_limit_rejects_even_explicit_success():
    assert cp._atr_episode_fill_ledger(
        [fill("buy", 1, 4, str(index)) for index in range(1000)],
        fills_read_status="READABLE",
    ) is None
