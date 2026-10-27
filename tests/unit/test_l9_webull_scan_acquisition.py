"""L9 preparatory scan mechanics only; no terminal-time or native-case proof."""

from copy import deepcopy

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import (
    QueryBudget, QueryBudgetUnavailable, TodayOrderReader,
)


def raw(coid):
    return {"client_order_id": coid, "account_id": "ACC1", "items": [{"status": "CANCELLED"}]}


def test_each_explicit_refresh_is_at_most_one_http_and_continues_partial_cursor():
    clock = [0.]
    budget = QueryBudget(clock=lambda: clock[0])
    reader = TodayOrderReader(budget, 15, clock=lambda: clock[0])
    calls = []

    def page(cursor):
        calls.append(cursor)
        coid = str(len(calls))
        return 200, {"orders": [raw(coid)], "has_next": len(calls) < 3}

    generation = None
    for index in range(3):
        before = len(calls)
        reader.refresh_one_page("ACC1", page)
        assert len(calls) == before + 1
        scan = reader.scans["ACC1"]
        generation = generation or scan.generation
        assert scan.generation == generation
        if index < 2:
            assert not scan.complete and reader.peek_complete("ACC1", [str(index + 1)]) is None
        clock[0] += 15.
    assert calls == ["", "1", "2"]
    assert scan.complete and not scan.failed and scan.pages == 3


def test_ordinary_reader_keeps_existing_two_page_allowance():
    calls = []
    reader = TodayOrderReader(QueryBudget(), 15)

    def page(cursor):
        calls.append(cursor)
        return 200, {"orders": [raw(str(len(calls)))], "has_next": True}

    assert reader.read("ACC1", "missing", page) is None
    assert calls == ["", "1"]


def test_completing_later_page_never_reacquires_earlier_row_or_makes_it_fresh():
    clock = [0.]
    reader = TodayOrderReader(QueryBudget(clock=lambda: clock[0]), 15, clock=lambda: clock[0])
    rows = [raw("T"), raw("S")]
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": [rows[0]], "has_next": True}))
    acquired = reader.scans["ACC1"].rows["T"][:2]
    generation = reader.scans["ACC1"].generation
    clock[0] += 15.
    reader.refresh_one_page("ACC1", lambda cursor: (200, {"orders": [rows[1]], "has_next": False}))
    scan = reader.scans["ACC1"]
    assert scan.complete and scan.generation == generation
    assert scan.rows["T"][:2] == acquired and scan.rows["T"][0] < scan.rows["S"][0]
    assert reader.peek_complete("ACC1", ["T", "S"]) is None
    assert reader.peek_complete("ACC1", ["S"])[0]["_webull_list_evidence"]["scan_generation"] == generation


def test_native_input_mutation_cannot_rewrite_retained_acquisition():
    reader = TodayOrderReader(QueryBudget(), 15)
    body = raw("T")
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": [body], "has_next": False}))
    retained = deepcopy(reader.scans["ACC1"].rows["T"])
    body["items"][0]["status"] = "WORKING"
    assert reader.scans["ACC1"].rows["T"] == retained
    proof = reader.peek_complete("ACC1", ["T"])[0]
    assert proof["items"][0]["status"] == "CANCELLED"
    assert proof["_webull_list_evidence"]["failed"] is False


def test_new_generation_discards_completed_scan_without_borrowing_completeness():
    clock = [0.]
    reader = TodayOrderReader(QueryBudget(clock=lambda: clock[0]), 15, clock=lambda: clock[0])
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": [raw("old")], "has_next": False}))
    generation = reader.scans["ACC1"].generation
    clock[0] += 15.
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": [raw("new")], "has_next": True}))
    scan = reader.scans["ACC1"]
    assert scan.generation != generation and set(scan.rows) == {"new"}
    assert not scan.complete and reader.peek_complete("ACC1", ["new"]) is None


@pytest.mark.parametrize("failure", ["http", "malformed", "conflicting_identity"])
def test_failed_next_page_invalidates_all_terminal_cache_evidence(failure):
    clock = [0.]
    reader = TodayOrderReader(QueryBudget(clock=lambda: clock[0]), 15, clock=lambda: clock[0])
    reader.refresh_one_page("ACC1", lambda _: (200, {"orders": [raw("T")], "has_next": True}))
    changed = raw("T")
    changed["items"][0]["status"] = "WORKING"
    response = ((500, {}) if failure == "http" else (200, {"orders": [None], "has_next": False})
                if failure == "malformed" else (200, {"orders": [changed], "has_next": False}))
    clock[0] += 15.
    with pytest.raises(ValueError):
        reader.refresh_one_page("ACC1", lambda _: response)
    assert reader.scans["ACC1"].failed
    assert reader.peek_complete("ACC1", ["T"]) is None


def test_explicit_refresh_respects_existing_budget_without_attempt():
    clock = [0.]
    budget = QueryBudget(clock=lambda: clock[0])
    budget.claim("list-today", "ACC1")
    budget.claim("list-today", "ACC1")
    reader = TodayOrderReader(budget, 15, clock=lambda: clock[0])
    calls = []
    reader.refresh_one_page("ACC1", lambda cursor: calls.append(cursor))
    assert calls == [] and reader.scans["ACC1"].pages == 0
    assert len(budget.attempts["list-today"]) == 2


def test_http429_attempt_is_counted_and_backoff_shared_across_physical_accounts():
    clock = [0.]
    budget = QueryBudget(clock=lambda: clock[0])
    reader = TodayOrderReader(budget, 15, clock=lambda: clock[0])
    calls = []

    def limited(cursor):
        calls.append(cursor)
        return 429, {"error_code": "TOO_MANY_REQUESTS"}

    with pytest.raises(ValueError):
        reader.refresh_one_page("ACC1", limited)
    assert calls == [""] and len(budget.attempts["list-today"]) == 1
    clock[0] += 3.
    with pytest.raises(QueryBudgetUnavailable):
        budget.claim("list-today", "ACC2")
    reader.refresh_one_page("ACC2", limited)
    assert calls == [""]
    assert reader.scans["ACC1"].failed and not reader.scans["ACC2"].complete
    # Independent DETAIL endpoint retains its existing allowance.
    budget.claim("detail", "fresh-proof", strict=True)
