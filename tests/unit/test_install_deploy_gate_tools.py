from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "ops/systemd/install_deploy_gate_tools.sh"
PAYLOAD = (
    "ops/preflight/preflight_oms_restart.sh",
    "ops/systemd/deploy_oms_strategy_authorized.sh",
    "ops/systemd/deploy_service.sh",
    "src/project_mai_tai/deploy_preflight.py",
)
EXECUTABLES = PAYLOAD[:3]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    _git(source, "config", "user.email", "deploy-bootstrap@example.invalid")
    _git(source, "config", "user.name", "Deploy Bootstrap Test")

    for relative in PAYLOAD:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".py"):
            target.write_text("# fixture\n", encoding="utf-8")
        else:
            target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)

    installer_target = source / "ops/systemd/install_deploy_gate_tools.sh"
    shutil.copy2(INSTALLER, installer_target)
    installer_target.chmod(0o755)
    manifest = source / "ops/systemd/deploy_gate_tools.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((source / relative).read_bytes()).hexdigest()}  {relative}\n"
            for relative in PAYLOAD
        ),
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    return source, _git(source, "rev-parse", "HEAD")


def _run_installer(source: Path, sha: str, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", INSTALLER, source, sha, destination],
        check=False,
        capture_output=True,
        text=True,
    )


def test_installs_verified_release_outside_source_with_committed_modes(
    tmp_path: Path,
) -> None:
    source, sha = _write_source(tmp_path)
    destination = tmp_path / "tools"

    result = _run_installer(source, sha, destination)

    assert result.returncode == 0, result.stderr
    release = destination / "releases" / sha
    assert (release / "DEPLOY_GATE_SOURCE_SHA").read_text().strip() == sha
    for relative in PAYLOAD:
        assert (release / relative).read_bytes() == (source / relative).read_bytes()
    for relative in EXECUTABLES:
        assert (release / relative).stat().st_mode & 0o111
    verify = subprocess.run(
        ["sha256sum", "-c", "ops/systemd/deploy_gate_tools.sha256"],
        cwd=release,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr


def test_existing_verified_release_is_idempotent(tmp_path: Path) -> None:
    source, sha = _write_source(tmp_path)
    destination = tmp_path / "tools"
    first = _run_installer(source, sha, destination)

    second = _run_installer(source, sha, destination)

    assert first.returncode == 0
    assert second.returncode == 0
    assert "ALREADY INSTALLED" in second.stdout


def test_existing_tool_and_manifest_rewrite_refuse_against_source_manifest(
    tmp_path: Path,
) -> None:
    source, sha = _write_source(tmp_path)
    destination = tmp_path / "tools"
    first = _run_installer(source, sha, destination)
    assert first.returncode == 0
    release = destination / "releases" / sha
    deploy = release / "ops/systemd/deploy_service.sh"
    deploy.write_text(deploy.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    local_manifest = release / "ops/systemd/deploy_gate_tools.sha256"
    local_manifest.write_text(
        "".join(
            f"{hashlib.sha256((release / relative).read_bytes()).hexdigest()}  {relative}\n"
            for relative in PAYLOAD
        ),
        encoding="utf-8",
    )

    second = _run_installer(source, sha, destination)

    assert second.returncode == 1
    assert "differs from the approved source manifest" in second.stderr


def test_checksum_drift_refuses_without_installing(tmp_path: Path) -> None:
    source, _sha = _write_source(tmp_path)
    target = source / "src/project_mai_tai/deploy_preflight.py"
    target.write_text("# changed without manifest update\n", encoding="utf-8")
    _git(source, "add", str(target.relative_to(source)))
    _git(source, "commit", "-m", "drift")
    sha = _git(source, "rev-parse", "HEAD")
    destination = tmp_path / "tools"

    result = _run_installer(source, sha, destination)

    assert result.returncode == 1
    assert "source checksum verification failed" in result.stderr
    assert not (destination / "releases" / sha).exists()


def test_non_executable_committed_shell_refuses_without_installing(
    tmp_path: Path,
) -> None:
    source, _sha = _write_source(tmp_path)
    target = source / "ops/systemd/deploy_service.sh"
    target.chmod(0o644)
    _git(source, "add", str(target.relative_to(source)))
    _git(source, "commit", "-m", "drop executable bit")
    sha = _git(source, "rev-parse", "HEAD")
    destination = tmp_path / "tools"

    result = _run_installer(source, sha, destination)

    assert result.returncode == 1
    assert "not committed executable" in result.stderr
    assert not (destination / "releases" / sha).exists()
