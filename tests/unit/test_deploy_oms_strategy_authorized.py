from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "ops/systemd/deploy_oms_strategy_authorized.sh"
DEPLOY_SERVICE = ROOT / "ops/systemd/deploy_service.sh"
EXPECTED_SHA = "a" * 40
CURRENT_SHA = "c" * 40
DEPLOY_GATE_PAYLOAD = (
    "ops/preflight/preflight_oms_restart.sh",
    "ops/systemd/deploy_oms_strategy_authorized.sh",
    "ops/systemd/deploy_service.sh",
    "src/project_mai_tai/deploy_preflight.py",
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    call_log = tmp_path / "calls.log"
    deploy_marker = tmp_path / "deployed"
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
        '#!/usr/bin/env bash\necho "preflight:$*" >> "$CALL_LOG"\nexit "${PREFLIGHT_RC:-0}"\n',
    )
    (repo / "src/project_mai_tai").mkdir(parents=True)
    (repo / "src/project_mai_tai/deploy_preflight.py").write_text("# fixture\n")
    _write_executable(
        repo / "ops/preflight/preflight_oms_restart.sh",
        '#!/usr/bin/env bash\necho fence >> "$CALL_LOG"\nexit "${FENCE_RC:-0}"\n',
    )
    _write_executable(
        repo / "ops/systemd/deploy_service.sh",
        "#!/usr/bin/env bash\n"
        'if [[ -e "$APPROVAL_PATH" ]] || ! compgen -G "$APPROVAL_PATH.used-*" >/dev/null; then\n'
        '  echo deploy-before-approval-consumption >> "$CALL_LOG"\n'
        "  exit 88\n"
        "fi\n"
        'echo "deploy:${MAI_TAI_EXPECTED_SHA:-missing}:$*" >> "$CALL_LOG"\n'
        'touch "$DEPLOY_MARKER"\n',
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
        "  '-u -d 2026-09-18T23:00:00Z +%s') echo 1789772400 ;;\n"
        "  '-u -d 2026-09-25T23:30:00Z +%s') echo 1790379000 ;;\n"
        '  *) echo "unexpected date args: $*" >&2; exit 90 ;;\n'
        "esac\n",
    )
    _write_executable(
        fake_bin / "stat",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "-c %u "*|"-f %u "*) echo "${FAKE_APPROVAL_UID}" ;;\n'
        '  "-c %a "*|"-f %Lp "*) echo "${FAKE_APPROVAL_MODE:-600}" ;;\n'
        '  *) echo "unexpected stat args: $*" >&2; exit 91 ;;\n'
        "esac\n",
    )
    _write_executable(
        fake_bin / "git",
        "#!/usr/bin/env bash\n"
        'echo "git:$*" >> "$CALL_LOG"\n'
        'if [[ "$*" == *" show ${EXPECTED_SHA}:ops/systemd/deploy_gate_tools.sha256" ]]; then\n'
        '  cat "$APPROVED_MANIFEST_PATH"\n'
        "  exit $?\n"
        "fi\n"
        'if [[ "$*" == *"rev-parse origin/main" ]]; then\n'
        '  echo "${REMOTE_MAIN_SHA:-$EXPECTED_SHA}"\n'
        "fi\n"
        'if [[ "$*" == *"rev-parse HEAD" ]]; then\n'
        '  if [[ -e "$DEPLOY_MARKER" ]]; then echo "$EXPECTED_SHA"; else echo "$CURRENT_SHA"; fi\n'
        "fi\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "CALL_LOG": str(call_log),
            "APPROVAL_PATH": str(approval),
            "CURRENT_SHA": CURRENT_SHA,
            "DEPLOY_MARKER": str(deploy_marker),
            "EXPECTED_SHA": EXPECTED_SHA,
            "FAKE_APPROVAL_UID": str(os.getuid()),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return repo, approval, call_log, env


def _run_gate(
    repo: Path,
    approval: Path | None,
    env: dict[str, str],
    *,
    expected_sha: str = EXPECTED_SHA,
    tools_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["bash", GATE, repo, expected_sha, str(approval) if approval else ""]
    if tools_dir is not None:
        command.append(str(tools_dir))
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _calls(call_log: Path) -> list[str]:
    if not call_log.exists():
        return []
    return call_log.read_text(encoding="utf-8").splitlines()


def _replace_field(approval: Path, key: str, value: str) -> None:
    lines = approval.read_text(encoding="utf-8").splitlines()
    approval.write_text(
        "\n".join(f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines)
        + "\n",
        encoding="utf-8",
    )


def _external_tools(tmp_path: Path, repo: Path) -> tuple[Path, Path]:
    tools = tmp_path / "external-tools"
    for relative in DEPLOY_GATE_PAYLOAD:
        source = GATE if relative.endswith("deploy_oms_strategy_authorized.sh") else repo / relative
        target = tools / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (tools / "DEPLOY_GATE_SOURCE_SHA").write_text(EXPECTED_SHA + "\n", encoding="utf-8")
    manifest = tools / "ops/systemd/deploy_gate_tools.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((tools / relative).read_bytes()).hexdigest()}  {relative}\n"
            for relative in DEPLOY_GATE_PAYLOAD
        ),
        encoding="utf-8",
    )
    approved_manifest = tmp_path / "approved-deploy-gate-tools.sha256"
    shutil.copy2(manifest, approved_manifest)
    return tools, approved_manifest


def _assert_authorization_refused(result: subprocess.CompletedProcess[str], call_log: Path) -> None:
    assert result.returncode == 3
    assert "DEPLOY NOT AUTHORISED" in result.stdout
    calls = _calls(call_log)
    assert not any(line.startswith("preflight:") for line in calls)
    assert not any(line.startswith("deploy:") for line in calls)


def test_missing_operator_go_refuses_before_any_production_read(tmp_path: Path) -> None:
    repo, _approval, call_log, env = _fixture(tmp_path)

    result = _run_gate(repo, None, env)

    _assert_authorization_refused(result, call_log)
    assert result.stdout.splitlines()[0] == "DEPLOY NOT AUTHORISED"


def test_approval_for_a_different_sha_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    approval.write_text(
        approval.read_text(encoding="utf-8").replace(EXPECTED_SHA, "b" * 40),
        encoding="utf-8",
    )

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)


def test_expired_approval_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "EXPIRES_AT_UTC", "2026-09-18T20:00:00Z")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "expired" in result.stderr


def test_previous_et_date_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "APPROVED_DATE_ET", "2026-09-17")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "today's ET session" in result.stderr


def test_world_writable_approval_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FAKE_APPROVAL_MODE"] = "666"

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "mode 0600 or 0400" in result.stderr


def test_approval_longer_than_twelve_hours_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "EXPIRES_AT_UTC", "2026-09-25T23:30:00Z")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "more than 12 hours" in result.stderr


def test_non_operator_authority_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "AUTHORITY", "reviewer")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "authority is not the operator" in result.stderr


def test_wrong_deployment_name_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "DEPLOYMENT", "oms-only")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "not for the OMS plus strategy" in result.stderr


def test_unknown_approval_field_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    approval.write_text(
        approval.read_text(encoding="utf-8").replace("AUTHORITY=operator", "UNEXPECTED_FIELD=true"),
        encoding="utf-8",
    )

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "unknown field" in result.stderr


def test_duplicated_approval_field_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    approval.write_text(
        approval.read_text(encoding="utf-8") + "AUTHORITY=operator\n",
        encoding="utf-8",
    )

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "exactly six lines" in result.stderr


def test_approval_owned_by_another_user_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FAKE_APPROVAL_UID"] = str(os.getuid() + 1)

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "not owned by the invoking user" in result.stderr


def test_future_approval_timestamp_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "APPROVED_AT_UTC", "2026-09-18T23:00:00Z")

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "in the future" in result.stderr


def test_non_full_sha_argument_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    _replace_field(approval, "EXPECTED_SHA", "abc123")

    result = _run_gate(repo, approval, env, expected_sha="abc123")

    _assert_authorization_refused(result, call_log)
    assert "not a full commit SHA" in result.stderr


def test_already_deployed_sha_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["CURRENT_SHA"] = EXPECTED_SHA

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "already deployed" in result.stderr


def test_external_reviewed_tools_can_gate_an_older_production_checkout(
    tmp_path: Path,
) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    tools, approved_manifest = _external_tools(tmp_path, repo)
    env["APPROVED_MANIFEST_PATH"] = str(approved_manifest)
    (repo / "src/project_mai_tai/deploy_preflight.py").unlink()
    (repo / "ops/preflight/preflight_oms_restart.sh").unlink()
    (repo / "ops/systemd/deploy_service.sh").unlink()

    result = _run_gate(repo, approval, env, tools_dir=tools)

    assert result.returncode == 0, result.stderr
    calls = _calls(call_log)
    assert f"git:-C {repo} show {EXPECTED_SHA}:ops/systemd/deploy_gate_tools.sha256" in calls
    assert any(
        line.startswith(f"preflight:{tools}/src/project_mai_tai/deploy_preflight.py")
        for line in calls
    )
    assert "fence" in calls
    assert f"deploy:{EXPECTED_SHA}:{repo} main oms" in calls
    assert f"tools_dir={tools}" in result.stdout


def test_external_tools_for_a_different_sha_refuse_before_preflight(
    tmp_path: Path,
) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    tools, _approved_manifest = _external_tools(tmp_path, repo)
    (tools / "DEPLOY_GATE_SOURCE_SHA").write_text("b" * 40 + "\n", encoding="utf-8")

    result = _run_gate(repo, approval, env, tools_dir=tools)

    _assert_authorization_refused(result, call_log)
    assert "do not name the approved target SHA" in result.stderr


def test_missing_approved_manifest_object_refuses_before_preflight(
    tmp_path: Path,
) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    tools, _approved_manifest = _external_tools(tmp_path, repo)
    env["APPROVED_MANIFEST_PATH"] = str(tmp_path / "missing-approved-manifest")

    result = _run_gate(repo, approval, env, tools_dir=tools)

    _assert_authorization_refused(result, call_log)
    assert "approved deploy-gate manifest object is missing" in result.stderr


def test_tool_and_local_manifest_rewrite_refuse_against_approved_commit(
    tmp_path: Path,
) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    tools, approved_manifest = _external_tools(tmp_path, repo)
    env["APPROVED_MANIFEST_PATH"] = str(approved_manifest)
    deploy = tools / "ops/systemd/deploy_service.sh"
    deploy.write_text(deploy.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    local_manifest = tools / "ops/systemd/deploy_gate_tools.sha256"
    local_manifest.write_text(
        "".join(
            f"{hashlib.sha256((tools / relative).read_bytes()).hexdigest()}  {relative}\n"
            for relative in DEPLOY_GATE_PAYLOAD
        ),
        encoding="utf-8",
    )

    result = _run_gate(repo, approval, env, tools_dir=tools)

    _assert_authorization_refused(result, call_log)
    assert "approved deploy-gate manifest" in result.stderr


def test_consumed_approval_cannot_be_reused(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    first = _run_gate(repo, approval, env)
    calls_after_first = _calls(call_log)

    second = _run_gate(repo, approval, env)

    assert first.returncode == 0
    assert second.returncode == 3
    assert "DEPLOY NOT AUTHORISED" in second.stdout
    assert _calls(call_log) == calls_after_first
    assert sum(line.startswith("deploy:") for line in calls_after_first) == 1


def test_preflight_refusal_never_reaches_fence_or_deploy(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["PREFLIGHT_RC"] = "1"

    result = _run_gate(repo, approval, env)

    assert result.returncode == 1
    assert _calls(call_log) == [
        f"git:-C {repo} rev-parse HEAD",
        f"preflight:{repo}/src/project_mai_tai/deploy_preflight.py --service oms "
        "--overview-url http://127.0.0.1:8100/api/overview",
    ]
    assert "one-shot live OMS preflight" in result.stderr


def test_managed_position_fence_refusal_never_reaches_deploy(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FENCE_RC"] = "2"

    result = _run_gate(repo, approval, env)

    assert result.returncode == 2
    assert _calls(call_log) == [
        f"git:-C {repo} rev-parse HEAD",
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
        f"git:-C {repo} rev-parse HEAD",
        f"preflight:{repo}/src/project_mai_tai/deploy_preflight.py --service oms "
        "--overview-url http://127.0.0.1:8100/api/overview",
        "fence",
        f"git:-C {repo} fetch origin main",
        f"git:-C {repo} rev-parse origin/main",
        f"deploy:{EXPECTED_SHA}:{repo} main oms",
        f"git:-C {repo} rev-parse HEAD",
    ]
    assert not approval.exists()
    assert len(list(approval.parent.glob(f"{approval.name}.used-*"))) == 1
    assert "[DEPLOY-APPROVAL-CONSUMED]" in result.stdout
    assert f"[DEPLOY-COMPLETE] deployment=oms-strategy sha={EXPECTED_SHA}" in result.stdout


def test_gate_refuses_when_origin_main_moved_after_the_approval(tmp_path: Path) -> None:
    # Gate-level pin. `deploy_service.sh` has its own main-moved refusal (tested below), but the
    # gate must refuse FIRST: by then the approval is the only thing standing between a stale GO
    # and a restart, so it has to survive the refusal unconsumed and deploy must never be called.
    repo, approval, call_log, env = _fixture(tmp_path)
    moved = "d" * 40
    env["REMOTE_MAIN_SHA"] = moved

    result = _run_gate(repo, approval, env)

    assert result.returncode == 3
    assert "DEPLOY NOT AUTHORISED" in result.stdout
    assert f"origin/main moved after approval: expected {EXPECTED_SHA}, found {moved}" in (
        result.stdout + result.stderr
    )
    calls = _calls(call_log)
    # the refusal comes AFTER both preflights and the fetch - it is the last look before consuming
    assert calls[-2:] == [
        f"git:-C {repo} fetch origin main",
        f"git:-C {repo} rev-parse origin/main",
    ]
    assert "fence" in calls
    assert not any(line.startswith("deploy") for line in calls)
    assert approval.exists(), "a refused approval must stay usable for the corrected run"
    assert list(approval.parent.glob(f"{approval.name}.used-*")) == []
    assert "[DEPLOY-APPROVAL-CONSUMED]" not in result.stdout


def test_before_1605_et_refuses_before_preflight(tmp_path: Path) -> None:
    repo, approval, call_log, env = _fixture(tmp_path)
    env["FAKE_ET_TIME"] = "1604"

    result = _run_gate(repo, approval, env)

    _assert_authorization_refused(result, call_log)
    assert "16:05 ET" in result.stderr


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
    subprocess.run(["git", "init", "-b", "main", author], check=True, capture_output=True)
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
