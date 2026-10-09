"""Same projected drift-read equivalence on isolated real PostgreSQL."""
import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_drift_cache_projection_equivalence as control

sessions = runtime.sessions


@pytest.fixture
def lane(sessions):
    return control.make_lane(sessions)


def test_pg_fourteen_close_orders_are_excluded_before_materialization(lane):
    control.test_fourteen_working_rows_no_candidates_without_any_orm_hydration(lane)


def test_pg_market_open_orders_skip_payload_materialization_and_lookups(lane):
    control.test_market_open_orders_skip_payload_materialization_and_lookup_reads(lane)


@pytest.mark.parametrize("payload_type", ["limit", "LIMIT", "LiMiT", " LIMIT ", "\tlimit\n",
                                         "market", None, 12, ["limit"], {"type": "limit"}])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_pg_cache_type_filter_keeps_existing_python_semantics(lane, payload_type, quote):
    control.test_cache_type_filter_preserves_python_string_and_whitespace_semantics(lane, payload_type, quote)


@pytest.mark.parametrize("quote", [None, {"ask": 3.0, "bid": 2.0}])
def test_pg_candidates_keep_metadata_and_budget(lane, quote):
    from project_mai_tai.db.models import BrokerOrderEvent

    first = control.seed_order(lane, payload={"order_type": "limit", "limit_price": "2.55",
        "fanout_segment_id": "original", "nested": {"a": 1}})
    second = control.seed_order(lane, side="sell", broker_id=False)
    with lane[1]() as session:
        for _ in range(2):
            session.add(BrokerOrderEvent(order_id=first, event_type="rejected", event_source="broker",
                payload={"reason": "Order in state FILLED cannot be canceled"}))
        session.commit()
    actual, _loaded = control.compare(lane, quote=quote, tolerance=0.01)
    by_id = {candidate.order_id: candidate for candidate in actual}
    assert set(by_id) == {first, second}
    assert by_id[first].terminal_cancel_reports == 2
    assert by_id[second].terminal_cancel_reports == 0
    assert by_id[first].existing_metadata['nested'] == "{'a': 1}"


@pytest.mark.parametrize("intent_type", ["open", "OPEN", "oPeN", "Open", " open", "open ", "CLOSE", "sCaLe"])
@pytest.mark.parametrize("quote", [None, {"ask": 3.0}])
def test_pg_join_case_and_whitespace_equivalence(lane, intent_type, quote):
    control.test_join_intent_filter_preserves_existing_case_and_whitespace_rules(lane, intent_type, quote)


@pytest.mark.parametrize("changes", [{"no_intent": True}, {"intent_type": "close"},
    {"intent_type": "scale"}, {"account": "inactive"}, {"status": "filled"},
    {"payload": {"order_type": "limit", "limit_price": "2.55", "stop_guard": " TRUE "}}])
def test_pg_exclusions_stay_closed(lane, changes):
    control.test_exclusions_match_original_read(lane, changes, {"ask": 3.0})
