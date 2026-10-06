"""October6 recorded all-date census, not a runtime ownership release."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import ticket_inventory as policy
import attended
from types import SimpleNamespace


def recorded():
    return [json.loads(line) for line in Path(__file__).with_name(
        "TICKET_CENSUS_2026-10-06_1614ET.jsonl").read_text().splitlines()
        if line.startswith("{")]


def test_all_103_recorded_tickets_are_informational_with_zero_in_flight():
    rows = recorded()
    result = policy.require_idle(rows)
    assert result["total"] == 103
    assert result["phases"] == dict(refused=80, expired=11, filled=10, held_unknown=2)
    assert result["in_flight"] == []
    assert {row["symbol"] for row in result["tickets"] if row["phase"] == "held_unknown"} == {"AIXI", "XHG"}
    assert rows == recorded()  # Admission never edits the unknown tickets.


@pytest.mark.parametrize("phase", ["requested", "price_wait", "submitting"])
def test_each_operator_named_in_flight_phase_blocks(phase):
    rows = recorded()
    rows[0]["payload"]["phase"] = phase
    with pytest.raises(ValueError, match="requested/price_wait/submitting"):
        policy.require_idle(rows)


@pytest.mark.parametrize("field,value", [("phase", "nonsense"), ("revision", None), ("slot", "other"), ("old", {})])
def test_unreadable_ticket_shape_blocks(field, value):
    rows = recorded()
    rows[0]["payload"][field] = value
    with pytest.raises(ValueError):
        policy.require_idle(rows)


def test_post_start_accounting_changes_do_not_require_payload_byte_equality():
    before = recorded()
    after = deepcopy(before)
    row = next(row for row in after if row["payload"]["phase"] == "held_unknown")
    row["payload"].update(phase="expired", revision=row["payload"]["revision"] + 1, reason="window_closed")
    result = policy.stable_bindings(before, after)
    assert result["phases"]["expired"] == 12
    assert result["phases"]["held_unknown"] == 1


@pytest.mark.parametrize("field", ["old", "slot", "segment_id", "replacement"])
def test_identity_or_replacement_mutation_still_blocks(field):
    before = recorded()
    after = deepcopy(before)
    after[0]["payload"][field] = "changed"
    with pytest.raises(ValueError):
        policy.stable_bindings(before, after)


def test_missing_identity_and_finished_ticket_revival_block():
    before = recorded()
    with pytest.raises(ValueError, match="identities"):
        policy.stable_bindings(before, before[1:])
    after = deepcopy(before)
    after[0]["payload"]["phase"] = "clear"
    with pytest.raises(ValueError, match="revived"):
        policy.stable_bindings(before, after)


def test_duplicate_and_backwards_revision_block():
    before = recorded()
    with pytest.raises(ValueError, match="duplicate"):
        policy.require_idle(before + before[:1])
    after = deepcopy(before)
    after[0]["payload"]["revision"] -= 1
    with pytest.raises(ValueError, match="backwards"):
        policy.stable_bindings(before, after)


def test_unchanged_schema_declared_in_both_reports():
    root = Path(__file__).parent
    assert '"--no-schema-change"' in (root / "post_proof.py").read_text()
    assert "--no-schema-change" in (root / "gate_patch.py").read_text()
    assert "ticket_inventory.py" in (root / "make_release.py").read_text()


@pytest.fixture
def sql_path(monkeypatch, tmp_path):
    import sqlalchemy
    import project_mai_tai.settings
    capture = dict(rows=recorded(), total=103, buys=[])
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def exec_driver_sql(self, sql):
            assert sql.startswith(("SET TRANSACTION", "SET LOCAL"))
        def rollback(self):
            pass
        def execute(self, query, params):
            query = str(query)
            if "current_database()" in query:
                rows = [dict(database=capture.get("database", "project_mai_tai"),
                             server_address=capture.get("server_address", "127.0.0.1"))]
            elif "alembic_version" in query:
                rows = [dict(version_num="20261005_0022")]
            elif "information_schema.columns" in query:
                rows = [dict(column_name="entry_order_id", data_type="uuid", character_maximum_length=None, is_nullable="YES"),
                        dict(column_name="entry_client_order_id", data_type="character varying", character_maximum_length=128, is_nullable="YES")]
            elif "count(*)" in query:
                rows = [dict(total=capture["total"])]
            elif "dashboard_snapshots" in query:
                assert "LIMIT 1025" in query
                rows = capture["rows"]
            else:
                rows = capture["buys"]
            return SimpleNamespace(mappings=lambda: rows)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: SimpleNamespace(connect=Connection, dispose=lambda: None))
    monkeypatch.setattr(project_mai_tai.settings, "Settings", lambda **k: SimpleNamespace(database_url="controlled://not-production"))
    real = attended.Real(tmp_path, {}, tmp_path)
    return real, capture


def test_actual_runner_sql_accepts_complete_103_and_accounted_restart_change(sql_path):
    real, capture = sql_path
    real.db_before = real.sql()
    capture["rows"] = deepcopy(capture["rows"])
    row = next(row for row in capture["rows"] if row["payload"]["phase"] == "held_unknown")
    row["payload"].update(phase="expired", revision=row["payload"]["revision"] + 1)
    assert len(real.sql("2026-10-06T20:10:00Z")["tickets"]) == 103


@pytest.mark.parametrize("phase", ["requested", "price_wait", "submitting"])
def test_actual_runner_sql_blocks_each_in_flight_phase(sql_path, phase):
    real, capture = sql_path
    capture["rows"][0]["payload"]["phase"] = phase
    with pytest.raises(attended.Stop, match="requested/price_wait/submitting"):
        real.sql()


def test_actual_runner_sql_missing_capture_and_new_buy_activity_block(sql_path):
    real, capture = sql_path
    real.db_before = real.sql()
    capture["buys"] = [dict(id="controlled-new-buy")]
    with pytest.raises(attended.Stop, match="entry activity"):
        real.sql("2026-10-06T20:10:00Z")
    capture["total"] = 104
    with pytest.raises(attended.Stop, match="incomplete"):
        real.sql()


@pytest.mark.parametrize("address", [None, "127.0.0.1", "127.0.0.1/32", "::1", "::1/128"])
def test_real_sql_accepts_only_exact_canonical_local_postgres_address(sql_path, address):
    real, capture = sql_path
    capture["server_address"] = address
    assert len(real.sql()["tickets"]) == 103


@pytest.mark.parametrize("address", ["::2/128", "192.0.2.1/32", "127.0.0.1/16", "::1/64", "unknown"])
def test_real_sql_nonlocal_or_unreadable_address_still_blocks(sql_path, address):
    real, capture = sql_path
    capture["server_address"] = address
    with pytest.raises(attended.Stop):
        real.sql()


def test_real_sql_wrong_database_never_admitted_by_address_normalization(sql_path):
    real, capture = sql_path
    capture["database"] = "foreign"
    with pytest.raises(attended.Stop, match="database name"):
        real.sql()
