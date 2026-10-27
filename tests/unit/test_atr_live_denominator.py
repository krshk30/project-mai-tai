"""NEW CONTROLLED census controls. No APP, PG, provider or actual corpus run."""

import pytest

from tools.atr_live_denominator import COUNTS, census, clock_group, membership, ns, reader, retained


STAMP = "2026-10-12T09:30:00-04:00"


def annotated(**columns):
    return dict(columns, __l12_json_text={"payload": '{ "x":null, "n":1.2300 }'},
                __l12_sql_null_columns=["broker_order_id"], payload={"x": None, "n": 1.23})


def fixture():
    accounts = [annotated(id="a", name="live:orb", provider="webull"),
                annotated(id="b", name="live:schwab_1m_v2", provider="schwab")]
    strategies = [annotated(id="s", code="schwab_1m_v2", execution_mode="live")]
    order = annotated(id="order", broker_account_id="a", strategy_id="s", intent_id="intent",
        symbol="CONTROLLED", side="buy", quantity="100", submitted_at=STAMP)
    fill = annotated(id="fill", broker_account_id="a", strategy_id="s", order_id="order",
        symbol="CONTROLLED", side="buy", quantity="100", price="1.23", filled_at=STAMP)
    intent = annotated(id="intent", broker_account_id="a", strategy_id="s", symbol="CONTROLLED",
        side="buy", created_at=STAMP, intent_type="open")
    tables = dict(broker_accounts=accounts, strategies=strategies,
                  broker_orders=[order], fills=[fill], trade_intents=[intent])

    class ControlledCapture:
        manifest = {"asof_ms": ns("2026-10-24T04:21:44.258Z") // 1000000}
        counts = dict(COUNTS)

        def rows(self, table):
            for i, row in enumerate(tables[table], 1):
                yield row, dict(table=table, part="CONTROLLED/" + table,
                                part_sha256="a" * 64, row_ordinal_1based=i)

    return ControlledCapture(), tables


@pytest.mark.parametrize("value,expected", [
    ("2026-10-12T04:00:00-04:00", "PM_0400_0930"),
    ("2026-10-12T09:29:59.999999999-04:00", "PM_0400_0930"),
    ("2026-10-12T09:30:00-04:00", "RTH_0930_1600"),
    ("2026-10-12T16:00:00-04:00", "AH_1600_2000"),
    ("2026-10-12T20:00:00-04:00", "OUTSIDE_0400_2000"),
    (None, "UNKNOWN_SESSION"),
])
def test_sessions_do_not_discard_ah_or_unknown(value, expected):
    assert clock_group(value)[1] == expected


def test_exact_ns_cutoff_does_not_floor_fractional_future_row():
    cutoff = ns("2026-10-12T13:30:00.000000001Z")
    assert membership("2026-10-12T13:30:00.000000001Z", cutoff) == "IN_WINDOW"
    assert membership("2026-10-12T13:30:00.000000002Z", cutoff) == "UNKNOWN_FUTURE_CLOCK"
    assert ns("2026-10-12T09:30:00-04:00") == ns("2026-10-12T13:30:00Z")


@pytest.mark.parametrize("value", [None, "2026-10-12T09:30:00", "2026-10-12T09:30:00.1234567891Z"])
def test_missing_offset_or_unrepresented_precision_is_unknown(value):
    assert membership(value, ns("2026-10-24T00:00:00Z")) == "UNKNOWN_TIME"


def test_original_json_lexemes_nulls_and_pointers_are_not_reserialized():
    row = annotated(id="raw")
    pointer = {"part_sha256": "f" * 64, "row_ordinal_1based": 4}
    view = retained(row, pointer)
    assert view["original_json_text"]["payload"] == '{ "x":null, "n":1.2300 }'
    assert view["sql_null_columns"] == ["broker_order_id"]
    assert "payload" not in view["scalar_columns"]
    assert view["source"] == pointer


def test_fill_rows_requests_and_filled_orders_are_distinct_counts():
    capture, tables = fixture()
    tables["fills"][0]["quantity"] = "60"
    second = dict(tables["fills"][0], id="partial-2", quantity="40")
    tables["fills"].append(second)
    result = census(capture)
    assert sum(result["order_request_counts"].values()) == 1
    assert sum(result["fill_row_counts"].values()) == 2
    assert sum(result["filled_order_counts_by_fill_session"].values()) == 1
    assert len(result["cases"][0]["fills"]) == 2
    assert result["actual_fills_reproduced"] == 0
    assert result["old_equals_new"] == "UNRUN"
    assert result["changed_order_count"] is None


def test_pre_window_entry_order_is_retained_for_in_window_fill_without_retiming():
    capture, tables = fixture()
    tables["broker_orders"][0]["submitted_at"] = "2026-10-11T19:59:00-04:00"
    result = census(capture)
    assert result["order_request_counts"] == {}
    assert result["cases"][0]["prewindow_parent_retained"] is True
    assert sum(result["fill_row_counts"].values()) == 1


def test_unknown_order_clock_never_drops_original_row():
    capture, tables = fixture()
    tables["broker_orders"][0]["submitted_at"] = None
    result = census(capture)
    assert result["order_count"] == 1
    assert result["uncertain_membership"][0]["reason"] == "UNKNOWN_TIME"
    assert next(iter(result["order_request_counts"])).startswith("UNKNOWN_TIME|")


def test_conflicting_fill_account_stays_unbound_in_denominator():
    capture, tables = fixture()
    tables["fills"][0]["broker_account_id"] = "b"
    result = census(capture)
    assert len(result["unbound_fills"]) == 1
    assert sum(result["fill_row_counts"].values()) == 1
    assert result["filled_order_counts_by_fill_session"] == {}
    assert result["identity_issues"][0]["reason"] == "FILL_ORDER_IDENTITY_UNKNOWN"


def test_intent_without_order_is_not_discarded_or_promoted_to_execution():
    capture, tables = fixture()
    tables["trade_intents"].append(dict(tables["trade_intents"][0], id="rejected-no-order"))
    result = census(capture)
    assert len(result["intent_only_rows"]) == 1
    assert result["intent_only_rows"][0]["raw"]["scalar_columns"]["id"] == "rejected-no-order"
    assert result["order_count"] == 1


def test_nonpositive_fill_is_kept_but_not_counted_as_executed_entry():
    capture, tables = fixture()
    tables["fills"][0]["quantity"] = "0"
    result = census(capture)
    assert sum(result["fill_row_counts"].values()) == 1
    assert result["filled_order_counts_by_fill_session"] == {}
    assert result["identity_issues"][0]["reason"] == "POSITIVE_EXECUTED_QUANTITY_OR_SIDE_UNKNOWN"


def test_live_account_nonlive_strategy_retains_unknown_membership():
    capture, tables = fixture()
    tables["strategies"][0]["execution_mode"] = "paper"
    result = census(capture)
    assert result["order_count"] == 1
    assert any(r["reason"] == "LIVE_ACCOUNT_STRATEGY_MODE_OR_PROVIDER_UNKNOWN"
               for r in result["uncertain_membership"])


def test_source_count_mismatch_refuses_not_partial_census():
    capture, _ = fixture()
    capture.counts = dict(COUNTS, fills=20732)
    with pytest.raises(ValueError, match="exact_five_source_table_counts_required"):
        census(capture)


@pytest.mark.parametrize("field", ["__l12_json_text", "__l12_sql_null_columns"])
def test_original_annotation_missing_refuses_no_reconstruction(field):
    row = annotated(id="raw")
    del row[field]
    with pytest.raises(ValueError, match="original_json_text_and_sql_null_annotations_required"):
        retained(row, {})


def test_foreign_reader_bytes_rejected_before_import(tmp_path):
    candidate = tmp_path / "foreign_reader.py"
    candidate.write_text("raise RuntimeError('MUST_NOT_BE_IMPORTED')\n")
    with pytest.raises(ValueError, match="frozen_shared_reader_source_changed"):
        reader(candidate)


def test_unknown_account_provider_metadata_is_not_dropped_or_called_native():
    capture, tables = fixture()
    tables["broker_accounts"][0]["name"] = None
    tables["broker_accounts"][0]["provider"] = None
    result = census(capture)
    assert result["order_count"] == 1
    assert any("UNKNOWN_ACCOUNT|UNKNOWN_PROVIDER" in k for k in result["order_request_counts"])
    assert any(r["reason"] == "LIVE_ACCOUNT_STRATEGY_MODE_OR_PROVIDER_UNKNOWN"
               for r in result["uncertain_membership"])
    assert result["cases"][0]["original_dispatch_provenance"].startswith("UNKNOWN_")


def test_boolean_fill_quantity_never_counts_as_one_real_share():
    capture, tables = fixture()
    tables["fills"][0]["quantity"] = True
    result = census(capture)
    assert sum(result["fill_row_counts"].values()) == 1
    assert result["filled_order_counts_by_fill_session"] == {}
    assert result["identity_issues"][0]["reason"] == "POSITIVE_EXECUTED_QUANTITY_OR_SIDE_UNKNOWN"
