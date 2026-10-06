"""Actual Redis show omitted empty EnvironmentFiles without --all, October6."""
from types import SimpleNamespace
import json

import attended
import pytest


def test_literal_fleet_requests_empty_fields_including_redis_environment_files(tmp_path):
    effects = attended.Real(tmp_path, {}, tmp_path)
    calls = []

    def command(args):
        calls.append(args)
        if args[:2] == ["busctl", "call"]:
            return SimpleNamespace(stdout=b'o "/org/freedesktop/systemd1/unit/recorded_2eservice"')
        if args[:2] == ["busctl", "get-property"]:
            return SimpleNamespace(stdout=b'a(sb) 0')
        assert args[:2] == ["systemctl", "show"]
        values = {key: "" for key in attended.UNIT_FIELDS}
        values.update(MainPID="926", NRestarts="0", ExecMainCode="0", ExecMainStatus="0",
                      ExecMainStartTimestampMonotonic="9887731", ActiveState="active",
                      SubState="running", Result="success")
        if args[2] in {"redis.service", "postgresql.service"}:
            values.pop("EnvironmentFiles")
        return SimpleNamespace(stdout="\n".join(key + "=" + value for key, value in values.items()).encode())

    effects.command = command
    fleet = effects.fleet()
    assert fleet["redis"]["EnvironmentFiles"] == ""
    assert len(calls) == len(attended.SERVICES) + 4
    assert all("--all" in args for args in calls if args[0] == "systemctl")


@pytest.mark.parametrize("array", [b"", b"as 0", b'a(sb) 1 "foreign" false'])
def test_omitted_system_environment_array_requires_exact_typed_empty_proof(tmp_path, array):
    effects = attended.Real(tmp_path, {}, tmp_path)
    def command(args):
        if args[:2] == ["busctl", "call"]:
            return SimpleNamespace(stdout=b'o "/org/freedesktop/systemd1/unit/redis_2dserver_2eservice"')
        if args[:2] == ["busctl", "get-property"]:
            return SimpleNamespace(stdout=array)
        values = {key: "0" for key in attended.UNIT_FIELDS}
        if args[2] == "redis.service":
            values.pop("EnvironmentFiles")
        return SimpleNamespace(stdout="\n".join(key + "=" + value for key, value in values.items()).encode())
    effects.command = command
    with pytest.raises(attended.Stop, match="not proven empty"):
        effects.fleet()


def test_actual_receipts_create_the_install_records_append_only_source_journal(tmp_path):
    effects = attended.Real(tmp_path, {}, tmp_path)
    effects.receipt("first.txt", b"private body")
    effects.receipt("second.txt", b"second")
    journal = tmp_path / "runner-journal.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [row["receipt"] for row in rows] == ["001-first.txt", "002-second.txt"]
    assert rows[0]["sha256"] == attended.digest(b"private body")
    assert "private body" not in journal.read_text()
    assert journal.stat().st_mode & 0o777 == 0o600
