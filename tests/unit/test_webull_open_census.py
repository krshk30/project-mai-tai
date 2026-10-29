"""CONTROLLED list-open wrappers; real detail rows are not live list-open proof."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1 import AdmissionSnapshot, NativeAccountBook, assess, decode, encode
from project_mai_tai.brokerdecides1_collect import _record_symbol_unknown_changes, webull_pages
from project_mai_tai.brokerdecides1_native import webull_book
from project_mai_tai.webull_open_orders import open_order_page

ACCOUNT = "CONTROLLED-account"


def row(symbol="PCLA", *, native_id="native-1", client_id="own-1", status="WORKING"):
    return {"account_id": ACCOUNT, "order_id": native_id, "client_order_id": client_id,
            "combo_type": "NORMAL", "items": [{"symbol": symbol, "side": "BUY",
            "qty": "36", "filled_qty": "0", "order_status": status}]}


def page(rows, more=False):
    return {"orders": rows, "hasNext": more, "pageSize": len(rows)}


def book(pages, native_account=ACCOUNT):
    return webull_book(account="live:webull", native_account=native_account,
        position_pages=[{"holdings": [], "hasNext": False}], order_pages=pages,
        wire_started_ms=1000, wire_completed_ms=1100,
        scope="all_native_working_including_previous_sessions", complete=True,
        order_request_account=native_account, positions_request_account=native_account)


def decisions(value):
    other = NativeAccountBook("live:schwab", "schwab", 1000, 1100,
                              frozenset(), frozenset(), True, "CONTROLLED-flat")
    snapshot = AdmissionSnapshot("CONTROLLED", 1, 1000, 1100, (value, other),
                                 frozenset(), frozenset(), True, "CONTROLLED")
    # Exercise the actual running-consumer wire contract, not a stubbed gate.
    restored = decode(encode(snapshot))
    return {symbol: assess(restored, accounts=("live:webull", "live:schwab"),
            symbol=symbol, segment=1, now_ms=1200, max_age_ms=20000)
            for symbol in ("PCLA", "OTHER", "MANUAL")}


def test_own_open_rest_crosses_all_three_gates_and_only_holds_its_symbol():
    calls = []
    def fetch(cursor):
        calls.append(cursor)
        return 200, page([row()])
    pages = webull_pages(fetch, account_id=ACCOUNT, field="orders", page_size=100,
                        cursor_keys=("client_order_id",), order_request_account=ACCOUNT)
    value = book(pages)
    assert calls == [""] and value.complete and value.working == frozenset({"PCLA"})
    result = decisions(value)
    assert not result["PCLA"].allowed and result["PCLA"].reason == "native_working_order"
    assert result["OTHER"].allowed


def test_manual_order_without_our_client_id_is_symbol_unknown_not_account_unknown():
    manual = row("MANUAL")
    manual.pop("client_order_id")
    decoded, _, _ = open_order_page(page([manual]), native_account=ACCOUNT, request_account=ACCOUNT)
    assert decoded[0].unknown_reason == "order_identity"
    value = book([page([manual])])
    assert value.complete and value.reason == "Webull_symbol_scoped_UNKNOWN"
    assert not decisions(value)["MANUAL"].allowed and decisions(value)["OTHER"].allowed


def test_two_page_account_census_uses_wire_cursor_and_keeps_both_symbols():
    calls = []
    def fetch(cursor):
        calls.append(cursor)
        return 200, (page([row(client_id="cursor-one")], True) if not cursor else
                     page([row("MANUAL", native_id="native-2", client_id="cursor-two")]))
    pages = webull_pages(fetch, account_id=ACCOUNT, field="orders", page_size=100,
                        cursor_keys=("client_order_id",), order_request_account=ACCOUNT)
    assert calls == ["", "cursor-one"]
    assert book(pages).complete and book(pages).working == frozenset({"PCLA", "MANUAL"})


@pytest.mark.parametrize("change", [
    {"order_status": "MYSTERY"}, {"status": "FILLED"}, {"qty": True},
    {"qty": "NaN"}, {"qty": "Infinity"}, {"qty": "0"}, {"filled_qty": "-1"},
    {"filled_qty": "37"}, {"filledQty": "2"}, {"side": {}}, {"accountId": "FOREIGN"},
])
def test_malformed_readable_item_holds_only_its_symbol(change):
    bad = row()
    bad["items"][0].update(change)
    value = book([page([bad])])
    assert value.complete and value.reason == "Webull_symbol_scoped_UNKNOWN"
    assert not decisions(value)["PCLA"].allowed and decisions(value)["OTHER"].allowed


@pytest.mark.parametrize("change", [
    {"orderId": "conflict"}, {"clientOrderId": "conflict"},
    {"accountId": "FOREIGN"}, {"combo_type": "MASTER"}, {"combo_type": {}},
    {"children": [{"symbol": "not-a-child-proof"}]},
])
def test_outer_identity_or_combo_ambiguity_is_a_symbol_hold(change):
    bad = row()
    bad.update(change)
    value = book([page([bad])])
    assert value.complete and value.reason == "Webull_symbol_scoped_UNKNOWN"
    assert not decisions(value)["PCLA"].allowed and decisions(value)["OTHER"].allowed


def test_multiple_readable_items_hold_every_symbol_without_decoding_combo_semantics():
    bad = row()
    bad["items"].append(dict(bad["items"][0], symbol="MANUAL"))
    assert book([page([bad])]).working == frozenset({"PCLA", "MANUAL"})
    assert decisions(book([page([bad])]))["OTHER"].allowed


@pytest.mark.parametrize("bad", [None, {}, {"items": []}, {"items": [{"symbol": None}]},
                                  {"items": [{"symbol": "PCLA"}, {}]}])
def test_any_unlocated_symbol_keeps_whole_book_unknown(bad):
    with pytest.raises(ValueError, match="unlocated_symbol_UNKNOWN"):
        book([page([row(), bad])])


def test_duplicate_native_id_across_pages_holds_both_symbols_even_if_first_terminal():
    first = row(status="CANCELLED", client_id="cursor-one")
    second = row("MANUAL", client_id="cursor-two")
    value = book([page([first], True), page([second])])
    assert value.complete and value.working == frozenset({"PCLA", "MANUAL"})


@pytest.mark.parametrize("bad", [
    {"orders": []}, {"orders": [], "hasNext": "false"},
    {"orders": [], "hasNext": False},
    {"orders": [], "hasNext": False, "pageSize": True},
    {"orders": [], "hasNext": False, "pageSize": 0, "error": "CONTROLLED"},
    {"orders": [], "hasNext": False, "pageSize": 0, "account_id": "FOREIGN"},
    {"orders": [], "hasNext": False, "has_next": True, "pageSize": 0},
])
def test_invalid_envelope_never_proves_flat(bad):
    with pytest.raises(ValueError):
        book([bad])


@pytest.mark.parametrize("failure", ["missing_final", "cycle", "foreign_request", "http", "cursor_alias"])
def test_incomplete_acquisition_fails_whole_account(failure):
    def fetch(cursor):
        item = row(client_id="same")
        if failure == "cursor_alias":
            item["clientOrderId"] = "different"
        return (503 if failure == "http" else 200), page([item], True)
    with pytest.raises(ValueError):
        if failure == "missing_final":
            book([page([row()], True)])
        else:
            webull_pages(fetch, account_id=ACCOUNT, field="orders", page_size=100,
                cursor_keys=("client_order_id",),
                order_request_account="FOREIGN" if failure == "foreign_request" else ACCOUNT)


def test_terminal_empty_after_continuation_and_old_empty_contract():
    assert book([page([row(client_id="cursor")], True), page([])]).complete
    empty = book([page([])])
    assert empty.complete and not empty.working and decisions(empty)["OTHER"].allowed


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"])
def test_proven_terminal_single_leg_does_not_create_working_hold(status):
    value = book([page([row(status=status)])])
    assert value.complete and not value.working


def test_exact_retained_detail_rows_under_labelled_controlled_list_wrapper():
    path = Path(os.environ["MAI_TAI_P0_REAL_ORDER_RECORDS"])
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == "4451beda582a492a2371b84d07ffbf7b38107d8507f2a4c56b214988e69bbb1d"
    records = [json.loads(line) for line in data.splitlines()]
    assert len(records) == 6
    actual_account = records[0]["account_id"]
    decoded, more, _ = open_order_page(page(records), native_account=actual_account,
                                      request_account=actual_account)
    assert not more and len(decoded) == 6
    assert decoded[0].unknown_reason == "combo_scope"  # MASTER is not child-absence proof.
    assert all(v.unknown_reason is None and v.working is False for v in decoded[1:])
    pcla = deepcopy(records[1])
    pcla["items"][0]["order_status"] = "WORKING"  # CONTROLLED status, never live evidence.
    decoded, _, _ = open_order_page(page([pcla]), native_account=actual_account,
                                    request_account=actual_account)
    assert decoded[0].working and decoded[0].symbols == frozenset({"PCLA"})


def test_real_pcla_row_controlled_working_status_fails_b2_collector_then_fixed_path_holds_symbol():
    source = Path(os.environ["MAI_TAI_P0_BASELINE_COLLECTOR"])
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "37b94fbb3809fa5566a1c61b9d80604f83d1724e9b446392fc71b28715aa72a5"
    baseline = runpy.run_path(str(source))
    records = Path(os.environ["MAI_TAI_P0_REAL_ORDER_RECORDS"]).read_bytes()
    assert hashlib.sha256(records).hexdigest() == "4451beda582a492a2371b84d07ffbf7b38107d8507f2a4c56b214988e69bbb1d"
    pcla = json.loads(records.splitlines()[1])
    assert pcla["items"][0]["symbol"] == "PCLA"
    pcla["items"][0]["order_status"] = "WORKING"  # Explicit CONTROLLED change only.
    account = pcla["account_id"]
    controlled_page = dict(page([pcla]), pageSize=0)  # CONTROLLED: reach the old nonempty guard.
    args = dict(account_id=account, field="orders", page_size=100,
                cursor_keys=("client_order_id",), order_request_account=account)
    with pytest.raises(ValueError, match="Webull_nonempty_item_model_UNKNOWN"):
        baseline["webull_pages"](lambda cursor: (200, controlled_page), **args)
    pages = webull_pages(lambda cursor: (200, controlled_page), **args)
    value = book(pages, native_account=account)
    assert value.complete and value.working == frozenset({"PCLA"})
    assert not decisions(value)["PCLA"].allowed and decisions(value)["OTHER"].allowed


@pytest.mark.parametrize("status", ["PARTIAL_FILLED", "PARTIAL FILLED", "PARTIALLY_FILLED"])
def test_partial_order_is_working_even_if_most_quantity_has_filled(status):
    partial = row(status=status)
    partial["items"][0]["filled_qty"] = "35"
    assert book([page([partial])]).working == frozenset({"PCLA"})


def test_page_count_row_cap_and_acquisition_time_are_bounded():
    with pytest.raises(ValueError, match="page_cap_UNKNOWN"):
        webull_pages(lambda cursor: (200, page([row(client_id=cursor + "x")], True)),
            account_id=ACCOUNT, field="orders", page_size=100,
            cursor_keys=("client_order_id",), order_request_account=ACCOUNT)
    with pytest.raises(ValueError, match="continuation_UNKNOWN"):
        book([page([row()] * 101)])
    with pytest.raises(ValueError, match="acquisition_budget_UNKNOWN"):
        webull_pages(lambda cursor: pytest.fail("must stop before request"),
            account_id=ACCOUNT, field="orders", page_size=100,
            cursor_keys=("client_order_id",), order_request_account=ACCOUNT,
            clock=iter([0, 5]).__next__)


def test_unknown_diagnostics_emit_once_per_account_symbol_reason_change(caplog):
    adapter = SimpleNamespace()
    caplog.set_level("INFO")
    for state in ({"PCLA": ("status_unknown",)}, {"PCLA": ("status_unknown",)},
                  {"PCLA": ("order_identity",)}, {}, {}, {"PCLA": ("order_identity",)}):
        _record_symbol_unknown_changes(adapter, "live:webull", state)
    events = [r for r in caplog.records if "NATIVE-SYMBOL-STATE" in r.getMessage()]
    assert len(events) == 4
    assert [r.args[2] for r in events] == ["UNKNOWN", "UNKNOWN", "DECODED", "UNKNOWN"]
    _record_symbol_unknown_changes(adapter, "live:other", {"PCLA": ("order_identity",)})
    assert len([r for r in caplog.records if "NATIVE-SYMBOL-STATE" in r.getMessage()]) == 5


def test_observer_receives_complete_unknown_set_and_failure_does_not_clear_it():
    seen = []
    bad = row()
    bad["items"][0]["order_status"] = "UNRECOGNIZED"
    args = dict(account="live:webull", native_account=ACCOUNT,
        position_pages=[{"holdings": [], "hasNext": False}], order_pages=[page([bad])],
        wire_started_ms=1000, wire_completed_ms=1100,
        scope="all_native_working_including_previous_sessions", complete=True,
        order_request_account=ACCOUNT, positions_request_account=ACCOUNT,
        symbol_unknown_observer=seen.append)
    value = webull_book(**args)
    assert value.complete and seen == [{"PCLA": ("status_unknown",)}]
    assert not decisions(value)["PCLA"].allowed
    seen.clear()
    args["position_pages"] = [{"holdings": None, "hasNext": False}]
    with pytest.raises(ValueError):
        webull_book(**args)
    assert seen == []  # An incomplete read cannot replace the diagnostic baseline.
