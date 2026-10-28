"""Authored SYNTH SQLite protocol tests; actual tonight-copy gate is separate."""

import json
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import Strategy
from scripts.momold1_copy_boot_proof import run_copy, sha


@pytest.fixture
def synth_package(tmp_path):
    copy = tmp_path / "SYNTH.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{copy}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Strategy(id=UUID(int=index + 1), code=code, name=f"SYNTH retained {code}",
            execution_mode="paper", metadata_json={"fixture_provenance": "SYNTH_NOT_ACTUAL"})
            for index, code in enumerate(("momentum_30s", "momentum_60s"))])
        session.commit()
    with engine.connect() as connection:
        rows = {table.name: connection.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables}
    engine.dispose()
    manifest = tmp_path / "SYNTH-manifest.json"
    payload = {"kind": "SYNTH_FIXTURE", "sqlite_sha256": sha(copy), "table_rows": rows,
        "captured_at_utc": "2026-10-21T21:00:00+00:00", "next_boot_utc": "2026-10-22T11:00:00+00:00",
        "effective_settings_redacted": {"momentum_paper_enabled": True,
            "market_data_momentum_warning_enabled": True, "momentum_live_enabled": True}}
    manifest.write_text(json.dumps(payload))
    return copy, manifest, payload


def test_synth_next_day_readers_preserve_legacy_ids_and_create_warning_rows(synth_package):
    copy, manifest, _ = synth_package
    original = sha(copy)
    result = run_copy(copy, manifest)
    assert sha(copy) == original  # All application writes happened on a second copy.
    assert result["must3"] == "SYNTH_ONLY_NOT_TONIGHT_ACCEPTANCE"
    assert result["legacy_rows_before"] == result["legacy_rows_after"]
    assert set(result["warning_rows_after"]) == {"momentum_30s_warn", "momentum_60s_warn"}
    assert result["reconciliation"]["status"] == "completed"


@pytest.mark.parametrize("fault", ["missing_table", "wrong_count", "wrong_hash", "not_after_close"])
def test_copy_manifest_refuses_missing_or_unproven_inputs(synth_package, fault):
    copy, manifest, payload = synth_package
    if fault == "missing_table":
        payload["table_rows"].pop("strategies")
    elif fault == "wrong_count":
        payload["table_rows"]["strategies"] += 1
    elif fault == "wrong_hash":
        payload["sqlite_sha256"] = "0" * 64
    else:
        payload.update(kind="ACTUAL_TONIGHT_COPY", coverage="COMPLETE_ALL_APPLICATION_TABLES",
                       captured_at_utc="2026-10-21T13:00:00+00:00")
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        run_copy(copy, manifest)


def test_missing_copy_manifest_is_refused_without_creating_an_empty_fixture(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_copy(tmp_path / "missing.sqlite", tmp_path / "missing-manifest.json")


def test_missing_copied_database_is_refused_without_creating_an_empty_fixture(tmp_path):
    manifest = tmp_path / "SYNTH-manifest.json"
    manifest.write_text(json.dumps({"kind": "SYNTH_FIXTURE", "sqlite_sha256": "0" * 64}))
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        run_copy(missing, manifest)
    assert not missing.exists()
