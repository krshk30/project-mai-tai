"""New source controls only; no PG/engine/cluster/service/provider fixtures."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tools import l12_private_restore_main as caller
from tools import l12_private_onechain as chain
from tools.l12_nonce_copy_lease import NonceCopyLease
from tools.l12_main_nonce_target import MainNonceTarget, VERSION as TARGET_VERSION, layout


def write(tmp_path, name, raw):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def packet():
    return dict(version=caller.VERSION, operation="construct", capability={},
        creation_review_sha256="a" * 64, source={}, manifest={}, metadata={}, bridge_receipt={},
        parts_root="/private/tmp/not-a-real-copy", chunk_review={}, schema_review={},
        writer_exclusion={}, execution_review_sha256="b" * 64, construction_receipt=None,
        configured_accounts=["live:schwab", "live:webull"],
        expanded_accounts=["live:schwab", "live:webull"])


def test_plan_only_has_no_engine_or_target_access(tmp_path, monkeypatch, capsys):
    path = write(tmp_path, "packet.json", json.dumps(packet()).encode())
    monkeypatch.setattr(caller, "create_engine", lambda *a, **k: pytest.fail("engine forbidden"))
    monkeypatch.setattr(caller, "execute", lambda *a, **k: pytest.fail("execution forbidden"))
    monkeypatch.setattr(caller.sys, "argv", ["caller", "--packet", str(path),
        "--reviewed-sha256", sha256(path.read_bytes()).hexdigest()])
    caller.main()
    assert json.loads(capsys.readouterr().out) == dict(
        status="PLAN_ONLY_NO_ENGINE_OR_TARGET_ACCESS", operation="construct")


def test_wrong_packet_hash_refuses_before_target_or_engine(monkeypatch):
    monkeypatch.setattr(caller, "load_target_file", lambda *a, **k: pytest.fail("target forbidden"))
    monkeypatch.setattr(caller, "create_engine", lambda *a, **k: pytest.fail("engine forbidden"))
    with pytest.raises(ValueError, match="independent_exact_packet"):
        caller.execute(json.dumps(packet()).encode(), reviewed_sha256="1" * 64)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_duplicate_or_nonfinite_json_refused(raw):
    with pytest.raises(ValueError):
        caller.json_value(raw)


@pytest.mark.parametrize("change", [dict(operation="retry"), dict(version="old"),
    dict(construction_receipt={}), dict(execution_review_sha256="not-a-hash"), dict(extra=True)])
def test_packet_shape_refused(change):
    value = packet()
    value.update(change)
    with pytest.raises(ValueError):
        caller.checked_packet(json.dumps(value).encode())


def test_restore_requires_actual_commit_artifact():
    value = packet()
    value["operation"] = "restore"
    with pytest.raises(ValueError, match="actual_committed"):
        caller.checked_packet(json.dumps(value).encode())


@pytest.mark.parametrize("kind", ["mode", "hardlink", "symlink", "oversize"])
def test_private_file_adverse(tmp_path, kind):
    path = write(tmp_path, "input", b"abc")
    if kind == "mode":
        path.chmod(0o644)
    if kind == "hardlink":
        os.link(path, tmp_path / "second")
    if kind == "symlink":
        original = path
        path = tmp_path / "symlink"
        path.symlink_to(original)
    with pytest.raises(ValueError):
        caller.private_file(path, 2 if kind == "oversize" else 10)


def test_artifact_hash_is_independent_of_observed_bytes(tmp_path):
    path = write(tmp_path, "input", b"abc")
    assert caller.artifact(dict(path=str(path), sha256=sha256(b"abc").hexdigest())) == b"abc"
    with pytest.raises(ValueError, match="hash_changed"):
        caller.artifact(dict(path=str(path), sha256="a" * 64))


@pytest.mark.parametrize("name", ["../secret", "/absolute", "v2/../other", "v2//a", "unknown/a"])
def test_parts_escape_or_noncanonical_refused(tmp_path, name):
    with pytest.raises(ValueError):
        caller.Parts(tmp_path)(name)


def test_named_part_keeps_original_bytes(tmp_path):
    raw = b'{ "original": [1, 2], "null": null }\n'
    write(tmp_path, "v2/part.json", raw)
    assert caller.Parts(tmp_path)("v2/part.json") == raw


def test_source_expiry_refuses_before_tree_or_target(monkeypatch):
    guard = caller.CopySourceGuard(Path("/no-copy"), "a" * 40, (), 5)
    monkeypatch.setattr(caller.time, "time_ns", lambda: 5_000_000)
    monkeypatch.setattr(caller.v10, "_verify_tree", lambda *a: pytest.fail("must refuse first"))
    assert guard(SimpleNamespace(execution_head="a" * 40)) is False


def test_source_head_mismatch_refuses_before_tree(monkeypatch):
    guard = caller.CopySourceGuard(Path("/no-copy"), "a" * 40, (), 10**30)
    monkeypatch.setattr(caller.v10, "_verify_tree", lambda *a: pytest.fail("must refuse first"))
    assert guard(SimpleNamespace(execution_head="b" * 40)) is False


def test_onechain_source_pins_and_sequence_are_literal():
    assert [item[1:] for item in chain.CHAIN] == [
        ("20261023_atr_simulation", "20261015_bot_control"),
        ("20261023_atr_aggregate", "20261023_atr_simulation"),
        ("20261022_l12_retirement", "20261023_atr_aggregate")]
    assert len(chain.SCHEMA_PINS) == 6
    assert all(len(value) == 64 for value in chain.SCHEMA_PINS.values())


def test_onechain_missing_real_target_refuses_before_any_DDL(monkeypatch):
    monkeypatch.setattr(chain, "source_bytes", lambda *a: pytest.fail("must refuse first"))
    with pytest.raises(ValueError, match="actual_onechain"):
        chain.construct_onechain(None, root="/not-a-root", metadata_raw=b"", metadata_sha256="a" * 64,
            target=None, lease=None, source_guard=lambda value: True, append_readback=lambda raw: raw)


def test_lease_wrong_stage_and_duplicate_DDL_do_not_consume_journal():
    lease = object.__new__(NonceCopyLease)
    lease._held_file = lambda: True
    lease.context, lease.ddl_consumed = dict(operation="restore"), False
    lease.journal = lambda *a: pytest.fail("must refuse before journal")
    with pytest.raises(ValueError, match="already_consumed"):
        lease.begin_ddl()
    lease.context, lease.ddl_consumed = dict(operation="construct"), True
    with pytest.raises(ValueError, match="already_consumed"):
        lease.begin_ddl()


def test_construct_lease_cannot_authorize_restore():
    lease = object.__new__(NonceCopyLease)
    lease._held_file = lambda: True
    lease.context, lease.bound_binding = dict(operation="construct"), None
    with pytest.raises(ValueError, match="already_consumed"):
        lease.bind_inputs(plan={}, schema_review={}, authority=None, binding="a" * 64)


@pytest.fixture
def nonce(tmp_path):
    value = dict(version=TARGET_VERSION, nonce=uuid4().hex, uid=os.getuid(), database_user="controlled",
        system_identifier="123456789", postmaster_pid=987654321, postmaster_started="CONTROLLED_EPOCH",
        server_version_num="160015", source_head="a" * 40, creation_review_sha256="b" * 64,
        creation_journal_tail_sha256="c" * 64,
        at_creation_schema_state="CATALOG_VERIFIED_ZERO_USER_RELATIONS_NO_COPY_OR_BOOT")
    value.update(layout(value["nonce"]))
    raw = json.dumps(value).encode()
    target = MainNonceTarget(raw, sha256(raw).hexdigest(), "b" * 64)
    root = Path(value["root"])
    if not root.parent.exists():
        pytest.skip(f"requires local rig artifact {root.parent} (nonce fixture parent)")
    root.mkdir(mode=0o700)
    try:
        yield target
    finally:
        # Only this test's newly created nonce. No server/process is started.
        shutil.rmtree(root)


def new_lease(target, operation):
    return NonceCopyLease(target, manifest_sha256="d" * 64, metadata_sha256="e" * 64,
        schema_review_sha256="f" * 64, writer_exclusion_receipt_sha256="1" * 64,
        source_guard=lambda value: value is target, operation=operation)


def test_real_construct_then_restore_have_distinct_no_retry_journals(nonce):
    lease = new_lease(nonce, "construct")
    lease.begin_ddl()
    assert lease.file_receipt["exact_readback"] is True
    with pytest.raises(BlockingIOError):
        new_lease(nonce, "restore")
    lease.close()
    with pytest.raises(FileExistsError):
        new_lease(nonce, "construct")
    restore = new_lease(nonce, "restore")
    try:
        assert restore.context["operation"] == "restore"
        assert restore._held_file() is True
    finally:
        restore.close()
    with pytest.raises(FileExistsError):
        new_lease(nonce, "restore")


@pytest.mark.parametrize("others", [1, None, True])
def test_actual_lease_census_unknown_or_other_client_blocks(nonce, monkeypatch, others):
    lease = new_lease(nonce, "construct")
    monkeypatch.setattr(MainNonceTarget, "verify_session", lambda self, session: None)
    session = SimpleNamespace(scalar=lambda statement: others)
    try:
        with pytest.raises(ValueError, match="other_nonce_client"):
            lease.verify_session(session)
    finally:
        lease.close()


def test_real_lease_replacement_is_not_ownership(nonce):
    lease = new_lease(nonce, "construct")
    try:
        original = lease.path.with_suffix(".original")
        lease.path.rename(original)
        lease.path.write_bytes(lease.raw)
        lease.path.chmod(0o600)
        with pytest.raises(ValueError, match="identity_or_bytes_changed"):
            lease._held_file()
    finally:
        lease.close()
