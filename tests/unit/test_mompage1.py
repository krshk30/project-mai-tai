from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path
import statistics
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.events import IsolatedBotStateEvent, StrategyBotStatePayload
from project_mai_tai import momentum_page_history as history
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings


FIXTURE = Path(__file__).parents[1] / "fixtures" / "mompage1_20261007_terminal.json"


class RedisSnapshot:
    def __init__(self, *, running=False):
        self.running = running

    async def xrevrange(self, stream, **kwargs):
        if not self.running or not stream.endswith(":strategy-state-isolated"):
            return []
        return [
            (str(index), {"data": IsolatedBotStateEvent(
                source_service="momentum-paper",
                payload=StrategyBotStatePayload(
                    strategy_code=code, account_name="paper:momentum",
                    watchlist=["BIYA"], daily_pnl=999, closed_today=[],
                    positions=[{"ticker": "BIYA", "quantity": 193, "entry_price": 2.5897}],
                ),
            ).model_dump_json()})
            for index, code in enumerate(("momentum_30s", "momentum_60s"))
        ]

    async def aclose(self):
        return None


def make_app(monkeypatch, *, running=False, records=True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    if records:
        with factory() as session:
            for item in json.loads(FIXTURE.read_text()):
                session.add(MomentumPaperEvent(
                    **{key: item[key] for key in ("event_key", "logical_id", "strategy_code", "event_type", "symbol", "payload")},
                    session_date=date.fromisoformat(item["session_date"]),
                    observed_at=datetime.fromisoformat(item["observed_at"]),
                ))
            session.commit()
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 7, 20, 30, tzinfo=UTC))
    app = cp.build_app(
        settings=Settings(momentum_paper_enabled=True, redis_stream_prefix="test",
                          trade_coach_enabled=False, schwab_token_refresher_enabled=False),
        session_factory=factory, redis_client=RedisSnapshot(running=running),
    )
    return app, factory


def test_mompage1_page_load_receipt(monkeypatch):
    app, _ = make_app(monkeypatch)
    with TestClient(app) as client:
        for route in ("/bot/momentum-30", "/bot/momentum-60"):
            elapsed = []
            for _ in range(20):
                started = time.perf_counter()
                assert client.get(route).status_code == 200
                elapsed.append((time.perf_counter() - started) * 1000)
            print(f"MOMPAGE1 {route}: first_ms={elapsed[0]:.3f} median_ms={statistics.median(elapsed[1:]):.3f}")


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("code,route,count,pnl", [
    ("momentum_30s", "/bot/momentum-30", 5, "31.8044"),
    ("momentum_60s", "/bot/momentum-60", 9, "175.9955"),
])
def test_mompage1_recorded_fourteen_trades_survive_stop_and_reload(monkeypatch, running, code, route, count, pnl):
    app, _ = make_app(monkeypatch, running=running)
    rendered = []
    original = cp._render_bot_detail_page

    def capture(data, strategy_code, **kwargs):
        bot = cp._find_bot_view(data, strategy_code)
        rendered.append(bot)
        return original(data, strategy_code, **kwargs)

    monkeypatch.setattr(cp, "_render_bot_detail_page", capture)
    with TestClient(app) as client:
        for _ in range(2):
            page = client.get(route)
            assert page.status_code == 200
            bot = rendered[-1]
            assert len(bot["closed_today"]) == count
            assert Decimal(str(bot["daily_pnl"])) == Decimal(pnl)
            _, completed_count, completed_pnl = cp._build_completed_position_rows(bot, [], [])
            assert completed_count == count
            assert completed_pnl == pytest.approx(float(pnl))
            assert bool(bot["positions"]) is running
            assert bool(bot["watchlist"]) is running
            assert f"${float(pnl):+.2f}" in page.text
        assert rendered[0]["closed_today"] == rendered[1]["closed_today"]
        cached = client.get("/api/bots").json()["bots"]
        cached_bot = next(item for item in cached if item["strategy_code"] == code)
        assert cached_bot["closed_today"] == []
        assert cached_bot["daily_pnl"] == (999 if running else 0)


def test_mompage1_empty_session_is_empty_without_error(monkeypatch):
    app, _ = make_app(monkeypatch, records=False)
    with TestClient(app) as client:
        for route in ("/bot/momentum-30", "/bot/momentum-60"):
            response = client.get(route)
            assert response.status_code == 200
            assert "No completed positions" in response.text


def test_mompage1_uses_et_session_and_never_path_prints_or_foreign_strategy(monkeypatch):
    _, factory = make_app(monkeypatch)
    with factory() as session:
        for suffix, code, event_type, day in (
            ("path", "momentum_30s", "PATH_PRINT", date(2026, 10, 7)),
            ("foreign", "orb", "FINAL", date(2026, 10, 7)),
            ("yesterday", "momentum_30s", "FINAL", date(2026, 10, 6)),
        ):
            session.add(MomentumPaperEvent(
                event_key=suffix, logical_id=suffix, strategy_code=code, event_type=event_type,
                session_date=day, symbol="NOT_THIS_PAGE", observed_at=datetime(2026, 10, 7, tzinfo=UTC),
                payload={"fill": {"price": "1", "sip_ts_ms": 1791360831534},
                         "exit": {"price": "2", "sip_ts_ms": 1791360834171}, "quantity": 10},
            ))
        session.commit()
    closed, pnl = history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7))
    assert len(closed) == 5 and pnl == Decimal("31.8044")
    assert all(item["ticker"] != "NOT_THIS_PAGE" for item in closed)
    assert history.load_completed_today(factory, "momentum_30s", date(2026, 10, 8)) == ([], Decimal("0"))


def test_mompage1_row_bound_refuses_partial_pnl(monkeypatch):
    _, factory = make_app(monkeypatch)
    monkeypatch.setattr(history, "MAX_TERMINAL_ROWS", 2)
    with pytest.raises(RuntimeError, match="refusing partial P&L"):
        history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7))


def test_mompage1_filled_and_exited_join_once_without_final(monkeypatch):
    _, factory = make_app(monkeypatch)
    with factory() as session:
        session.execute(delete(MomentumPaperEvent).where(MomentumPaperEvent.event_type == "FINAL"))
        session.commit()
    closed, pnl = history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7))
    assert len(closed) == 5 and pnl == Decimal("31.8044")
    with factory() as session:
        session.execute(delete(MomentumPaperEvent).where(MomentumPaperEvent.event_type == "EXITED"))
        session.commit()
    assert history.load_completed_today(factory, "momentum_30s", date(2026, 10, 7)) == ([], Decimal("0"))


def test_mompage1_page_date_is_et_not_utc(monkeypatch):
    app, _ = make_app(monkeypatch)
    monkeypatch.setattr(cp, "utcnow", lambda: datetime(2026, 10, 8, 2, 30, tzinfo=UTC))
    with TestClient(app) as client:
        response = client.get("/bot/momentum-30")
        assert response.status_code == 200
        assert "$+31.80" in response.text


def test_mompage1_other_pages_never_read_momentum_history(monkeypatch):
    app, _ = make_app(monkeypatch)

    def unexpected(*args, **kwargs):
        raise AssertionError("another page read Momentum history")

    monkeypatch.setattr(cp, "load_completed_today", unexpected)
    with TestClient(app) as client:
        assert client.get("/bot/orb").status_code == 200
