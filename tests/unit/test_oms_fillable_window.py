"""OMS exit fillable-session gate (`_market_is_fillable`) + window settings.

The gate keeps the OMS from placing/refreshing exit orders when they cannot fill
(outside 7 AM–8 PM ET). Its clock logic lives in `is_fillable_et_session`
(test_time_utils); here we cover the OMS method's settings wiring + defaults.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

EASTERN = ZoneInfo("America/New_York")


class _NoopRedis:
    def __getattr__(self, _name):  # any incidental redis call is a no-op
        def _noop(*args, **kwargs):
            return None
        return _noop


def _session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _svc(**settings_kwargs) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(oms_adapter="simulated", **settings_kwargs),
        redis_client=_NoopRedis(),
        session_factory=_session_factory(),
    )


def test_market_is_fillable_uses_real_settings_window() -> None:
    svc = _svc()  # default 7–20 ET
    assert svc._market_is_fillable(datetime(2026, 7, 14, 10, 0, tzinfo=EASTERN)) is True
    assert svc._market_is_fillable(datetime(2026, 7, 14, 6, 59, tzinfo=EASTERN)) is False  # pre-7 AM
    assert svc._market_is_fillable(datetime(2026, 7, 14, 20, 0, tzinfo=EASTERN)) is False  # 8 PM excl.
    assert svc._market_is_fillable(datetime(2026, 7, 14, 19, 59, tzinfo=EASTERN)) is True
    assert svc._market_is_fillable(datetime(2026, 7, 11, 12, 0, tzinfo=EASTERN)) is False  # Saturday


def test_market_is_fillable_respects_settings_override() -> None:
    svc = _svc(oms_fillable_session_start_hour_et=9, oms_fillable_session_end_hour_et=16)
    assert svc._market_is_fillable(datetime(2026, 7, 14, 8, 0, tzinfo=EASTERN)) is False
    assert svc._market_is_fillable(datetime(2026, 7, 14, 10, 0, tzinfo=EASTERN)) is True
    assert svc._market_is_fillable(datetime(2026, 7, 14, 16, 0, tzinfo=EASTERN)) is False  # end excl.


def test_window_settings_defaults() -> None:
    s = Settings()
    assert s.oms_fillable_session_start_hour_et == 7
    assert s.oms_fillable_session_end_hour_et == 20
    assert s.strategy_schwab_1m_v2_entry_window_start_hour_et == 7
    assert s.strategy_schwab_1m_v2_entry_window_end_hour_et == 18
