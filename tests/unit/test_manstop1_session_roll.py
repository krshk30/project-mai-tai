"""Session-scoped OMS controls; retained C1 row replay, controlled missing operands."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as oms
from project_mai_tai.services.strategy_engine_app import current_scanner_session_start_utc
from tests.unit.test_oms_risk_service import build_test_session_factory, _mstop_service


RETAINED_PAYLOAD = {
    "symbols": ["BKYI", "NCPL", "PFSA", "SAIQ", "STAK"],
    "scanner_session_start_utc": "2026-10-22T08:00:00+00:00",
}
OLD = os.environ.get("MANSTOP_REPLAY_OLD") == "2e79"


def stamp(text):
    return datetime.fromisoformat(text)


@pytest.fixture
def rig(monkeypatch):
    if root := os.environ.get("MANSTOP_EXPECTED_SOURCE_ROOT"):
        assert Path(oms.__file__).resolve() == Path(root) / "src/project_mai_tai/oms/service.py"
    clock = [stamp("2026-10-23T16:33:02+00:00")]
    monotonic = [100.0]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms.time, "monotonic", lambda: monotonic[0])
    factory = build_test_session_factory()
    service = _mstop_service(factory)
    yield service, factory, clock, monotonic
    factory.kw["bind"].dispose()


def write(factory, payload, *, created="2026-10-22T15:45:00+00:00", kind="global_manual_stop_symbols"):
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type=kind, payload=payload, created_at=stamp(created)))
        session.commit()


def risk(service, *, symbol="BKYI", strategy="macd_30s", account="paper:macd_30s",
         intent="open", side="buy"):
    service._load_global_manual_stop_symbols()
    return service._evaluate_risk(TradeIntentEvent(source_service="MANSTOP1_CONTROLLED_REPLAY",
        payload=TradeIntentPayload(strategy_code=strategy, broker_account_name=account,
            symbol=symbol, intent_type=intent, side=side, quantity=Decimal("2"),
            reason="MANSTOP1_CONTROLLED_NONSTOP_OPERANDS", metadata={})))


@pytest.mark.parametrize("at", ["16:33:02", "16:42:04", "17:03:04"])
def test_retained_oct22_row_at_three_oct23_refusals(rig, at):
    service, factory, clock, _ = rig
    clock[0] = stamp("2026-10-23T" + at + "+00:00")
    write(factory, RETAINED_PAYLOAD)
    result = risk(service)
    assert result == ((False, "manual_stop:BKYI") if OLD else (True, "ok"))
    with factory() as session:
        assert session.scalar(select(DashboardSnapshot)).payload == RETAINED_PAYLOAD
    print(json.dumps({"case": "retained_row", "source": "OLD2e79" if OLD else "NEW",
        "at_utc": clock[0].isoformat(), "risk": result,
        "provenance": "C1 LATEST14:00 retained row/time; intent/account/quantity controlled; not native replay"}))


def test_today_stop_remains_refused(rig):
    service, factory, _, _ = rig
    write(factory, {**RETAINED_PAYLOAD, "scanner_session_start_utc": "2026-10-23T08:00:00+00:00"})
    assert risk(service) == (False, "manual_stop:BKYI")


def test_0355_stop_rolls_at_0400_even_inside_cache_window(rig):
    service, factory, clock, mono = rig
    clock[0] = stamp("2026-10-24T07:55:00+00:00")
    write(factory, {"symbols": ["BKYI"], "scanner_session_start_utc": "2026-10-23T08:00:00+00:00"})
    assert risk(service) == (False, "manual_stop:BKYI")
    clock[0] = stamp("2026-10-24T08:00:00+00:00")
    mono[0] += 0.1
    assert risk(service) == ((False, "manual_stop:BKYI") if OLD else (True, "ok"))


def test_db_error_keeps_current_session_but_not_previous(rig):
    service, factory, clock, mono = rig
    write(factory, {"symbols": ["BKYI"], "scanner_session_start_utc": "2026-10-23T08:00:00+00:00"})
    assert risk(service) == (False, "manual_stop:BKYI")
    calls = []
    def unavailable():
        calls.append(clock[0])
        raise RuntimeError("CONTROLLED DB unavailable")
    service.session_factory = unavailable
    mono[0] += service._MANUAL_STOP_CACHE_SECS + 1
    assert risk(service) == (False, "manual_stop:BKYI")
    for _ in range(10):
        assert risk(service) == (False, "manual_stop:BKYI")
    assert len(calls) == 1
    clock[0] = stamp("2026-10-24T08:00:00+00:00")
    mono[0] += 0.1
    expected = (False, "manual_stop:BKYI") if OLD else (True, "ok")
    assert risk(service) == expected
    assert len(calls) == (1 if OLD else 2)


@pytest.mark.parametrize("intent,side", [("close", "sell"), ("cancel", "buy"), ("scale", "sell")])
def test_reducing_and_cancel_paths_unchanged(rig, intent, side):
    service, factory, _, _ = rig
    write(factory, {"symbols": ["BKYI"], "scanner_session_start_utc": "2026-10-23T08:00:00+00:00"})
    assert service._load_global_manual_stop_symbols() == {"BKYI"}
    assert risk(service, intent=intent, side=side) == (True, "ok")


@pytest.mark.parametrize("marker", [None, "", "bad", "2026-10-22T08:00:00+00:00",
                                  "2026-10-24T08:00:00+00:00", 123])
def test_unbound_or_other_marker_is_empty(rig, marker):
    service, factory, _, _ = rig
    payload = {"symbols": ["BKYI"]}
    if marker is not None:
        payload["scanner_session_start_utc"] = marker
    write(factory, payload)
    assert service._load_global_manual_stop_symbols() == set()


@pytest.mark.parametrize("marker", ["2026-10-23T08:00:00+00:00", "2026-10-23T04:00:00-04:00",
                                  "2026-10-23T08:00:00"])
def test_marker_comparison_matches_control_plane_timezone_contract(rig, marker):
    service, factory, _, _ = rig
    write(factory, {"symbols": [" bkyi "], "scanner_session_start_utc": marker})
    assert service._load_global_manual_stop_symbols() == {"BKYI"}


def test_current_cache_one_query_per_window_and_per_bot_row_untouched(rig):
    service, factory, clock, mono = rig
    payload = {"symbols": ["BKYI"], "scanner_session_start_utc": current_scanner_session_start_utc(clock[0]).isoformat()}
    write(factory, payload)
    write(factory, {"bots": {"macd_30s": ["OTHER"]}}, kind="bot_manual_stop_symbols")
    calls = []
    def counted():
        calls.append(1)
        return factory()
    service.session_factory = counted
    for _ in range(100):
        assert risk(service) == (False, "manual_stop:BKYI")
    assert len(calls) == 1
    mono[0] += service._MANUAL_STOP_CACHE_SECS + 1
    assert risk(service) == (False, "manual_stop:BKYI")
    assert len(calls) == 2
    with factory() as session:
        rows = session.scalars(select(DashboardSnapshot)).all()
    assert len(rows) == 2 and rows[1].payload == {"bots": {"macd_30s": ["OTHER"]}}


@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "momentum_live", "orb"])
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:webull"])
@pytest.mark.parametrize("at", ["2026-10-23T12:30:00+00:00", "2026-10-23T17:00:00+00:00"])
def test_global_chokepoint_blocks_every_bot_broker_session(rig, strategy, account, at):
    service, factory, clock, _ = rig
    clock[0] = stamp(at)
    write(factory, {"symbols": ["BKYI"], "scanner_session_start_utc": "2026-10-23T08:00:00+00:00"})
    assert risk(service, strategy=strategy, account=account) == (False, "manual_stop:BKYI")


def test_dst_session_boundary_uses_existing_helper(rig):
    service, factory, clock, mono = rig
    clock[0] = stamp("2026-11-01T08:59:59+00:00")
    write(factory, {"symbols": ["BKYI"], "scanner_session_start_utc": "2026-10-31T08:00:00+00:00"})
    assert risk(service) == (False, "manual_stop:BKYI")
    clock[0] += timedelta(seconds=1)
    mono[0] += 0.1
    assert risk(service) == (True, "ok")
