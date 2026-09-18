from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "ops/systemd/deploy_oms_strategy_authorized.sh"
DEPLOY_SERVICE = ROOT / "ops/systemd/deploy_service.sh"
EXPECTED_SHA = "a" * 40


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    call_log = tmp_path / "calls.log"
    approval = tmp_path / "operator-go.txt"
    approval.write_text(
        "\n".join(
            (
                "DEPLOYMENT=oms-strategy",
                "AUTHORITY=operator",
                "APPROVED_DATE_ET=2026-09-18",
                "APPROVED_AT_UTC=2026-09-18T20:30:00Z",
                "EXPIRES_AT_UTC=2026-09-18T23:30:00Z",
                f"EXPECTED_SHA={EXPECTED_SHA}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    approval.chmod(0o600)

    _write_executable(
        repo / ".venv/bin/python",
        "#!/usr/bin/env bash\n"
        'echo "preflight:$*" >> "$CALL_LOG"\n'
        'exit "${PREFLIGHT_RC:-0}"\n',
    )
    (repo / "src/project_mai_tai").mkdir(parents=True)
    (repo / "src/project_mai_tai/deploy_preflight.py").write_text("# fixture\n")
    _write_executable(
        repo / "ops/preflight/preflight_oms_restart.sh",
        "#!/usr/bin/env bash\n"
        'echo fence >> "$CALL_LOG"\n'
        'exit "${FENCE_RC:-0}"\n',
    )
    _write_executable(
        repo / "ops/systemd/deploy_service.sh",
        "#!/usr/bin/env bash\n"
        'echo "deploy:${MAI_TAI_EXPECTED_SHA:-missing}:$*" >> "$CALL_LOG"\n',
    )

    fake_bin = tmp_path / "bin"
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  '+%F') echo 2026-09-18 ;;\n"
        '  "+%H%M") echo "${FAKE_ET_TIME:-1700}" ;;\n'
        "  '-u +%s') echo 1789768800 ;;\n"
        "  '-u -d 2026-09-18T20:30:00Z +%s') echo 1789763400 ;;\n"
        "  '-u -d 2026-09-18T23:30:00Z +%s') echo 1789774200 ;;\n"
        "  '-u -d 2026-09-18T20:00:00Z +%s') echo 1789761600 ;;\n"
        '  *) echo "unexpected date args: $*" >&2; exit 90 ;;\n'
        "esac\n",
    )
    _write_executable(
        fake_bin / "git",
        "#!/usr/bin/env bash\n"
        'echo "git:$*" >> "$CALL_LOG"\n'
        'if [[ "$*" == *"rev-parse origin/main" ]]; then echo "$EXPECTED_SHA"; fi\n'
        'if [[ "$*" == *"rev-parse HEAD" ]]; then echo "$EXPECTED_SHA"; fi\n',
    )

    env = os.environ.copy()
    env.update(
        {
            "CALL_LOG": str(call_log),
            "EXPECTED_SHA": EXPECTED_SHA,
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return repo, approval, call_log, env


def _run_gate(
    repo: Path,
    approval: Path | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", GATE, repo, EXPECTED_SHA, str(approval) if approval else ""],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _calls(call_log: Path) -> list[str]:
    if not call_log.exists():
        return []
    return call_log.read_text(encoding="utf-8").splitlines()


def test_missing_operator_go_refuses_before_any_production_read(tmp_path: Path) -> None:
    repo, _approval, call_log, env = _fixture(tmp_path)

    result = _run_gate(repo, None, env)

    assert result.returncode == 3
    assert result.stdout.splitlines()[0] == "DEPLOY NOT AUTHORISED"
    assert _calls(call_log) == []


def test_approval_for_a_different_sha_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    approval.write_text(
        approval.read_text(encoding="utf-8").replace(EXPECTED_SHA, "b" * 40),
        encoding="utf-8",
    )

    result = _run_gate(repo, approval, env)

    assert result.returncode == 3
    assert "DEPLOY NOT AUTHORISED" in result.stdout
    assert _calls(call_log) == []


def test_preflight_refusal_never_reaches_fence_or_deploy(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["PREFLIGHT_RC"] = "1"

    result = _run_gate(repo, approval, env)

    assert result.returncode == 1
    assert _calls(call_log) == [
        f"preflight:{repo}/src/project_mai_tai/deploy_preflight.py --service oms "
        "--overview-url http://127.0.0.1:8100/api/overview"
    ]
    assert "one-shot live OMS preflight" in result.stderr


def test_managed_position_fence_refusal_never_reaches_deploy(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FENCE_RC"] = "2"

    result = _run_gate(repo, approval, env)

    assert result.returncode == 2
    assert _calls(call_log) == [
        f"preflight:{repo}/src/project_mai_tai/deploy_preflight.py --service oms "
        "--overview-url http://127.0.0.1:8100/api/overview",
        "fence",
    ]
    assert "zero managed rows" in result.stderr


def test_valid_go_and_both_preflights_reach_only_the_pinned_deploy(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)

    result = _run_gate(repo, approval, env)

    assert result.returncode == 0
    assert _calls(call_log) == [
        f"preflight:{repo}/src/project_mai_tai/deploy_preflight.py --service oms "
        "--overview-url http://127.0.0.1:8100/api/overview",
        "fence",
        f"git:-C {repo} fetch origin main",
        f"git:-C {repo} rev-parse origin/main",
        f"deploy:{EXPECTED_SHA}:{repo} main oms",
        f"git:-C {repo} rev-parse HEAD",
    ]
    assert f"[DEPLOY-COMPLETE] deployment=oms-strategy sha={EXPECTED_SHA}" in result.stdout


def test_before_1605_et_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FAKE_ET_TIME"] = "1604"

    result = _run_gate(repo, approval, env)

    assert result.returncode == 3
    assert "16:05 ET" in result.stderr
    assert _calls(call_log) == []


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_deploy_service_refuses_if_main_moved_past_the_approved_sha(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    box = tmp_path / "box"
    subprocess.run(["git", "init", "--bare", origin], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "-b", "main", author], check=True, capture_output=True
    )
    _git(author, "config", "user.email", "deploy-test@example.invalid")
    _git(author, "config", "user.name", "Deploy Test")
    (author / "value.txt").write_text("base\n", encoding="utf-8")
    _commit(author, "base")
    _git(author, "remote", "add", "origin", str(origin))
    _git(author, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(["git", "clone", origin, box], check=True, capture_output=True)
    box_base = _git(box, "rev-parse", "HEAD")

    (author / "value.txt").write_text("approved\n", encoding="utf-8")
    approved = _commit(author, "approved")
    _git(author, "push", "origin", "main")
    (author / "value.txt").write_text("moved\n", encoding="utf-8")
    moved = _commit(author, "moved")
    _git(author, "push", "origin", "main")

    env = os.environ.copy()
    env["MAI_TAI_EXPECTED_SHA"] = approved
    result = subprocess.run(
        ["bash", DEPLOY_SERVICE, box, "main", "control"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 3
    assert f"origin/main moved: expected {approved}, found {moved}" in result.stderr
    assert _git(box, "rev-parse", "HEAD") == box_base
