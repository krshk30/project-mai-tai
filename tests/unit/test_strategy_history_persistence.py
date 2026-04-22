from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.strategy_core import IndicatorConfig, TradingConfig


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def fixed_now() -> datetime:
    return datetime(2026, 4, 1, 10, 0, tzinfo=UTC)


def make_runtime(session_factory: sessionmaker[Session]) -> StrategyBotRuntime:
    return StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="MACD Bot",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=TradingConfig().make_30s_variant(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=fixed_now,
        session_factory=session_factory,
    )


def seed_bars(runtime: StrategyBotRuntime) -> None:
    runtime.seed_bars(
        "CYCN",
        [
            {
                "open": 5.10,
                "high": 5.25,
                "low": 5.05,
                "close": 5.20,
                "volume": 25_000,
                "timestamp": datetime(2026, 4, 1, 14, 0, 0, tzinfo=UTC).timestamp(),
            },
            {
                "open": 5.20,
                "high": 5.40,
                "low": 5.18,
                "close": 5.35,
                "volume": 31_000,
                "timestamp": datetime(2026, 4, 1, 14, 0, 30, tzinfo=UTC).timestamp(),
            },
        ],
    )


def indicator_snapshot() -> dict[str, float | bool]:
    return {
        "price": 5.35,
        "ema9": 5.12,
        "ema20": 4.98,
        "macd": 0.041,
        "signal": 0.032,
        "histogram": 0.009,
        "stoch_k": 74.0,
        "vwap": 5.08,
        "decision_vwap": 5.08,
        "bar_volume": 31_000,
        "macd_delta": 0.006,
        "macd_above_signal": True,
        "macd_cross_above": False,
        "macd_increasing": True,
        "macd_was_below_3bars": True,
        "macd_delta_accelerating": True,
        "price_above_vwap": True,
        "price_above_ema9": True,
        "price_above_ema20": True,
    }


def test_strategy_bar_history_persists_bar_and_decision_snapshot() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory)
    seed_bars(runtime)

    indicators = indicator_snapshot()
    decision = runtime._record_decision(
        symbol="CYCN",
        status="signal",
        reason="P3_MACD_SURGE",
        indicators=indicators,
        path="P3_MACD_SURGE",
        score="5",
        score_details="histogram_growing,macd_increasing",
    )
    runtime._persist_bar_history(symbol="CYCN", indicators=indicators, decision=decision)

    with session_factory() as session:
        row = session.scalar(select(StrategyBarHistory))

    assert row is not None
    assert row.strategy_code == "macd_30s"
    assert row.symbol == "CYCN"
    assert row.interval_secs == 30
    assert float(row.close_price) == 5.35
    assert row.volume == 31_000
    assert row.position_state == "flat"
    assert row.decision_status == "signal"
    assert row.decision_path == "P3_MACD_SURGE"
    assert row.decision_score == "5"
    assert row.indicators_json["vwap"] == 5.08
    assert row.indicators_json["macd_above_signal"] is True


def test_strategy_bar_history_upserts_same_bar_without_duplicate_rows() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory)
    seed_bars(runtime)

    indicators = indicator_snapshot()
    pending = runtime._build_persisted_decision(
        symbol="CYCN",
        status="pending",
        reason="P2_VWAP_BREAKOUT waiting confirmation",
        indicators=indicators,
        path="P2_VWAP_BREAKOUT",
    )
    runtime._persist_bar_history(symbol="CYCN", indicators=indicators, decision=pending)

    signal = runtime._build_persisted_decision(
        symbol="CYCN",
        status="signal",
        reason="P2_VWAP_BREAKOUT",
        indicators=indicators,
        path="P2_VWAP_BREAKOUT",
        score="4",
        score_details="price_above_vwap,macd_increasing",
    )
    runtime._persist_bar_history(symbol="CYCN", indicators=indicators, decision=signal)

    with session_factory() as session:
        row_count = session.scalar(select(func.count()).select_from(StrategyBarHistory))
        row = session.scalar(select(StrategyBarHistory))

    assert row_count == 1
    assert row is not None
    assert row.decision_status == "signal"
    assert row.decision_reason == "P2_VWAP_BREAKOUT"
    assert row.decision_score == "4"
