"""New driver protocol controls; no database, provider or actual boot evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tools.l12_boot_owner_clock import OwnerClock, OwnerProtocolRefusal
from tools.l12_recorded_boot_driver import CaseClock, RecordedBootDriver, reviewed_steps
from tools.l12_recorded_boot_timeline import RecordedSessionMap


FRI_0400 = 1792742400000
FRI_0700 = 1792753200000
MON_0400 = 1793001600000


def mapping():
    return RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)


def event(kind="observe_0700", stamp=FRI_0700, members=None):
    return dict(kind=kind, clock_ns=stamp * 1000000, watchlist=members,
        raw_ref="CONTROLLED_RETAINED_EVENT_ORDINAL_1", raw_sha256="c" * 64)


def prepared():
    return dict(mapping=mapping().receipt(), permission_granted=False,
        population=dict(names=["AAA", "BBB"], missing_chronology=["BBB"]),
        histories={}, missing_history=["AAA", "BBB"])


def test_declared_missing_names_not_removed_and_no_permission_from_driver_binding():
    value = prepared()
    driver = RecordedBootDriver(prepared=value, mapping=mapping(), events=[event()], maximum_owner_actions=3)
    assert driver.names == ("AAA", "BBB") and value["permission_granted"] is False
    assert driver.steps[0]["original_clock_ns"] == FRI_0700 * 1000000
    assert driver.steps[0]["clock_ns"] == (MON_0400 + 10800000) * 1000000


@pytest.mark.parametrize("fault", ["mapping", "permission", "budget_bool", "budget_zero", "not0700", "twice0700"])
def test_driver_refuses_implicit_mapping_permission_budget_or_false_0700(fault):
    value, events, budget = prepared(), [event()], 3
    if fault == "mapping":
        value["mapping"]["target_day"] = "2026-10-27"
    elif fault == "permission":
        value["permission_granted"] = True
    elif fault == "budget_bool":
        budget = True
    elif fault == "budget_zero":
        budget = 0
    elif fault == "not0700":
        events = [event(stamp=FRI_0700 + 1)]
    else:
        events.append(event())
    with pytest.raises(ValueError):
        RecordedBootDriver(prepared=value, mapping=mapping(), events=events, maximum_owner_actions=budget)


@pytest.mark.parametrize("fault", ["foreign_name", "duplicate_name", "no_ref", "bad_SHA", "native", "extra_key", "nonmembership_mutation", "rewind"])
def test_timeline_events_cannot_hide_unknowns_reorder_or_update_native(fault):
    item, events = event("membership", FRI_0400, ["AAA"]), None
    if fault == "foreign_name":
        item["watchlist"] = ["CCC"]
    elif fault == "duplicate_name":
        item["watchlist"] *= 2
    elif fault == "no_ref":
        item["raw_ref"] = ""
    elif fault == "bad_SHA":
        item["raw_sha256"] = "not-a-sha"
    elif fault == "native":
        item["kind"] = "replace_native_header"
    elif fault == "extra_key":
        item["ready"] = True
    elif fault == "nonmembership_mutation":
        item["kind"] = "history_poll"
    else:
        events = [event(), item]
    with pytest.raises(ValueError):
        reviewed_steps(events or [item], mapping(), ("AAA", "BBB"))


def test_same_clock_source_order_is_preserved_not_sorted_by_success():
    events = [event("membership", FRI_0400, ["AAA"]), event("history_poll", FRI_0400)]
    before = deepcopy(events)
    result = reviewed_steps(events, mapping(), ("AAA",))
    assert [item["kind"] for item in result] == ["membership", "history_poll"]
    assert events == before


def test_case_clock_changes_only_model_clock_not_native_or_durable_headers():
    case = SimpleNamespace(now_ms=MON_0400, native_header_ms=123, durable_created_ms=456)
    clock = CaseClock(case)
    clock.advance(MON_0400 * 1000000 + 999999)
    assert clock.now_ns == MON_0400 * 1000000 + 999999
    assert case.now_ms == MON_0400 and case.native_header_ms == 123 and case.durable_created_ms == 456
    with pytest.raises(ValueError):
        clock.advance(MON_0400 * 1000000)


def bot():
    return SimpleNamespace(settings=SimpleNamespace(strategy_schwab_1m_v2_bar_poll_interval_seconds=1),
        _keepwatch_bar_tasks={}, _line_sessions={}, _keepwatch_bar_inboxes={},
        _line_published={}, _keepwatch_unknown=set(), strategy=SimpleNamespace(_symbol_states={}))


@pytest.mark.parametrize("names", [(), ("AAA", "AAA"), (True,), ("",)])
def test_fleet_owner_requires_explicit_valid_scope(names):
    with pytest.raises(OwnerProtocolRefusal):
        OwnerClock(bot(), SimpleNamespace(), SimpleNamespace(advance=lambda value: None),
            names, [], maximum_actions=3, before_action=lambda event: None)


def test_fleet_owner_accepts_both_names_but_never_foreign_task_or_observation():
    service_bot = bot()
    owner = OwnerClock(service_bot, SimpleNamespace(), SimpleNamespace(advance=lambda value: None, now_ns=1),
        ("AAA", "BBB"), [], maximum_actions=3, before_action=lambda event: None)
    assert owner.snapshot(symbol="BBB")["symbol"] == "BBB"
    with pytest.raises(OwnerProtocolRefusal):
        owner.snapshot(symbol="CCC")
    service_bot._keepwatch_bar_tasks["CCC"] = SimpleNamespace(done=lambda: False)
    with pytest.raises(OwnerProtocolRefusal):
        owner.owner_tasks()
