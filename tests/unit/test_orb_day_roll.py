"""ORB day-roll reset: per-symbol state + aggregators clear when the ET date changes,
so a bot left running across midnight starts the new session clean (no carryover of
running_high / traded flag / prior-day symbols). No-op within the same session."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService, _SymbolState, _ET
from project_mai_tai.settings import Settings


def _svc() -> OrbService:
    return OrbService(settings=Settings(orb_running_high_enabled=True), redis_client=MagicMock())


def _seed_state(svc):
    st = _SymbolState()
    st.running_high = 13.0
    st.traded = True
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
