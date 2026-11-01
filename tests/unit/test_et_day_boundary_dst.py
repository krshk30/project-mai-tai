"""Controlled ET clock/DB boundary regressions; no broker/economic proof implied."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, Strategy, TradeIntent
from project_mai_tai import orb_live_decisions as tape_module
from project_mai_tai.services import control_plane as cp, schwab_1m_v2_bot as bot_module
from project_mai_tai.services import trade_coach_app as coach_module
from project_mai_tai.settings import Settings


# Explicit UTC oracles, including the session beginning BEFORE each 02:00 change.
CASES = [
    ("2026-11-01", "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00",
     "2026-10-31T08:00:00+00:00", "2026-11-01T09:00:00+00:00", "2026-11-01T08:56:00+00:00"),
    ("2027-03-14", "2027-03-14T05:00:00+00:00", "2027-03-15T04:00:00+00:00",
     "2027-03-13T09:00:00+00:00", "2027-03-14T08:00:00+00:00", "2027-03-14T07:30:00+00:00"),
    ("2026-10-29", "2026-10-29T04:00:00+00:00", "2026-10-30T04:00:00+00:00",
     "2026-10-29T08:00:00+00:00", "2026-10-30T08:00:00+00:00", "2026-10-29T16:00:00+00:00"),
]


@pytest.fixture(params=CASES, ids=["fallback-25h", "spring-23h", "normal-24h"])
def boundary(request):
    day, midnight, midnight_end, session, session_end, now = request.param
    return SimpleNamespace(day=day, midnight=datetime.fromisoformat(midnight),
        midnight_end=datetime.fromisoformat(midnight_end), session=datetime.fromisoformat(session),
        session_end=datetime.fromisoformat(session_end), now=datetime.fromisoformat(now))


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'offorder-boundaries.sqlite'}", poolclass=NullPool)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def review_rows(boundary):
    return [{"symbol": symbol, "summary": symbol, "strategy_code": "macd_30s",
             "created_at": at.astimezone(cp.EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET"),
             "verdict": "good", "payload": {}}
            for symbol, at in (("INSIDE_BOUNDARY_ROW", boundary.midnight_end - timedelta(minutes=30)),
                               ("OUTSIDE_BOUNDARY_ROW", boundary.midnight_end + timedelta(minutes=30)))]


def test_current_control_plane_session_end_is_next_04_et(boundary):
    assert cp.current_eastern_day_start_utc(boundary.now) == boundary.session
    assert cp.current_eastern_day_end_utc(boundary.now) == boundary.session_end
    assert cp._within_current_eastern_day(boundary.session_end - timedelta(microseconds=1), boundary.now)
    assert not cp._within_current_eastern_day(boundary.session_end, boundary.now)


@pytest.mark.asyncio
async def test_coach_review_api_explicit_end_date_uses_next_et_midnight(boundary, sessions):
    app = cp.build_app(settings=Settings(_env_file=None, trade_coach_enabled=True),
                       session_factory=sessions)

    async def dashboard():
        return {"bots": []}

    app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard,
        load_trade_coach_review_history=lambda: review_rows(boundary),
        load_trade_coach_regime_profiles=lambda rows: {})
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/coach-reviews")
    result = await endpoint(start_date=boundary.day, end_date=boundary.day)
    assert result["count"] == 1
    assert [row["symbol"] for row in result["reviews"]] == ["INSIDE_BOUNDARY_ROW"]


def test_coach_review_html_explicit_end_date_uses_next_et_midnight(boundary):
    html = cp._render_trade_coach_review_center({"bots": []}, review_history=review_rows(boundary),
        start_date=boundary.day, end_date=boundary.day)
    assert "INSIDE_BOUNDARY_ROW" in html
    assert "OUTSIDE_BOUNDARY_ROW" not in html


def test_trade_coach_completed_trade_window_ends_at_next_04_et(boundary, monkeypatch):
    monkeypatch.setattr(coach_module, "current_scanner_session_start_utc", lambda: boundary.session)
    app = coach_module.TradeCoachApp.__new__(coach_module.TradeCoachApp)
    app.settings = SimpleNamespace(trade_coach_completed_trade_lookback_days=0)
    start, end = app._review_window_bounds()
    assert start == datetime(2000, 1, 1, tzinfo=UTC)
    assert end == boundary.session_end


def test_v2_reported_closed_fills_include_only_actual_et_calendar_day(boundary, sessions, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            observed = boundary.midnight_end - timedelta(minutes=15)
            return observed.astimezone(tz) if tz is not None else observed.replace(tzinfo=None)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    with sessions.begin() as unit:
        strategy = Strategy(code="schwab_1m_v2", name="controlled", execution_mode="live")
        account = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="controlled")
        unit.add_all([strategy, account])
        unit.flush()
        for symbol, buy_at, sell_at in (
            ("INSIDE", boundary.midnight + timedelta(hours=1), boundary.midnight_end - timedelta(minutes=30)),
            ("AFTER", boundary.midnight_end + timedelta(minutes=1), boundary.midnight_end + timedelta(minutes=30)),
            ("BEFORE", boundary.midnight - timedelta(hours=1), boundary.midnight - timedelta(minutes=30)),
        ):
            for side, at, price in (("buy", buy_at, "10"), ("sell", sell_at, "11")):
                intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                    symbol=symbol, side=side, intent_type="open" if side == "buy" else "close",
                    quantity=Decimal(1), reason="CONTROLLED_BOUNDARY", status="filled", payload={})
                unit.add(intent)
                unit.flush()
                order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
                    broker_account_id=account.id, client_order_id=str(uuid4()), symbol=symbol,
                    side=side, order_type="limit", time_in_force="day", quantity=Decimal(1),
                    status="filled", payload={}, submitted_at=at)
                unit.add(order)
                unit.flush()
                unit.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                    broker_fill_id=str(uuid4()), symbol=symbol, side=side, quantity=Decimal(1),
                    price=Decimal(price), filled_at=at, payload={}))
    bot = bot_module.SchwabV2BotService.__new__(bot_module.SchwabV2BotService)
    bot.settings = SimpleNamespace(strategy_schwab_1m_v2_account_name="live:schwab_1m_v2")
    with sessions() as unit:
        closed, pnl = bot._closed_round_trips_today(unit)
    assert [row["symbol"] for row in closed] == ["INSIDE"]
    assert pnl == 1.0


def test_orb_observer_caps_exact_et_day_without_deleting_next_day(boundary, sessions):
    limit = tape_module.VISIBLE_LIMIT
    with sessions.begin() as unit:
        for label, at in (("inside", boundary.midnight_end - timedelta(minutes=30)),
                          ("after", boundary.midnight_end + timedelta(minutes=30))):
            unit.add_all(DashboardSnapshot(snapshot_type=tape_module.SNAPSHOT_TYPE,
                created_at=at + timedelta(microseconds=i), payload={"label": label})
                for i in range(limit + 2))
    tape = tape_module.LiveDecisionTape(sessions)
    tape._persist([{"evaluated_at": (boundary.midnight_end - timedelta(seconds=1)).isoformat(),
                    "label": "inside"}])
    with sessions() as unit:
        inside = unit.scalar(select(func.count()).select_from(DashboardSnapshot).where(
            DashboardSnapshot.created_at >= boundary.midnight,
            DashboardSnapshot.created_at < boundary.midnight_end))
        after = unit.scalar(select(func.count()).select_from(DashboardSnapshot).where(
            DashboardSnapshot.created_at >= boundary.midnight_end))
    assert inside == limit
    assert after == limit + 2
