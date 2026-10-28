from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SEED_WRAPPER = ROOT / "ops" / "health" / "seed_exposure_cron.sh"
PREOPEN_WRAPPER = ROOT / "ops" / "health" / "preopen_readiness_cron.sh"
PREOPEN_ALERT = ROOT / "ops" / "health" / "preopen_alert.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_alert(tmp_path: Path, *, exit_code: int = 0) -> Path:
    path = tmp_path / "alert.sh"
    _write_executable(
        path,
        "#!/bin/bash\n"
        f"printf 'CALL %s\\n' \"$*\" >> {str(tmp_path / 'alert.calls')!r}\n"
        f"exit {exit_code}\n",
    )
    return path


def _run_seed(
    tmp_path: Path, result: Path, code: int, alert: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SEED_EXPOSURE_TEST_MODE": "1",
        "SEED_EXPOSURE_OUT": str(tmp_path / "seed-state"),
        "SEED_EXPOSURE_RESULT_FILE": str(result),
        "SEED_EXPOSURE_RESULT_CODE": str(code),
        "SEED_EXPOSURE_ALERT": str(alert),
    }
    return subprocess.run(
        ["bash", str(SEED_WRAPPER)], check=False, capture_output=True, text=True, env=env
    )


def test_seed_exposure_pages_once_per_active_transition_and_recurs_after_clean(
    tmp_path: Path,
) -> None:
    result = tmp_path / "seed.txt"
    result.write_text("  VERDICT: EXPOSED count=1\n  TNON window=seed\n"
                      "  PAGEABLE : 1 watched_exposed_untruncated symbols=TNON\n", encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("  VERDICT: CLEAN count=0\n", encoding="utf-8")
    alert = _fake_alert(tmp_path)

    _run_seed(tmp_path, result, 1, alert)
    _run_seed(tmp_path, result, 1, alert)
    calls = tmp_path / "alert.calls"
    assert calls.read_text(encoding="utf-8").count("CALL") == 1

    _run_seed(tmp_path, clean, 0, alert)
    _run_seed(tmp_path, result, 1, alert)
    assert calls.read_text(encoding="utf-8").count("CALL") == 2


def test_seed_exposure_stored_gap_or_unknown_truncation_is_status_only(tmp_path: Path) -> None:
    result = tmp_path / "seed.txt"
    result.write_text("  VERDICT: EXPOSED count=1\n  OLOX window=250\n"
                      "  PAGEABLE : 0 watched_exposed_untruncated symbols=-\n", encoding="utf-8")
    alert = _fake_alert(tmp_path)
    assert _run_seed(tmp_path, result, 1, alert).returncode == 0
    assert not (tmp_path / "alert.calls").exists()
    assert "OLOX window=250" in (tmp_path / "seed-state/latest.txt").read_text()


def test_new_untruncated_symbol_changes_the_seed_page_key(tmp_path: Path) -> None:
    result = tmp_path / "seed.txt"
    alert = _fake_alert(tmp_path)
    for symbol in ("OLOX", "TNON"):
        result.write_text("  VERDICT: EXPOSED count=2\n  OLOX window=250\n  TNON window=250\n"
                          f"  PAGEABLE : 1 watched_exposed_untruncated symbols={symbol}\n")
        assert _run_seed(tmp_path, result, 1, alert).returncode == 0
    calls = (tmp_path / "alert.calls").read_text()
    assert calls.count("CALL RED") == 2
    assert "symbols=OLOX" in calls and "symbols=TNON" in calls


def test_seed_exposure_failed_delivery_does_not_consume_transition(tmp_path: Path) -> None:
    result = tmp_path / "seed.txt"
    result.write_text("  \u26d4 CANNOT SEE \u2014 REFUSING: no DSN\n", encoding="utf-8")

    failed = _run_seed(tmp_path, result, 2, _fake_alert(tmp_path, exit_code=1))
    assert failed.returncode == 1
    assert not (tmp_path / "seed-state" / f"active-{_today_et()}.txt").exists()


def _today_et() -> str:
    return subprocess.check_output(
        ["date", "+%F"], text=True, env={**os.environ, "TZ": "America/New_York"}
    ).strip()


def test_preopen_readiness_delivers_only_once_per_session(tmp_path: Path) -> None:
    readiness = tmp_path / "readiness.txt"
    readiness.write_text("VERDICT: GREEN ready\n", encoding="utf-8")
    seed = tmp_path / "seed.txt"
    seed.write_text("  VERDICT: CLEAN count=0\n", encoding="utf-8")
    alert = _fake_alert(tmp_path)
    env = {
        **os.environ,
        "PREOPEN_READINESS_TEST_MODE": "1",
        "PREOPEN_READINESS_OUT": str(tmp_path / "preopen-state"),
        "PREOPEN_READINESS_RESULT_FILE": str(readiness),
        "PREOPEN_READINESS_RESULT_CODE": "0",
        "PREOPEN_SEED_RESULT_FILE": str(seed),
        "PREOPEN_SEED_RESULT_CODE": "0",
        "PREOPEN_READINESS_ALERT": str(alert),
    }

    first = subprocess.run(["bash", str(PREOPEN_WRAPPER)], check=False, env=env)
    second = subprocess.run(["bash", str(PREOPEN_WRAPPER)], check=False, env=env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert (tmp_path / "alert.calls").read_text(encoding="utf-8").count("CALL") == 1


def test_preopen_stored_gap_does_not_elevate_ready_verdict(tmp_path: Path) -> None:
    readiness = tmp_path / "readiness.txt"
    readiness.write_text("VERDICT: GREEN ready\n")
    seed = tmp_path / "seed.txt"
    seed.write_text("  VERDICT: EXPOSED count=1\n"
                    "  PAGEABLE : 0 watched_exposed_untruncated symbols=-\n")
    result = subprocess.run(["bash", str(PREOPEN_WRAPPER)], check=False, capture_output=True, text=True,
        env={**os.environ, "PREOPEN_READINESS_TEST_MODE": "1",
             "PREOPEN_READINESS_OUT": str(tmp_path / "preopen-state"),
             "PREOPEN_READINESS_RESULT_FILE": str(readiness), "PREOPEN_READINESS_RESULT_CODE": "0",
             "PREOPEN_SEED_RESULT_FILE": str(seed), "PREOPEN_SEED_RESULT_CODE": "1",
             "PREOPEN_READINESS_ALERT": str(_fake_alert(tmp_path))})
    assert result.returncode == 0
    assert "CALL GREEN" in (tmp_path / "alert.calls").read_text()
    assert "seed-exposure: status only" in (tmp_path / "alert.calls").read_text()


def test_preopen_readiness_retries_an_undelivered_once_per_session_page(tmp_path: Path) -> None:
    readiness = tmp_path / "readiness.txt"
    readiness.write_text("VERDICT: RED not ready\n", encoding="utf-8")
    seed = tmp_path / "seed.txt"
    seed.write_text("  VERDICT: CLEAN count=0\n", encoding="utf-8")
    env = {
        **os.environ,
        "PREOPEN_READINESS_TEST_MODE": "1",
        "PREOPEN_READINESS_OUT": str(tmp_path / "preopen-state"),
        "PREOPEN_READINESS_RESULT_FILE": str(readiness),
        "PREOPEN_READINESS_RESULT_CODE": "2",
        "PREOPEN_SEED_RESULT_FILE": str(seed),
        "PREOPEN_SEED_RESULT_CODE": "0",
        "PREOPEN_READINESS_ALERT": str(_fake_alert(tmp_path, exit_code=1)),
    }

    failed = subprocess.run(["bash", str(PREOPEN_WRAPPER)], check=False, env=env)
    assert failed.returncode == 1
    assert not list((tmp_path / "preopen-state").glob("delivered-*"))

    env["PREOPEN_READINESS_ALERT"] = str(_fake_alert(tmp_path, exit_code=0))
    delivered = subprocess.run(["bash", str(PREOPEN_WRAPPER)], check=False, env=env)
    assert delivered.returncode == 0
    assert (tmp_path / "alert.calls").read_text(encoding="utf-8").count("CALL") == 2


def test_shared_preopen_adapter_fails_on_http_errors_and_is_time_bounded(tmp_path: Path) -> None:
    calls = tmp_path / "curl.calls"
    fake_curl = tmp_path / "curl.sh"
    _write_executable(
        fake_curl,
        f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {str(calls)!r}\nexit 22\n",
    )
    result = subprocess.run(
        ["bash", str(PREOPEN_ALERT), "RED", "not ready", "/tmp/detail"],
        check=False,
        env={
            **os.environ,
            "PREOPEN_ALERT_OUT": str(tmp_path / "adapter-state"),
            "PREOPEN_ALERT_CURL": str(fake_curl),
            "PREOPEN_ALERT_URL": "https://example.invalid/topic",
        },
    )

    assert result.returncode == 1
    assert "--fail-with-body --connect-timeout 10 --max-time 30" in calls.read_text(
        encoding="utf-8"
    )


def test_scheduled_callers_use_the_versioned_alert_adapter() -> None:
    seed_source = SEED_WRAPPER.read_text(encoding="utf-8")
    preopen_source = PREOPEN_WRAPPER.read_text(encoding="utf-8")

    assert "SEED_EXPOSURE_ALERT:-$REPO/ops/health/preopen_alert.sh" in seed_source
    assert "PREOPEN_READINESS_ALERT:-$REPO/ops/health/preopen_alert.sh" in preopen_source
    assert "SEED_EXPOSURE_ALERT:-/home/trader/preopen_alert.sh" not in seed_source
    assert "PREOPEN_READINESS_ALERT:-/home/trader/preopen_alert.sh" not in preopen_source
