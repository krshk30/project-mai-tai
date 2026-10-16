"""Controlled SQL-transaction spy, NOT executed PostgreSQL archive qualification."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from ops.weekmaint import archive, policy
from tests.unit.test_weekmaint1_policy import NOW, retained_row


class Result:
    rowcount = 1

    def __init__(self, value=None):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def all(self):
        return self.value


class TransactionSpy:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.saved = deepcopy((self.engine.run, self.engine.records))
        self.engine.events.append("begin")
        assert list(self.engine.directory.glob("*.staged"))  # CSV fsynced BEFORE DB commit boundary.
        return self

    def __exit__(self, kind, *_args):
        if kind:
            self.engine.run, self.engine.records = self.saved
            self.engine.events.append("rollback")
        else:
            self.engine.events.append("commit")

    def execute(self, statement, parameters=None):
        sql, values = str(statement), parameters or {}
        self.engine.sql.append(sql)
        if "SELECT to_jsonb" in sql:
            assert "FOR UPDATE" in sql
            return Result(self.engine.original)
        if "INSERT INTO weekmaint_runs" in sql:
            if self.engine.run:
                raise ValueError("controlled duplicate operation")
            self.engine.run = {"csv_name": values["name"], "csv_sha256": values["digest"], "row_count": values["count"]}
        elif "INSERT INTO weekmaint_archive" in sql:
            self.engine.records.append({"table": values["table"], "id": values["id"],
                "account": values["account"], "symbol": values["symbol"],
                "source_sha256": values["source_sha256"], "source_json_text": values["source_json_text"],
                "changes_json": values["changes_json"]})
            self.engine.events.append("archive")
        elif sql.startswith("UPDATE public."):
            assert self.engine.records
            self.engine.events.append("terminalize")
            if self.engine.fail_update:
                raise OSError("controlled write failure")
        return Result()


class ReadSpy:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, statement, _parameters):
        return Result(self.engine.records if "FROM weekmaint_archive" in str(statement) else self.engine.run)


class EngineSpy:
    def __init__(self, original, directory):
        self.original, self.directory = original, directory
        self.run, self.records, self.events, self.sql = {}, [], [], []
        self.fail_update = False

    def begin(self):
        return TransactionSpy(self)

    def connect(self):
        return ReadSpy(self)


def plan():
    item = retained_row("broker_orders", existence=NOW.replace(year=2025))
    # Keep the exact original JSON text, including precision, independently of the parsed dict.
    item["source_json_text"] = item["source_json_text"].replace('"1.23456789"', '1.23456789')
    return policy.retirement_plan([item], NOW)[0]


def test_archive_original_and_terminalization_share_transaction_then_publish_csv(tmp_path):
    rows, operation = plan(), UUID(int=90000001)
    engine = EngineSpy(rows[0].before_json, tmp_path)
    result = archive.archive_transaction(engine, operation, rows, tmp_path)
    assert engine.events == ["begin", "archive", "terminalize", "commit"]
    assert engine.records[0]["source_json_text"] == rows[0].before_json
    assert hashlib.sha256((tmp_path / f"{operation}.csv").read_bytes()).hexdigest() == result["csv_sha256"]
    assert not list(tmp_path.glob("*.staged"))
    assert not any("DELETE FROM" in sql or "TRUNCATE" in sql for sql in engine.sql)
    assert any("COALESCE(payload::jsonb" in sql for sql in engine.sql)


def test_terminalization_failure_rolls_back_archive_and_run_but_keeps_uncommitted_csv(tmp_path):
    rows, operation = plan(), UUID(int=90000002)
    engine = EngineSpy(rows[0].before_json, tmp_path)
    engine.fail_update = True
    with pytest.raises(OSError):
        archive.archive_transaction(engine, operation, rows, tmp_path)
    assert engine.events[-1] == "rollback" and not engine.records and not engine.run
    assert not (tmp_path / f"{operation}.csv").exists()
    assert (tmp_path / f"{operation}.csv.staged").exists()


def test_committed_archive_recovers_identical_csv_after_publish_crash(tmp_path, monkeypatch):
    rows, operation = plan(), UUID(int=90000003)
    engine = EngineSpy(rows[0].before_json, tmp_path)
    original = archive.publish
    def crash(*_args):
        raise OSError("controlled crash AFTER commit")
    monkeypatch.setattr(archive, "publish", crash)
    with pytest.raises(OSError):
        archive.archive_transaction(engine, operation, rows, tmp_path)
    assert engine.events[-1] == "commit" and engine.run and len(engine.records) == 1
    monkeypatch.setattr(archive, "publish", original)
    final = archive.recover_committed_csv(engine, operation, tmp_path)
    assert hashlib.sha256(final.read_bytes()).hexdigest() == engine.run["csv_sha256"]
    assert final.stat().st_mode & 0o077 == 0


def test_changed_original_row_refuses_before_terminalization(tmp_path):
    rows = plan()
    engine = EngineSpy(json.dumps({"different": True}), tmp_path)
    with pytest.raises(policy.Refused, match="row_changed"):
        archive.archive_transaction(engine, UUID(int=90000004), rows, tmp_path)
    assert engine.events == ["begin", "rollback"] and not engine.records


def test_duplicate_archive_candidate_never_updates_twice(tmp_path):
    rows = plan()
    engine = EngineSpy(rows[0].before_json, tmp_path)
    with pytest.raises(policy.Refused, match="identity"):
        archive.archive_transaction(engine, UUID(int=90000005), rows + rows, tmp_path)
    assert not engine.events
