"""Core drift cursor and visibility controls on isolated real PostgreSQL."""

import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_drift_core_query_parity as control
from tests.unit.test_drift_cache_projection_equivalence import make_lane

sessions = runtime.sessions


@pytest.fixture
def lane(sessions):
    return make_lane(sessions)


@pytest.mark.parametrize("symbol", [None, "DKI"])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_pg_empty_core_statement(lane, symbol, quote):
    control.test_empty_book_uses_one_core_statement(lane, symbol, quote)


@pytest.mark.parametrize("eligible", [False, True])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_pg_core_cursor_setup(lane, monkeypatch, eligible, quote):
    control.test_core_cursor_reads_skip_orm_setup(lane, monkeypatch, eligible, quote)


@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
@pytest.mark.parametrize("changes,expected", [
    ({"symbol": "DKI"}, True),
    ({"symbol": "FLYE", "side": "sell"}, True),
    ({"symbol": "VEEA", "intent_type": "OPEN"}, True),
    ({"account": "inactive"}, False),
    ({"intent_type": "close"}, False),
    ({"intent_type": "scale"}, False),
    ({"status": "filled"}, False),
    ({"payload": {"order_type": "limit", "limit_price": "2.55",
                  "stop_guard": " TRUE "}}, False),
    ({"payload": {"order_type": "market", "limit_price": "2.55"}}, False),
    ({"column_type": "market"}, True),
])
def test_pg_actual_order_scope(lane, quote, changes, expected):
    control.test_actual_order_scope_matches_frozen_orm_control(lane, quote, changes, expected)


@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("change", [
    "order_status", "intent_type", "account_active", "account_name",
    "strategy_code", "quantity", "metadata",
])
def test_pg_dirty_visibility(lane, autoflush, change):
    control.test_dirty_columns_follow_autoflush_not_identity_map(lane, autoflush, change)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_pending_new_rows(lane, autoflush):
    control.test_unflushed_order_with_uncommitted_parent_visibility(lane, autoflush)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_pending_delete(lane, autoflush):
    control.test_pending_delete_visibility(lane, autoflush)


def test_pg_no_autoflush_status(lane):
    control.test_no_autoflush_context_retains_uncommitted_order_status(lane)


def test_pg_no_autoflush_candidate_fields(lane):
    control.test_no_autoflush_context_preserves_candidate_fields(lane)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_pending_flush_failure(lane, autoflush):
    control.test_empty_book_preserves_pending_flush_failure(lane, autoflush)
