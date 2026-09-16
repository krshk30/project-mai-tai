"""ORB day-roll reset clears prior-session paper observation state."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import (
    OrbService,
    _scanner_session_start_utc,
    _SymbolState,
    _ET,
)
from project_mai_tai.settings import Settings


def _svc() -> OrbService:
    return OrbService(settings=Settings(orb_running_high_enabled=True), redis_client=MagicMock())


def _seed_state(svc):
    st = _SymbolState()
    st.running_high = 13.0
    st.paper_entries = 1
    svc._states["PLSM"] = st
    svc._aggregators["PLSM"] = object()


def test_same_session_is_noop():
    svc = _svc()
    _seed_state(svc)
    svc._maybe_roll_session()                 # same ET date as init
    assert "PLSM" in svc._states
    assert "PLSM" in svc._aggregators


def test_day_roll_clears_state_and_aggregators():
    svc = _svc()
    _seed_state(svc)
    svc._session_date = date(2000, 1, 1)      # force a prior-day session
    svc._maybe_roll_session()
    assert svc._states == {}
    assert svc._aggregators == {}
    assert svc._session_date == datetime.now(_ET).date()


def test_scanner_roll_clears_state_without_a_calendar_date_change():
    svc = _svc()
    _seed_state(svc)
    svc._session_date = datetime.now(_ET).date()
    svc._scanner_session_start = datetime(2000, 1, 1, tzinfo=UTC)

    svc._maybe_roll_session()

    assert svc._states == {}
    assert svc._aggregators == {}
    assert svc._scanner_session_start == _scanner_session_start_utc()


def test_scanner_session_boundary_is_0400_et():
    before = datetime(2026, 9, 16, 3, 59, 59, tzinfo=_ET)
    boundary = datetime(2026, 9, 16, 4, 0, tzinfo=_ET)

    assert _scanner_session_start_utc(before) == datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
    assert _scanner_session_start_utc(boundary) == datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
