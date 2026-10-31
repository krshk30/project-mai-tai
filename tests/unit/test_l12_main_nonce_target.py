"""Controlled nonce/file/session doubles only. No PG/process/provider starts."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from tools import l12_main_nonce_target as target
from tools.l12_service_boot_replay import require_private_factory, verify_private_session


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def capability():
    value = dict(version=target.VERSION, nonce=uuid4().hex, uid=os.getuid(), database_user="controlled",
        system_identifier="123456789", postmaster_pid=987654321, postmaster_started="CONTROLLED_EPOCH",
        server_version_num="160015", source_head="a" * 40, creation_review_sha256="b" * 64,
        creation_journal_tail_sha256="c" * 64,
        at_creation_schema_state="CATALOG_VERIFIED_ZERO_USER_RELATIONS_NO_COPY_OR_BOOT")
    value.update(target.layout(value["nonce"]))
    data = raw(value)
    return target.MainNonceTarget(data, sha256(data).hexdigest(), "b" * 64)


def forbidden_no_connect():
    raise AssertionError("controlled_URL_factory_must_never_connect")


def factory(capability, **changes):
    values = capability.values
    parts = dict(drivername="postgresql+psycopg", username=values["database_user"],
        database=values["database"], query={"host": values["socket"], "port": "55521"})
    parts.update(changes)
    return SimpleNamespace(kw={"bind": SimpleNamespace(url=URL.create(**parts), echo=False,
        pool=NullPool(creator=forbidden_no_connect))})


def identity(capability):
    value = capability.values
    return [value["database"], value["database_user"], value["data_directory"], "55521", None, "",
            "123456789", "CONTROLLED_EPOCH", "160015"]


class Session:
    def __init__(self, row):
        self.row, self.calls = row, []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement):
        self.calls.append(str(statement))
        return SimpleNamespace(one=lambda: self.row)


def test_explicit_nonce_capability_URL_and_legacy_default_still_refuses(capability):
    value = factory(capability)
    require_private_factory(value, target_capability=capability)
    with pytest.raises(ValueError, match="private_pg_target_required"):
        require_private_factory(value)
    assert capability.values["database"].startswith("oct24_l12_")
    assert "/oct23-l12-arendt-pg" not in capability.values["root"]


def test_offline_census_factory_cannot_hide_idle_connections_in_pool(capability):
    value = factory(capability)
    value.kw["bind"].pool = object()
    with pytest.raises(ValueError, match="NullPool_no_hidden_idle_clients"):
        require_private_factory(value, target_capability=capability)


@pytest.mark.parametrize("fault", ["db", "role", "TCP", "port", "socket", "driver", "password", "options"])
def test_capability_never_allows_a_similar_but_different_URL(capability, fault):
    changes = {"db": {"database": "foreign"}, "role": {"username": "foreign"},
        "TCP": {"host": "127.0.0.1"}, "port": {"query": {"host": capability.values["socket"], "port": "5432"}},
        "socket": {"query": {"host": "/tmp", "port": "55521"}},
        "driver": {"drivername": "postgresql+psycopg2"}, "password": {"password": "CONTROLLED"},
        "options": {"query": {"host": capability.values["socket"], "port": "55521", "options": "foreign"}}}[fault]
    with pytest.raises(ValueError, match="exact_nonce_capability_URL"):
        require_private_factory(factory(capability, **changes), target_capability=capability)


@pytest.mark.parametrize("index", range(9))
def test_every_connected_operand_mismatch_refuses_before_private_files(capability, index):
    values = identity(capability)
    values[index] = "FOREIGN"
    session = Session(values)
    with pytest.raises(ValueError, match="actual_nonce_target_epoch_role_or_noTCP"):
        verify_private_session(session, target_capability=capability)
    assert session.calls[:2] == ["SET LOCAL statement_timeout = '750ms'", "SET LOCAL lock_timeout = '250ms'"]


@pytest.fixture
def owned_files(capability):
    value = capability.values
    root = Path(value["root"])
    if not root.parent.exists():
        pytest.skip(f"requires local rig artifact {root.parent} (nonce fixture parent)")
    root.mkdir(mode=0o700)  # Fresh random fixture only, never an adopted cluster.
    try:
        (root / "data").mkdir(mode=0o700)
        (root / "socket").mkdir(mode=0o700)
        pid = root / "data/postmaster.pid"
        pid.write_text(f'987654321\n{value["data_directory"]}\n1\n55521\n{value["socket"]}\n')
        pid.chmod(0o600)
        yield root
    finally:
        shutil.rmtree(root)


def test_connected_identity_and_PID_file_are_both_required(capability, owned_files):
    verify_private_session(Session(identity(capability)), target_capability=capability)
    pid = owned_files / "data/postmaster.pid"
    pid.write_text(pid.read_text().replace("987654321", "987654322"))
    with pytest.raises(ValueError, match="actual_nonce_postmaster_PID"):
        verify_private_session(Session(identity(capability)), target_capability=capability)


def test_target_artifact_requires_actual_review_hash_and_0600(capability, owned_files):
    artifact = owned_files / "target.json"
    artifact.write_bytes(capability.raw)
    artifact.chmod(0o600)
    assert target.load_target_file(artifact, reviewed_sha256=capability.reviewed_sha256,
        expected_creation_review_sha256="b" * 64).values == capability.values
    artifact.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        target.load_target_file(artifact, reviewed_sha256=capability.reviewed_sha256,
            expected_creation_review_sha256="b" * 64)


@pytest.mark.parametrize("fault", ["root", "database", "nonce", "creation_review", "pid", "version", "extra"])
def test_artifact_membership_nonce_layout_and_creation_binding_are_closed(capability, fault):
    value = dict(capability.values)
    if fault == "root":
        value["root"] = "/private/tmp/oct23-l12-arendt-pg"
    elif fault == "database":
        value["database"] = "oct23_l12_arendt"
    elif fault == "nonce":
        value["nonce"] = "../../foreign"
    elif fault == "creation_review":
        value["creation_review_sha256"] = "d" * 64
    elif fault == "pid":
        value["postmaster_pid"] = True
    elif fault == "version":
        value["server_version_num"] = "150001"
    else:
        value["unreviewed"] = True
    data = raw(value)
    with pytest.raises(ValueError):
        target.MainNonceTarget(data, sha256(data).hexdigest(), "b" * 64)
