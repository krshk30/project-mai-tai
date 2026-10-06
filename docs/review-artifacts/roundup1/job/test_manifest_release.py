"""Manifest hash/scope and fail-closed artifact verification; controlled metadata only."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import attended
import make_release
import make_approval
import release_policy as policy
from test_attended_release import decision, release

NOW = datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc)


def staged_control(monkeypatch, tmp_path):
    # This is a local pytest fixture directory, not a production stage or real approval.
    data = release()
    artifact = tmp_path / "controlled.py"
    artifact.write_text("CONTROLLED immutable bytes")
    data.update(artifacts={artifact.name: policy.digest(artifact.read_bytes())}, blocking_acceptance=[])
    raw = policy.canonical(data)
    (tmp_path / "release.json").write_bytes(raw)
    expected = policy.digest(raw)
    approved = dict(decision(), release_sha256=expected)
    (tmp_path / "approval.json").write_bytes(policy.canonical(approved))
    real_stat = Path.stat
    def metadata(path, *args, **kwargs):
        stat = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_uid=0, st_mode=stat.st_mode & ~0o022)
    monkeypatch.setattr(Path, "stat", metadata)
    return expected, artifact


def test_exact_local_manifest_control_hash_and_artifact_drift(monkeypatch, tmp_path):
    expected, artifact = staged_control(monkeypatch, tmp_path)
    assert attended.verify(tmp_path, expected, NOW)["approved_sha"] == policy.APP
    artifact.write_text("CONTROLLED drift")
    with pytest.raises(policy.Stop):
        attended.verify(tmp_path, expected, NOW)


def test_missing_approval_never_runs(monkeypatch, tmp_path):
    expected, _ = staged_control(monkeypatch, tmp_path)
    (tmp_path / "approval.json").unlink()
    with pytest.raises(FileNotFoundError):
        attended.verify(tmp_path, expected, NOW)


def test_unready_acceptance_blocker_cannot_be_overruled_by_signed_json(monkeypatch, tmp_path):
    _, _ = staged_control(monkeypatch, tmp_path)
    data = json.loads((tmp_path / "release.json").read_bytes())
    data["blocking_acceptance"] = ["RETRYOFF1 env-only contradicts operator card"]
    raw = policy.canonical(data)
    (tmp_path / "release.json").write_bytes(raw)
    approved = json.loads((tmp_path / "approval.json").read_bytes())
    approved["release_sha256"] = policy.digest(raw)
    (tmp_path / "approval.json").write_bytes(policy.canonical(approved))
    with pytest.raises(policy.Stop, match="unresolved acceptance"):
        attended.verify(tmp_path, policy.digest(raw), NOW)


def test_artifact_symlink_and_manifest_path_escape_block(monkeypatch, tmp_path):
    expected, artifact = staged_control(monkeypatch, tmp_path)
    other = tmp_path / "other"
    artifact.rename(other)
    artifact.symlink_to(other)
    with pytest.raises(policy.Stop):
        attended.verify(tmp_path, expected, NOW)


def test_builder_fixed_candidate_scope_and_local_package_no_approval():
    assert make_release.APP == policy.APP and make_release.TREE == policy.TREE
    assert "attended.py" in make_release.NAMES and "make_release.py" in make_release.NAMES
    assert "sql/migrations/versions/20261005_0022_managed_entry_binding.py" not in make_release.SOURCES
    assert "ops/health/preopen_alert.sh" in make_release.SOURCES
    with pytest.raises(policy.Stop):
        make_release.generate("HEAD")


def working_blob_control(monkeypatch):
    # Only fake the future plan commit lookup; candidate blobs/tree are real Git.
    original = make_release.blob
    root = Path(__file__).resolve().parents[4]
    monkeypatch.setattr(make_release, "blob", lambda ref, path: (root / path).read_bytes()
                        if ref == "a" * 40 else original(ref, path))


def test_deterministic_manifest_adopts_zero_not_off_and_pins_runtime_conditions(monkeypatch):
    working_blob_control(monkeypatch)
    first = make_release.generate("a" * 40)
    assert policy.canonical(first) == policy.canonical(make_release.generate("a" * 40))
    assert first["approved_sha"] == policy.APP and first["tree"] == policy.TREE
    assert first["blocking_acceptance"] == []
    assert any("RETRYOFF1" in entry and "withdrawn" in entry for entry in first["excluded"])
    assert len(first["runtime_required"]) == 6
    assert first["flaggate"]["total"] == 153 and "151/153 UNKNOWN2" in first["flaggate"]["required"]
    assert first["env_updates"] == policy.ENV_UPDATES
    assert first["retained_env"] == {policy.RETRY_ENABLED: "true"}
    assert first["numeric_catalog"]["sha256"] == policy.NUMERIC_SHA
    assert policy.NUMERIC_ARTIFACT in first["artifacts"]
    assert "retry_zero_readonly.py" in first["artifacts"]
    assert first["display"] == dict(merged_sha=policy.APP, source_in_checkout=True,
                                    activation="AUTHORIZED_INSTALL1_UNMEASURED", control_restart=True,
                                    restart_count=1, page="/bot/orb", symbol="JAGX", trades=1)
    assert "control_display_proof.py" in first["artifacts"]
    assert "src/project_mai_tai/services/control_plane.py" in first["application_blobs"]
    assert "armed_readonly.py" in first["artifacts"]


def test_literal_local_package_all_hashes_match_and_no_approval_generated(monkeypatch, tmp_path):
    working_blob_control(monkeypatch)
    directory = tmp_path / "package"
    monkeypatch.setattr("sys.argv", ["make_release.py", "--plan", "a" * 40, "--package", str(directory)])
    make_release.main()
    manifest = json.loads((directory / "release.json").read_bytes())
    assert not (directory / "approval.json").exists()
    assert set(path.name for path in directory.iterdir()) == set(manifest["artifacts"]) | {"release.json"}
    for name, sha in manifest["artifacts"].items():
        assert policy.digest((directory / name).read_bytes()) == sha
    with pytest.raises(FileExistsError):
        make_release.main()


@pytest.mark.parametrize("option", ["--output", "--package"])
def test_builder_refuses_production_destination_before_any_write(monkeypatch, option):
    working_blob_control(monkeypatch)
    monkeypatch.setattr("sys.argv", ["make_release.py", "--plan", "a" * 40, option, "/home/trader/not-authorized"])
    with pytest.raises(policy.Stop, match="never stages production"):
        make_release.main()


def test_local_approval_exact_committed_blobs_and_standing_authority(monkeypatch, tmp_path):
    working_blob_control(monkeypatch)
    raw = policy.canonical(make_release.generate("a" * 40))
    expected = policy.digest(raw)
    result = json.loads(make_approval.create(raw, expected))
    assert result["authority"] == policy.AUTHORITY and "reviewer" not in result
    policy.approval(json.loads(raw), expected, result, NOW)
    release_file, output = tmp_path / "release.json", tmp_path / "approval.json"
    release_file.write_bytes(raw)
    monkeypatch.setattr("sys.argv", ["make_approval.py", "--release", str(release_file), "--expected", expected,
                                    "--output", str(output)])
    make_approval.main()
    assert output.read_bytes() == make_approval.create(raw, expected)
    with pytest.raises(FileExistsError):
        make_approval.main()


@pytest.mark.parametrize("fault", ["hash", "uncommitted", "blocker", "scope", "old-reviewer"])
def test_standing_approval_drift_unready_or_fabricated_reviewer_blocks(monkeypatch, fault):
    working_blob_control(monkeypatch)
    data = make_release.generate("a" * 40)
    if fault == "uncommitted":
        data["artifacts"]["attended.py"] = "a" * 64
    elif fault == "blocker":
        data["blocking_acceptance"] = ["controlled unresolved acceptance"]
    elif fault == "scope":
        data["scope"] = "all-services"
    raw = policy.canonical(data)
    expected = "c" * 64 if fault == "hash" else policy.digest(raw)
    if fault == "old-reviewer":
        decision = json.loads(make_approval.create(raw, expected))
        decision["reviewer"] = "claude-1"
        with pytest.raises(policy.Stop):
            policy.approval(data, expected, decision, NOW)
    else:
        with pytest.raises(policy.Stop):
            make_approval.create(raw, expected)


def test_approval_builder_refuses_production_destination_before_read(monkeypatch):
    monkeypatch.setattr("sys.argv", ["make_approval.py", "--release", "/missing", "--expected", "a" * 64,
                                    "--output", "/home/trader/unapproved/approval.json"])
    with pytest.raises(policy.Stop, match="never stages production"):
        make_approval.main()
