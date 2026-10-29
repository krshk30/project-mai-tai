"""Exact ready-subset composition under the reviewed installed ON profile.

Controlled-input qualification, not live execution evidence. Existing literal
OLD and private FILE fixtures retain their complete state and decision checks.
Only test Settings construction supplies the combined profile; no permission
method is replaced here. The separate ORB combo exercises the real OMS guards.
"""

from datetime import date, datetime
import json
import logging

import pytest

from project_mai_tai.settings import Settings
from tests.unit import test_momlive1_runtime as mom_fixture
from tests.unit import test_pomexitlog1_v4 as pom
from tests.unit import test_postflip_nochase_hooks as nochase
from tests.unit import test_scanner_session_strategy_parity as scanner


ORB = "orb_country_semantic_admission_enabled"
CAPTURE = "scanner_confirmed_capture_session_day_enabled"
PROFILE = {
    ORB: True,
    CAPTURE: True,
    "strategy_schwab_1m_v2_keepwatch1_enabled": True,
    "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
    "scanner_nuweref1_enabled": False,
}


def bind_profile(monkeypatch, module, *, capture_comparison=False):
    """Real Settings with explicit inputs; never replace a business guard.

    Scanner's three worlds vary its one capture policy switch deliberately.
    ORB and the existing live switches are uniformly ON in all three, so their
    full state graphs differ only by the already-reviewed capture policy input.
    This is not a new claim about the actual installed ALL-NEW-OFF baseline.
    """
    assert Settings.model_fields[ORB].default is False
    assert Settings.model_fields[CAPTURE].default is False
    constructed = []

    def configured(*args, **kwargs):
        supplied = dict(PROFILE)
        if capture_comparison:
            assert CAPTURE in kwargs
            supplied[CAPTURE] = kwargs[CAPTURE]
        kwargs.update(supplied)
        kwargs.setdefault("_env_file", None)
        value = Settings(*args, **kwargs)
        for name, expected in supplied.items():
            assert getattr(value, name) is expected
        constructed.append(value)
        return value

    monkeypatch.setattr(module, "Settings", configured)
    return constructed


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
@pytest.mark.parametrize("case", ["normal", "chase", "positions_error", "cancelled"])
async def test_all_on_pom_conserves_complete_decisions_and_native_state(monkeypatch, hour, case):
    profiles = bind_profile(monkeypatch, mom_fixture)
    before, quiet = await pom.controlled_exit(monkeypatch, diagnostics=False, hour=hour, case=case)
    after, lines = await pom.controlled_exit(monkeypatch, diagnostics=True, hour=hour, case=case)
    # Complete serialized decisions, requests, clocks, guard results, owners,
    # durable economics/payloads and native handles; no field removal/rounding.
    assert after.encode() == before.encode()
    assert len(profiles) == 2
    assert quiet == {"gate": [], "attempt": []}
    data = json.loads(after)
    assert data["active_count"] == 1 and data["guard_results"]
    exits = [request for request in data["requests"]
             if request["intent_type"] == "close" and request["metadata"].get("exit_reason")]
    expected_wires = 2 if case == "chase" and hour == 12 else 1 if case in {"normal", "chase"} else 0
    assert len(exits) == expected_wires
    assert data["result"] == {"positions_error": "OSError", "cancelled": "CancelledError"}.get(case, "returned")
    assert len(lines["gate"]) <= 20
    assert len(lines["attempt"]) == expected_wires


@pytest.mark.parametrize("hour", [12, 18], ids=["PM", "RTH"])
@pytest.mark.parametrize("state_only", [False, True])
def test_all_on_nochase_keeps_literal_old_real_gate_country_and_owner_state(monkeypatch, caplog, hour, state_only):
    profiles = bind_profile(monkeypatch, nochase)
    literal = nochase.literal_old()
    old, now = nochase.strategy(literal, hour=hour, dual=True)
    new, new_now = nochase.strategy(nochase.proposed, hour=hour, dual=True)
    assert new_now == now and len(profiles) == 2
    nochase.controlled_buy(old, literal, now, state_only=state_only)
    state = nochase.controlled_buy(new, nochase.proposed, now, state_only=state_only)
    caplog.set_level(logging.INFO)

    def apply(value, event):
        current = value.watchlist_state("POM")
        if event["operation"] == "country":
            with pytest.raises(nochase.CountryRouteRefusal) as exc:
                value._country_entry_selection(current)
            return str(exc.value)
        return value._strict_first_rest_admitted(current, slot="first")

    events = [dict(origin="CONTROLLED_STATE", source_ref="exact-subset:real-BUY-transition",
                   operation=operation) for operation in ("gate", "country", "gate")]
    result = nochase.compare_supplied_state(events, old=old, proposed=new,
                                           apply_event=apply, capture=nochase.capture)
    assert result["supplied_state_equal"]
    assert result["historical_replay_qualified"] is False
    assert [row["proposed_result"] for row in result["rows"]] == [
        False, "country_owner_admission_unproven", False]
    lines = [record.getMessage() for record in caplog.records
             if "[V2-POSTFLIP-NOCHASE]" in record.getMessage()]
    assert len(lines) == 1 and f"flip_bar={now}" in lines[0]
    assert new._flip_owner_counts["admission_evaluated"] == 3
    assert state._postflip_nochase_log.current == (state.atr_session_anchor_ms, now)


def capture_policy_only(old_calls, new_calls, *, before_four):
    assert len(old_calls) == len(new_calls)
    for (old_kind, old), (new_kind, new) in zip(old_calls, new_calls):
        assert old_kind == new_kind
        assert {key: value for key, value in old.items() if key not in {"trade_date", "event_at"}} == {
            key: value for key, value in new.items() if key not in {"trade_date", "event_at"}}
        if not before_four:
            assert old == new
        else:
            assert new["trade_date"] == date(2026, 10, 26)
            if new_kind == "INSERT" and new["symbol"] == "CARRY":
                assert new["event_at"] == datetime(2026, 10, 26, 9, 36, 55, tzinfo=scanner.ET)


@pytest.mark.parametrize("hour,retention,db_error,clock_missing", [
    (0, False, False, False), (0, True, False, False),
    (13, False, False, False), (13, True, False, False),
    (0, True, True, False), (0, True, False, True),
], ids=["before04", "before04-retention", "RTH", "RTH-retention", "capture-error", "unknown-clock"])
def test_all_on_scanner_real_loops_keep_full_state_alerts_and_watchlists(
        tmp_path, monkeypatch, hour, retention, db_error, clock_missing):
    monkeypatch.chdir(tmp_path)
    profiles = bind_profile(monkeypatch, scanner, capture_comparison=True)
    start = datetime(2026, 10, 27, hour, 10, tzinfo=scanner.ET)
    common = dict(start=start, clock_missing=clock_missing, db_error=db_error,
                  retention=retention, monkeypatch=monkeypatch)
    old = scanner.run_world(scanner.literal_old(), enabled=False, **common)
    off = scanner.run_world(scanner.current, enabled=False, **common)
    on = scanner.run_world(scanner.current, enabled=True, **common)
    assert len(profiles) == 3
    assert [getattr(value, CAPTURE) for value in profiles] == [False, False, True]
    for baseline, disabled, enabled in zip(old, off, on):
        assert scanner.comparable(baseline) == scanner.comparable(disabled) == scanner.comparable(enabled)
        assert baseline["capture"] == disabled["capture"]
        if clock_missing:
            # Original approved UNKNOWN behavior omits only CARRY's unprovable
            # confirmation capture. Never invent a timestamp or drop decisions.
            assert len(enabled["capture"]) <= len(baseline["capture"])
            assert not any(kind == "INSERT" and params["symbol"] == "CARRY"
                           for kind, params in enabled["capture"])
            assert "CARRY" in enabled["output"]["watchlist"]
        else:
            capture_policy_only(baseline["capture"], enabled["capture"], before_four=hour < 4)
    if db_error:
        for world in (old, off, on):
            assert world[1]["commits"] == world[0]["commits"]
            assert world[2]["commits"] > world[1]["commits"]
