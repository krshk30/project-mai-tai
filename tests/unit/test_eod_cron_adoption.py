from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "health" / "eod_cron.sh"
INSTALLER = REPO_ROOT / "ops" / "health" / "install_eod_cron.sh"
def _bash() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    found = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert found is not None
    return found


def _shell_path(path: Path) -> str:
    resolved = path.resolve().as_posix()
    if len(resolved) >= 3 and resolved[1:3] == ":/":
        return f"/{resolved[0].lower()}/{resolved[3:]}"
    return resolved


def _fake_python(path: Path, *, complete: bool) -> None:
    verdict = "echo 'VERDICT eod day=2026-08-28 trips=0'" if complete else "echo partial"
    exit_code = 0 if complete else 7
    path.write_text(
        f"#!/usr/bin/env bash\n{verdict}\nexit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fake_curl(bin_dir: Path, *, exit_code: int = 0) -> None:
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s" "$*" > "${FAKE_CURL_ARGS:-/dev/null}"\n'
        f"if [[ {exit_code} -ne 0 && \" $* \" == *\" --fail-with-body \"* ]]; then\n"
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    force: bool = False,
    complete: bool = True,
    etmin: int = 1086,
    dow: int = 5,
    curl_exit: int = 0,
) -> subprocess.CompletedProcess:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    fake_python = tmp_path / "python"
    _fake_python(fake_python, complete=complete)
    _fake_curl(tmp_path, exit_code=curl_exit)
    (out / "eod_counts.py").write_text("# fake\n", encoding="utf-8")
    env = {
        "PATH": f"{_shell_path(tmp_path)}:/usr/bin:/bin",
        "EOD_OUT_DIR": _shell_path(out),
        "EOD_PYTHON_BIN": _shell_path(fake_python),
        "EOD_CURL_BIN": _shell_path(tmp_path / "curl"),
        "EOD_ENV_FILE": _shell_path(tmp_path / "missing.env"),
        "MAI_TAI_NTFY_URL": "https://ntfy.invalid/test",
        "MAI_TAI_EOD_TEST_MODE": "1",
        "MAI_TAI_EOD_TEST_DAY": "2026-08-28",
        "MAI_TAI_EOD_TEST_ETMIN": str(etmin),
        "MAI_TAI_EOD_TEST_ETDOW": str(dow),
        "MAI_TAI_EOD_TEST_STAMP": "fixed",
        "FAKE_CURL_ARGS": _shell_path(tmp_path / "curl.args"),
    }
    command = [_bash(), _shell_path(SCRIPT)] + (["--force"] if force else [])
    return subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=15, check=False
    )


def test_current_wrapper_and_installer_remain_executable() -> None:
    for relative in ("ops/health/eod_cron.sh", "ops/health/install_eod_cron.sh"):
        staged = subprocess.run(
            ["git", "ls-files", "--stage", "--", relative],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert staged.startswith("100755 ")


def test_crashed_report_does_not_latch_the_day_and_next_run_retries(tmp_path) -> None:
    first = _run(tmp_path, complete=False)
    canonical = tmp_path / "out" / "eod_2026-08-28.txt"
    assert first.returncode != 0
    assert not canonical.exists()
    assert not (tmp_path / "out" / "eod_2026-08-28.notified").exists()
    assert "eod_failed_2026-08-28" in (tmp_path / "curl.args").read_text(encoding="utf-8")

    second = _run(tmp_path, complete=True)
    assert second.returncode == 0
    assert "VERDICT eod" in canonical.read_text(encoding="utf-8")


def test_force_run_uses_scratch_artifact_and_cannot_poison_canonical_latch(tmp_path) -> None:
    forced = _run(tmp_path, force=True, complete=True)
    out = tmp_path / "out"
    assert forced.returncode == 0
    assert not (out / "eod_2026-08-28.txt").exists()
    assert not (out / "eod_2026-08-28.notified").exists()
    assert len(list(out.glob("eod_force_2026-08-28_fixed_*.txt"))) == 1

    scheduled = _run(tmp_path, complete=True)
    assert scheduled.returncode == 0
    assert (out / "eod_2026-08-28.txt").exists()


def test_notification_http_failure_is_not_silent(tmp_path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    fake_python = tmp_path / "python"
    _fake_python(fake_python, complete=True)
    _fake_curl(tmp_path, exit_code=22)
    (out / "eod_counts.py").write_text("# fake\n", encoding="utf-8")
    env = {
        "PATH": f"{_shell_path(tmp_path)}:/usr/bin:/bin",
        "EOD_OUT_DIR": _shell_path(out),
        "EOD_PYTHON_BIN": _shell_path(fake_python),
        "EOD_CURL_BIN": _shell_path(tmp_path / "curl"),
        "EOD_ENV_FILE": _shell_path(tmp_path / "missing.env"),
        "MAI_TAI_NTFY_URL": "https://ntfy.invalid/test",
        "MAI_TAI_EOD_TEST_MODE": "1",
        "MAI_TAI_EOD_TEST_DAY": "2026-08-28",
        "MAI_TAI_EOD_TEST_ETMIN": "1086",
        "MAI_TAI_EOD_TEST_ETDOW": "5",
    }
    result = subprocess.run(
        [_bash(), _shell_path(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "ntfy push failed" in (out / "eod.log").read_text(encoding="utf-8")
    assert not (out / "eod_2026-08-28.notified").exists()

    _fake_curl(tmp_path, exit_code=0)
    retry = subprocess.run(
        [_bash(), _shell_path(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert retry.returncode == 0
    assert (out / "eod_2026-08-28.notified").exists()


def test_missing_push_can_never_earn_the_success_marker(tmp_path) -> None:
    out = tmp_path / "out"
    marker = out / "eod_2026-08-28.notified"

    crashed = _run(tmp_path, complete=False, curl_exit=0)
    assert crashed.returncode != 0
    assert not marker.exists()

    real_report_push_failed = _run(tmp_path, complete=True, curl_exit=22)
    assert real_report_push_failed.returncode != 0
    assert not marker.exists()

    recovered = _run(tmp_path, complete=True, curl_exit=0)
    assert recovered.returncode == 0
    assert marker.exists()


def test_every_tick_is_observable_including_guard_skips(tmp_path) -> None:
    before = _run(tmp_path, etmin=600)
    assert before.returncode == 0
    log = (tmp_path / "out" / "eod.log").read_text(encoding="utf-8")
    assert "[EOD-CRON-TICK]" in log
    assert "reason=before_window" in log
    assert not (tmp_path / "curl.args").exists()


def test_shell_sources_contain_no_capability_topic_and_parse() -> None:
    assert "mai-tai-preopen" not in SCRIPT.read_text(encoding="utf-8")
    for shell_file in (SCRIPT, INSTALLER):
        result = subprocess.run(
            [_bash(), "-n", str(shell_file)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
