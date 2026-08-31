from __future__ import annotations

from datetime import datetime
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = ROOT / "ops" / "health" / "scanner_capture_check.py"
WRAPPER_PATH = ROOT / "ops" / "health" / "scanner_capture_verify_cron.sh"
INSTALLER_PATH = ROOT / "ops" / "health" / "install_scanner_capture_verify.sh"

SPEC = importlib.util.spec_from_file_location("scanner_capture_check", CHECK_PATH)
assert SPEC is not None and SPEC.loader is not None
scanner_capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scanner_capture
SPEC.loader.exec_module(scanner_capture)


def _bash_path() -> str | None:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


def _shell_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _now() -> datetime:
    return datetime.fromisoformat("2026-08-31T09:00:00-04:00")


def _observation(
    *,
    rows: int,
    baseline: float = 28,
    sessions: int = 5,
) -> scanner_capture.DatabaseObservation:
    return scanner_capture.DatabaseObservation(
        row_count=rows,
        symbol_count=12,
        newest_row_at_et="2026-08-31 08:55:14" if rows else "-",
        baseline_median=baseline,
        baseline_sessions=sessions,
    )


def test_measured_monday_is_observed_against_matching_window_baseline() -> None:
    verdict = scanner_capture.classify(
        _observation(rows=37),
        now_et=_now(),
        feed_age_minutes=0,
    )

    assert verdict.status == "OBSERVED"
    assert verdict.exit_code == 0
    assert "rows=37" in verdict.line
    assert "same_weekday_baseline_median=28" in verdict.line
    assert "baseline_sessions=5" in verdict.line
    assert "current_vs_baseline=132.1%" in verdict.line
    assert 'window="2026-08-31 00:00-09:00:00 ET"' in verdict.line
    assert 'newest_row_et="2026-08-31 08:55:14"' in verdict.line


def test_low_volume_control_flips_on_only_the_measured_ratio() -> None:
    below = scanner_capture.classify(
        _observation(rows=5, baseline=28),
        now_et=_now(),
        feed_age_minutes=0,
    )
    at_bound = scanner_capture.classify(
        _observation(rows=6, baseline=28),
        now_et=_now(),
        feed_age_minutes=0,
    )

    assert below.status == "LOW_VOLUME"
    assert below.exit_code == 1
    assert "current_vs_baseline=17.9%" in below.line
    assert "cause=NOT_DETERMINED" in below.line
    assert "capture not writing" not in below.line.lower()
    assert at_bound.status == "OBSERVED"
    assert at_bound.exit_code == 0


def test_real_zero_with_a_nonzero_baseline_is_low_volume_not_unmeasured() -> None:
    verdict = scanner_capture.classify(
        _observation(rows=0),
        now_et=_now(),
        feed_age_minutes=0,
    )

    assert verdict.status == "LOW_VOLUME"
    assert "rows=0" in verdict.line
    assert "current_vs_baseline=0.0%" in verdict.line


def test_unreadable_database_never_becomes_a_measured_zero() -> None:
    def fail_database(_database_url: str, _now_et: datetime):
        raise RuntimeError("authentication failed")

    verdict = scanner_capture.run_check(
        now=_now(),
        database_url="postgresql://ignored",
        database_reader=fail_database,
        feed_reader=lambda **_kwargs: 0,
    )

    assert verdict.status == "COULD_NOT_TELL"
    assert verdict.exit_code == 2
    assert "database_read=FAILED" in verdict.line
    assert "row_count=UNMEASURED" in verdict.line
    assert "rows=0" not in verdict.line
    assert "capture not writing" not in verdict.line.lower()


def test_missing_baseline_population_is_could_not_tell() -> None:
    verdict = scanner_capture.classify(
        _observation(rows=37, baseline=-1, sessions=0),
        now_et=_now(),
        feed_age_minutes=0,
    )

    assert verdict.status == "COULD_NOT_TELL"
    assert verdict.exit_code == 2
    assert "baseline_sessions=0" in verdict.line
    assert "reason=baseline_population_insufficient" in verdict.line


def test_stale_independent_feed_does_not_accuse_capture() -> None:
    verdict = scanner_capture.classify(
        _observation(rows=1),
        now_et=_now(),
        feed_age_minutes=15,
    )

    assert verdict.status == "COULD_NOT_TELL"
    assert "reason=market_data_not_fresh" in verdict.line
    assert "cause=NOT_DETERMINED" not in verdict.line


def test_sql_uses_matching_weekday_same_cutoff_and_real_table() -> None:
    assert "EXTRACT(ISODOW FROM events.trade_date)" in scanner_capture.SQL
    assert "EXTRACT(ISODOW FROM params.today_et)" in scanner_capture.SQL
    assert "days.trade_date + params.cutoff_et" in scanner_capture.SQL
    assert "PERCENTILE_CONT(0.5)" in scanner_capture.SQL
    assert scanner_capture.SQL.count("scanner_confirmed_events") == 3


def test_database_password_is_environment_only_not_psql_argv() -> None:
    captured: dict[str, object] = {}

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "37|12|2026-08-31 08:55:14|28|5\n", "")

    observation = scanner_capture.read_database_observation(
        "postgresql+psycopg://mai_tai:secret-value@localhost:5432/project_mai_tai",
        _now(),
        runner=runner,
    )

    assert observation.row_count == 37
    assert "secret-value" not in " ".join(captured["command"])
    assert captured["env"]["PGPASSWORD"] == "secret-value"


def test_wrapper_sources_current_env_and_never_embeds_or_asserts_a_cause() -> None:
    source = WRAPPER_PATH.read_text(encoding="utf-8")

    assert "source \"$ENV_FILE\"" in source
    assert "PGPASSWORD=" not in source
    assert "capture not writing" not in source.lower()
    assert "cause=NOT_DETERMINED" in source
    assert "--fail-with-body" in source


def test_installer_preserves_existing_schedule_and_backs_up_box_artifact() -> None:
    source = INSTALLER_PATH.read_text(encoding="utf-8")

    assert 'schedule="0,30 12-15 * * 1-5 $production_target"' in source
    assert 'grep -Fxc "$schedule"' in source
    assert "pre-versioned-$stamp" in source
    assert 'cmp -s "$target" "$backup"' in source
    assert 'cmp -s "$source_cron" "$target"' in source
    assert "restart_required=0" in source


@pytest.mark.skipif(_bash_path() is None, reason="bash is required")
def test_installer_executes_real_copy_backup_and_schedule_readback(tmp_path: Path) -> None:
    test_root = tmp_path / "install-root"
    fake_bin = test_root / "fake-bin"
    fake_bin.mkdir(parents=True)
    target = test_root / "scanner_capture_verify_cron.sh"
    target.write_text("#!/usr/bin/env bash\n# box-only old copy\n", encoding="utf-8")
    target.chmod(0o755)
    crontab = fake_bin / "crontab"
    crontab.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "if [[ \"${1:-}\" != \"-l\" ]]; then exit 19; fi\n"
        "reads=0\n"
        "if [[ -f \"$FAKE_CRONTAB_READS\" ]]; then reads=$(cat \"$FAKE_CRONTAB_READS\"); fi\n"
        "reads=$((reads + 1)); printf '%s\\n' \"$reads\" > \"$FAKE_CRONTAB_READS\"\n"
        "if [[ \"${FAKE_FAIL_SECOND_READ:-0}\" == 1 && \"$reads\" -ge 2 ]]; then\n"
        "  printf '%s\\n' '# schedule disappeared'; exit 0\n"
        "fi\n"
        "printf '%s\\n' \"0,30 12-15 * * 1-5 /home/trader/scanner_capture_verify_cron.sh\"\n",
        encoding="utf-8",
    )
    crontab.chmod(0o755)
    python3 = fake_bin / "python3"
    python3.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python3.chmod(0o755)
    env = os.environ.copy()
    env["MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT"] = _shell_path(test_root)
    env["FAKE_CRONTAB_READS"] = _shell_path(test_root / "crontab-reads")

    result = subprocess.run(
        [str(_bash_path()), _shell_path(INSTALLER_PATH)],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == WRAPPER_PATH.read_bytes()
    backups = list((test_root / "backups").glob("scanner_capture_verify_cron.sh.pre-versioned-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "#!/usr/bin/env bash\n# box-only old copy\n"
    assert "managed_entries=1" in result.stdout
    assert "restart_required=0" in result.stdout


@pytest.mark.skipif(_bash_path() is None, reason="bash is required")
def test_installer_restores_prior_box_artifact_when_readback_loses_schedule(tmp_path: Path) -> None:
    test_root = tmp_path / "install-root"
    fake_bin = test_root / "fake-bin"
    fake_bin.mkdir(parents=True)
    target = test_root / "scanner_capture_verify_cron.sh"
    original = b"#!/usr/bin/env bash\n# box-only old copy\n"
    target.write_bytes(original)
    target.chmod(0o755)
    crontab = fake_bin / "crontab"
    crontab.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "reads=0\n"
        "if [[ -f \"$FAKE_CRONTAB_READS\" ]]; then reads=$(cat \"$FAKE_CRONTAB_READS\"); fi\n"
        "reads=$((reads + 1)); printf '%s\\n' \"$reads\" > \"$FAKE_CRONTAB_READS\"\n"
        "if [[ \"$reads\" -ge 2 ]]; then printf '%s\\n' '# schedule disappeared'; exit 0; fi\n"
        "printf '%s\\n' \"0,30 12-15 * * 1-5 /home/trader/scanner_capture_verify_cron.sh\"\n",
        encoding="utf-8",
    )
    crontab.chmod(0o755)
    python3 = fake_bin / "python3"
    python3.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    python3.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT": _shell_path(test_root),
            "FAKE_CRONTAB_READS": _shell_path(test_root / "crontab-reads"),
        }
    )

    result = subprocess.run(
        [str(_bash_path()), _shell_path(INSTALLER_PATH)],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "prior wrapper restored" in result.stderr
    assert target.read_bytes() == original
