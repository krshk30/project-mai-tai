"""CONTROLLED lab status fix; every other proof guard remains bound to accepted 9ee."""
import ast
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.oms import wbexit2
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk


BASE = "CONTROLLED-geometry-protect"
QTY = Decimal("10")


def reports():
    return [ExecutionReport("accepted", BASE + suffix, broker_order_id="native-" + suffix,
        symbol="CONTROLLED", side="sell", intent_type="close", quantity=QTY,
        filled_quantity=Decimal(0), reported_at=datetime(2026, 10, 30, tzinfo=UTC),
        origin="broker", metadata={"wbexit2_geometry": json.dumps({field: price,
            "order_status": "SUBMITTED"})})
        for suffix, field, price in (("T", "limit_price", "10.20"), ("S", "stop_price", "9.50"))]


def original():
    source = Path(__file__).parents[1] / "fixtures/wbexit_geometry_original_9ee.py.txt"
    namespace = {"Decimal": Decimal, "_now": wbexit2._now}
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    return namespace["original_geometry"]


def result(function, rows):
    try:
        return "RETURN", function(rows, BASE, QTY, "CONTROLLED")
    except Exception as exc:
        return type(exc).__name__, str(exc)
@pytest.mark.parametrize("change,predicate", [
    ("good", None), ("child_count", "child_count"),
    ("same_native", "distinct_broker_ids"), ("native_missing", "broker_order_id_present"),
    ("missing_geometry", "geometry_present"), ("null_geometry", "geometry_present"),
    ("origin", "origin_broker"),
    ("event", "event_type_accepted"), ("quantity", "quantity_equals_original"),
    ("fill", "filled_quantity_zero"), ("missing_status", "statuses_present"),
    ("submitted", None), ("pending", None),
    ("conflicting_status", "status"), ("missing_price", "price_present"),
    ("nan_price", "price_finite"), ("zero_price", "price_positive"),
    ("conflicting_price", "price_aliases_equal"), ("subtick_price", "price_tick_rounded"),
], ids=['good', 'child_count', 'same_native', 'native_missing', 'missing_geometry', 'null_geometry', 'origin', 'event', 'quantity', 'fill', 'missing_status', 'submitted', 'pending', 'conflicting_status', 'missing_price', 'nan_price', 'zero_price', 'conflicting_price', 'subtick_price'])
def test_same_outcome_and_one_exact_refusal_receipt(change, predicate, caplog):
    rows = reports()
    geometry = json.loads(rows[0].metadata["wbexit2_geometry"])
    if change == "child_count":
        rows.pop()
    elif change == "same_native":
        rows[1] = replace(rows[1], broker_order_id=rows[0].broker_order_id)
    elif change in {"origin", "event", "quantity", "fill", "native_missing"}:
        mutation = {"native_missing": {"broker_order_id": None}, "origin": {"origin": "unknown"}, "event": {"event_type": "cancelled"},
                    "quantity": {"quantity": Decimal(9)},
                    "fill": {"filled_quantity": Decimal(1), "broker_fill_id": "native-T:1",
                             "fill_price": Decimal("10.20")}}[change]
        rows[0] = replace(rows[0], **mutation)
    elif change == "missing_status":
        geometry.pop("order_status")
    elif change in {"submitted", "pending"}:
        geometry["order_status"] = change.upper()
    elif change == "conflicting_status":
        geometry["status"] = "PENDING"
    elif change == "missing_price":
        geometry.pop("limit_price")
    elif change in {"nan_price", "zero_price", "subtick_price"}:
        geometry["limit_price"] = {"nan_price": "NaN", "zero_price": "0",
                                   "subtick_price": "10.201"}[change]
    elif change == "conflicting_price":
        geometry["limitPrice"] = "10.21"
    rows[0] = replace(rows[0], metadata={} if change == "missing_geometry" else
        {"wbexit2_geometry": "null" if change == "null_geometry" else json.dumps(geometry)})
    expected = result(original(), rows)
    with caplog.at_level(logging.WARNING, logger=wbexit2.__name__):
        actual = result(wbexit2.original_geometry, rows)
    if change in {"good", "submitted", "pending"}:
        assert expected == ("ValueError", "wbexit2_original_WORKING_geometry_UNKNOWN")
        assert actual == ("RETURN", (Decimal("10.20"), Decimal("9.50")))
    else:
        assert actual == expected
    messages = [r.getMessage() for r in caplog.records if "[WBEXIT2-GEOMETRY-REFUSED]" in r.getMessage()]
    assert len(messages) == int(predicate is not None)
    if predicate:
        message, = messages
        assert f"base={BASE}" in message and f"predicate={predicate} " in message
        assert "\n" not in message
        details = json.loads(message.split(" details=", 1)[1])
        if change not in {"child_count", "same_native"}:
            assert "suffix=T " in message
            for key in ("origin", "event_type", "quantity", "filled_quantity"):
                assert f"{key}={getattr(rows[0], key)} " in message
            assert "statuses=" in message and "price_fields=" in message
            assert f"geometry_present={change not in {'missing_geometry', 'null_geometry'}} " in message
            assert details["broker_order_id"] == rows[0].broker_order_id


def test_logging_failure_preserves_original_refusal(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("CONTROLLED logging failure")
    monkeypatch.setattr(logging.getLogger(wbexit2.__name__), "warning", fail)
    rows = reports()
    geometry = json.loads(rows[0].metadata["wbexit2_geometry"])
    geometry["order_status"] = "UNKNOWN"
    rows[0] = replace(rows[0], metadata={"wbexit2_geometry": json.dumps(geometry)})
    assert result(wbexit2.original_geometry, rows) == result(original(), rows)


def test_original_guard_body_changes_only_working_status_vocabulary():
    old = ast.parse((Path(__file__).parents[1] / "fixtures/wbexit_geometry_original_9ee.py.txt").read_text())
    new = ast.parse(Path(wbexit2.__file__).read_text())
    function = next(n for n in new.body if isinstance(n, ast.FunctionDef) and n.name == "original_geometry")
    class RemoveDiagnostics(ast.NodeTransformer):
        def visit_UnaryOp(self, node):
            if (isinstance(node.op, ast.Not) and isinstance(node.operand, ast.Call)
                    and isinstance(node.operand.func, ast.Name)
                    and node.operand.func.id == "_working_geometry_statuses"):
                return ast.Compare(left=ast.Call(func=ast.Name(id="set", ctx=ast.Load()),
                    args=[ast.Name(id="statuses", ctx=ast.Load())], keywords=[]),
                    ops=[ast.NotEq()], comparators=[ast.Set(elts=[ast.Constant(value="WORKING")])])
            return self.generic_visit(node)
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                if node.value.func.id == "_log_original_geometry_refused":
                    return None
            return self.generic_visit(node)
    assert ast.dump(RemoveDiagnostics().visit(function), include_attributes=False) == ast.dump(old.body[0], include_attributes=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["limit_price", "limitPrice", "stop_price", "stopPrice", "status", "order_status", "orderStatus"])
async def test_suppressed_geometry_reports_exact_raw_item_conflict_without_authority(alias, monkeypatch, caplog, fake_sdk):
    from copy import deepcopy
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    from project_mai_tai.broker_adapters import webull_exit_census as census_module
    from tests.unit.test_wbexit2_adapter_contract import adapter_with
    from tests.unit.test_webull_adapter import _FakeClient
    from tests.unit.test_webull_list_primary import Clock
    rows = []
    for suffix, field, price in (("T", "limit_price", "10.20"), ("S", "stop_price", "9.50")):
        coid = WebullBrokerAdapter._combo_leg_coid(BASE, suffix)
        rows.append({"client_order_id": coid, "order_id": "native-" + coid, "account_id": "ACC1",
            "items": [{"symbol": "AIFA", "side": "SELL", "qty": "10", "filled_qty": "0",
                       "order_status": "SUBMITTED", field: price}]})
    item = rows[0]["items"][0]
    item[alias] = "SUBMITTED" if alias in {"status", "order_status", "orderStatus"} else "10.20"
    rows[0][alias] = "PENDING" if alias in {"status", "order_status", "orderStatus"} else "10.21"
    before = deepcopy(rows)
    client = _FakeClient({"today": {"orders": rows, "has_next": False}})
    adapter = adapter_with(client)
    reader = census_module.ExitCensusReader(adapter._query_budget, 2., clock=Clock())
    monkeypatch.setattr(census_module, "shared_exit_census", lambda *_args: reader)
    result = await adapter.confirm_exit_pair_from_today_list(broker_account_name="live:orb", symbol="AIFA",
        base_client_order_id=BASE, quantity=QTY, original_attempts=[{"base": BASE, "quantity": "10"}])
    assert result.outcome == "reserved" and client.calls == {"today": 1}
    assert rows == before
    target = next(report for report in result.reports if report.client_order_id.endswith("T"))
    assert "wbexit2_geometry" not in target.metadata
    diagnostic = json.loads(target.metadata["wbexit2_geometry_refusal"])
    assert diagnostic["conflicts"] == {alias: {"raw": rows[0][alias], "item": item[alias]}}
    assert result_of_geometry(result.reports, "AIFA") == result_of_geometry(result.reports, "AIFA", baseline=True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=wbexit2.__name__):
        actual = result_of_geometry(result.reports, "AIFA")
    assert actual == ("ValueError", "wbexit2_original_WORKING_geometry_UNKNOWN")
    messages = [record.getMessage() for record in caplog.records if "[WBEXIT2-GEOMETRY-REFUSED]" in record.getMessage()]
    message, = messages
    assert "predicate=geometry_present " in message and "geometry_present=False " in message
    details = json.loads(message.split(" details=", 1)[1])
    assert details["suppressed_projection"] == diagnostic
    assert client.calls == {"today": 1}


def result_of_geometry(rows, symbol, *, baseline=False):
    try:
        return "RETURN", (original() if baseline else wbexit2.original_geometry)(rows, BASE, QTY, symbol)
    except Exception as exc:
        return type(exc).__name__, str(exc)
