"""Controlled model/APP-assess contracts, not actual seed, PG or native proof."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from project_mai_tai.brokerdecides1 import assess, decode
from tools.l12_service_boot_replay import prepare_c11929_controlled_native
from tools.l12_original122_branch_adapter import summarize_original122_local_callbacks


ACCOUNTS = {"schwab": "live:schwab_1m_v2", "webull": "live:orb"}
BOOT = 1793001600000


def model(**kwargs):
    return prepare_c11929_controlled_native(accounts=ACCOUNTS, modeled_boot_ms=BOOT, **kwargs)


def snapshot(value):
    item = value["artifacts"]["admission_snapshot"]
    return decode(value["blobs"][item["reference"]])


def test_empty_flat_model_has_four_explicit_nonwire_roles_and_unknown_admission():
    value = model(variant="EMPTY_FLAT")
    native_roles = set(value["artifacts"]) - {"admission_snapshot"}
    assert native_roles == {"schwab_working_orders", "schwab_gross_positions",
        "webull_working_orders", "webull_gross_positions"}
    for role in native_roles:
        item = value["artifacts"][role]
        raw = value["blobs"][item["reference"]]
        assert sha256(raw).hexdigest() == item["sha256"]
        record = json.loads(raw)
        assert record["purpose"] == "CONTROLLED_NOT_NATIVE_RESPONSE"
        assert record["complete"] is False
        assert record["http_status"] is None
        assert record["request_started_ms"] is None
        assert record["response_completed_ms"] is None
        assert record["working_buy_symbols"] == record["gross_position_symbols"] == []
        assert item["clock_domain"] == "MODELED_BOOT_MS_NOT_WIRE_CLOCK"
    state = snapshot(value)
    assert state.local_complete is False
    assert all(book.complete is False for book in state.books)
    assert value["native_permission"] == "UNKNOWN"
    assert value["actual_native_acquisition"] is False


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_one_working_buy_is_retained_exactly_one_provider_and_still_fails_closed(provider):
    value = model(variant="ONE_WORKING_BUY", working_provider=provider, working_symbol="YMAT")
    state = snapshot(value)
    assert [book.provider for book in state.books if "YMAT" in book.working] == [provider]
    assert all(not book.positions for book in state.books)
    decision = assess(state, accounts=tuple(ACCOUNTS.values()), symbol="YMAT", segment=1,
        now_ms=BOOT, max_age_ms=20000)
    assert decision.allowed is False
    assert decision.reason == "snapshot_UNKNOWN"
    assert value["native_permission"] == "UNKNOWN"


def test_fresh_modeled_empty_flat_clock_does_not_grant_actual_entry():
    state = snapshot(model(variant="EMPTY_FLAT"))
    decision = assess(state, accounts=tuple(ACCOUNTS.values()), symbol="YMAT", segment=1,
        now_ms=BOOT, max_age_ms=20000)
    assert decision.allowed is False
    assert decision.reason == "snapshot_UNKNOWN"


@pytest.mark.parametrize("kwargs", [
    dict(variant="EMPTY_FLAT", working_symbol="YMAT"),
    dict(variant="EMPTY_FLAT", working_provider="schwab"),
    dict(variant="ONE_WORKING_BUY"),
    dict(variant="ONE_WORKING_BUY", working_provider="other", working_symbol="YMAT"),
    dict(variant="ONE_WORKING_BUY", working_provider="schwab", working_symbol="NOT_IN_FLEET"),
    dict(variant="UNKNOWN"),
])
def test_no_favorable_variant_or_foreign_symbol_is_invented(kwargs):
    with pytest.raises(ValueError):
        model(**kwargs)


def test_model_preparation_does_not_rewrite_caller_accounts():
    before = deepcopy(ACCOUNTS)
    model(variant="EMPTY_FLAT")
    assert ACCOUNTS == before


@pytest.mark.parametrize("unknown,review", [(0, False), (12, False), (13, True), (122, True)])
def test_strictly_more_than_ten_percent_122_unknown_requires_case_review(unknown, review):
    rows = [dict(case_id="CONTROLLED:" + str(index), callback=(
        "UNRUN_MISSING_RESTORED_OPERANDS" if index < unknown else
        "REAL_FIRST_REST_PREDICATE_EXECUTED_LOCAL_BRANCH_ONLY")) for index in range(122)]
    result = summarize_original122_local_callbacks(rows)
    assert result["local_state_UNKNOWN_count"] == unknown
    assert result["reconstructed_local_predicate_cases"] == 122 - unknown
    assert result["C1_case_review_required"] is review
    assert result["native_asof_UNKNOWN_count"] == 122
    assert result["native_entry_permission"] == "UNKNOWN"
    assert result["local_predicate_result_is_not_full_BUY_case_PASS"] is True


def test_application_exception_is_unknown_not_reconstructed_or_omitted():
    result = summarize_original122_local_callbacks([
        dict(case_id="CONTROLLED:exception", callback="APPLICATION_CALLBACK_ERROR_NOT_ACCEPTANCE")])
    assert result["local_state_UNKNOWN_case_ids"] == ["CONTROLLED:exception"]
    assert result["reconstructed_local_predicate_cases"] == 0
