"""Lab controls. C1-reported CHOW shape is CONTROLLED until raw reports are supplied."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.webull import _ACCEPTED_STATUSES
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.oms import wbexit2
from tests.unit.test_wbexit2_geometry_logging import original, reports, result


@pytest.mark.parametrize("status", ["SUBMITTED", "PENDING", "ACCEPTED", "QUEUED", "WORKING"])
@pytest.mark.parametrize("alias", ["status", "order_status", "orderStatus"])
def test_each_adapter_accepted_raw_status_and_alias_preserves_geometry(status, alias):
    rows = reports()
    changed = []
    for row in rows:
        geometry = json.loads(row.metadata["wbexit2_geometry"])
        geometry.pop("order_status")
        geometry[alias] = status
        changed.append(replace(row, metadata={"wbexit2_geometry": json.dumps(geometry)}))
    before = deepcopy(changed)
    assert result(wbexit2.original_geometry, changed) == ("RETURN", (Decimal("10.20"), Decimal("9.50")))
    assert changed == before
    if status == "WORKING":
        assert result(original(), changed) == result(wbexit2.original_geometry, changed)
    else:
        assert result(original(), changed) == ("ValueError", "wbexit2_original_WORKING_geometry_UNKNOWN")


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "FILLED", "PARTIAL_FILLED",
    "PARTIALLY_FILLED", "FAILED", "REJECTED", "UNKNOWN", "", None, True])
def test_every_nonworking_raw_status_remains_refused(status):
    rows = reports()
    geometry = json.loads(rows[0].metadata["wbexit2_geometry"])
    geometry["order_status"] = status
    rows[0] = replace(rows[0], metadata={"wbexit2_geometry": json.dumps(geometry)})
    assert result(wbexit2.original_geometry, rows) == result(original(), rows)
    assert result(wbexit2.original_geometry, rows)[0] == "ValueError"


@pytest.mark.parametrize("status", ["PENDING", "ACCEPTED", "QUEUED", "WORKING"])
def test_different_accepted_aliases_still_conflict(status):
    rows = reports()
    geometry = json.loads(rows[0].metadata["wbexit2_geometry"])
    geometry["status"] = status
    rows[0] = replace(rows[0], metadata={"wbexit2_geometry": json.dumps(geometry)})
    assert result(wbexit2.original_geometry, rows) == result(original(), rows)
    assert result(wbexit2.original_geometry, rows)[0] == "ValueError"


def test_vocabulary_uses_exact_adapter_set_and_unknown_mapping_never_grants_proof():
    assert _ACCEPTED_STATUSES == {"SUBMITTED", "PENDING", "WORKING", "ACCEPTED", "QUEUED"}
    assert not wbexit2._working_geometry_statuses([])
    assert not wbexit2._working_geometry_statuses(["UNKNOWN"])
    assert wbexit2._working_geometry_statuses(["SUBMITTED", "SUBMITTED"])
    assert not wbexit2._working_geometry_statuses(["SUBMITTED", "PENDING"])


def test_CONTROLLED_C1_reported_CHOW_probe_shape_old_refused_new_geometry_only():
    # This binds the exact base/quantity/prices C1 reported, not missing native IDs or timestamps.
    # It is NOT the requested original real-data replay; that remains pending raw C1 reports.
    base = "schwab_1m_v2-CHOW-protect-3f84a37f10f2"
    rows = []
    for row, suffix, field, price in zip(reports(), ("T", "S"),
            ("limit_price", "stop_price"), ("4.92", "4.31")):
        rows.append(replace(row, client_order_id=base + suffix, symbol="CHOW",
            broker_order_id="CONTROLLED-unretained-native-" + suffix,
            quantity=Decimal(64), reported_at=datetime(2026, 10, 30, tzinfo=UTC),
            metadata={"wbexit2_geometry": json.dumps({field: price, "order_status": "SUBMITTED"})}))
    with pytest.raises(ValueError, match="wbexit2_original_WORKING_geometry_UNKNOWN"):
        original()(rows, base, Decimal(64), "CHOW")
    assert wbexit2.original_geometry(rows, base, Decimal(64), "CHOW") == (Decimal("4.92"), Decimal("4.31"))


def retained_projection():
    path = Path(__file__).parents[1] / "fixtures/chow_raw_census_1030.txt"
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == "53357536431eb14e31c99ca3a1af2da2fa5a4ada82bb11a114efe2e1f0ffae0e"
    rows = []
    for line in raw.decode().splitlines():
        if not line.startswith("REPORT "):
            continue
        fields, geometry = line.split(" geometry= ", 1)
        _, coid, event, origin, native, quantity, filled = fields.split()
        rows.append(ExecutionReport(event, coid, broker_order_id=native,
            quantity=Decimal(quantity), filled_quantity=Decimal(filled), origin=origin,
            metadata={"wbexit2_geometry": json.dumps(json.loads(geometry))},
            # These four dataclass fields were NOT printed in the retained census.
            # Explicit controlled bindings permit a projection replay, not original live proof.
            symbol="CHOW", side="sell", intent_type="close",
            reported_at=datetime(2026, 10, 30, tzinfo=UTC)))
    assert len(rows) == 2
    return rows


def test_retained_CHOW_cancelled_projection_never_becomes_working_permission():
    rows = retained_projection()
    base = "schwab_1m_v2-CHOW-protect-3f84a37f10f2"
    for function in (original(), wbexit2.original_geometry):
        with pytest.raises(ValueError, match="wbexit2_original_WORKING_geometry_UNKNOWN"):
            function(rows, base, Decimal(64), "CHOW")


@pytest.mark.parametrize("status", ["SUBMITTED", "PENDING", "ACCEPTED", "QUEUED", "WORKING"])
def test_retained_CHOW_projection_explicitly_varies_status_and_event_like_C1(status):
    base = "schwab_1m_v2-CHOW-protect-3f84a37f10f2"
    rows = []
    for row in retained_projection():
        geometry = json.loads(row.metadata["wbexit2_geometry"])
        geometry["order_status"] = status
        rows.append(replace(row, event_type="accepted",
            metadata={"wbexit2_geometry": json.dumps(geometry)}))
    assert wbexit2.original_geometry(rows, base, Decimal(64), "CHOW") == (Decimal("4.92"), Decimal("4.31"))
    if status == "WORKING":
        assert original()(rows, base, Decimal(64), "CHOW") == (Decimal("4.92"), Decimal("4.31"))
    else:
        with pytest.raises(ValueError, match="wbexit2_original_WORKING_geometry_UNKNOWN"):
            original()(rows, base, Decimal(64), "CHOW")
