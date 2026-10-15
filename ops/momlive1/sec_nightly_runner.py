"""Single-flight, after-close SEC preparation. No order or service operations."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, time, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BOUND = 65_536


def target_session(now, holidays):
    if now.tzinfo is None:
        raise ValueError("aware_clock_required")
    local = now.astimezone(ET)
    if time(2) <= local.time() < time(16):
        raise ValueError("nightly_start_window_closed")
    years = {day.year for day in holidays}
    day = local.date() + (timedelta(days=1) if local.time() >= time(16) else timedelta())
    while True:
        if day.year not in years:
            raise ValueError("calendar_coverage_UNKNOWN")
        if day.weekday() < 5 and day not in holidays:
            return day
        day += timedelta(days=1)


def successful(report):
    if not isinstance(report, dict):
        return False
    requested = report.get("requested_symbols")
    completed = report.get("completed_symbols")
    index = report.get("sec_index_count")
    if (type(requested) is not int or not 0 < requested <= 30_000
            or type(completed) is not int or completed != requested
            or type(index) is not int or not requested <= index <= 40_000
            or type(report.get("pending_symbols")) is not int or report["pending_symbols"] != 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("sec_index_sha256", "")))):
        return False
    return (isinstance(report, dict) and report.get("status") == "FULL"
            and report.get("cohort") == "SEC_INDEX"
            and report.get("security_type") == "TYPE_UNKNOWN"
            and isinstance(report.get("http_errors"), dict)
            and type(report["http_errors"].get("403")) is int and report["http_errors"]["403"] == 0
            and type(report["http_errors"].get("429")) is int and report["http_errors"]["429"] == 0
            and isinstance(report.get("counts"), dict)
            # Fetch-failed names remain UNKNOWN; retain the diagnostic count.
            and type(report["counts"].get("FETCH_FAILED")) is int
            and 0 <= report["counts"]["FETCH_FAILED"] <= completed)


def atomic_receipt(path, value):
    raw = json.dumps(value, sort_keys=True).encode()
    if len(raw) > BOUND:
        raise ValueError("receipt_bound")
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run_job(repo, state, approved_head, *, clock, holidays, run=subprocess.run):
    if not re.fullmatch(r"[0-9a-f]{40}", approved_head):
        raise ValueError("exact_reviewed_head_required")
    day = target_session(clock(), holidays)
    if state.is_symlink():
        raise ValueError("private_state_symlink")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("private_state_permissions")
    lock_fd = os.open(state / "runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                     text=True, timeout=10, check=True).stdout.strip()
        if actual != approved_head:
            raise ValueError("reviewed_head_drift")
        run(["git", "diff", "--exit-code", "HEAD", "--",
             "src/project_mai_tai/momentum_live_sec.py",
             "src/project_mai_tai/services/momentum_live_sec_prepare.py",
             "ops/momlive1/sec_nightly_runner.py"], cwd=repo, capture_output=True,
            text=True, timeout=10, check=True)
        success_path = state / f"{day.isoformat()}-{approved_head}-success.json"
        if success_path.exists() or success_path.is_symlink():
            if success_path.is_symlink() or success_path.stat().st_size > BOUND:
                raise ValueError("success_receipt_UNKNOWN")
            prior = json.loads(success_path.read_bytes())
            if (prior.get("head") != approved_head or prior.get("lookup_day") != day.isoformat()
                    or prior.get("returncode") != 0 or not successful(prior.get("report"))):
                raise ValueError("success_receipt_UNKNOWN")
            return {"status": "SKIPPED_SUCCESS_ALREADY_RETAINED", "lookup_day": day.isoformat()}
        attempt_path = state / f"{day.isoformat()}-{approved_head}-attempt.json"
        if (success_path.with_suffix(".tmp").exists() or attempt_path.exists()
                or attempt_path.is_symlink() or attempt_path.with_suffix(".tmp").exists()):
            raise ValueError("prior_attempt_requires_review_no_auto_retry")
        started = clock()
        if target_session(started, holidays) != day:
            raise ValueError("session_changed_before_start")
        command = [str(repo / ".venv/bin/python"), "-B", "-m",
                   "project_mai_tai.services.momentum_live_sec_prepare",
                   "--cohort", "SEC_INDEX", "--security-type", "TYPE_UNKNOWN",
                   "--lookup-day", day.isoformat(), "--max-requests", "20000", "--max-seconds", "3600"]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repo / "src")}
        # A crash/timeout/partial run is an unknown attempt, not permission for
        # the timer to repeat a manual run later in the same session.
        atomic_receipt(attempt_path, {"head": approved_head, "lookup_day": day.isoformat(),
                                    "started_at": started.isoformat(), "status": "STARTED_UNMEASURED"})
        result = run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=3660, check=False)
        stdout, stderr = result.stdout.encode(), result.stderr.encode()
        if len(stdout) > BOUND or len(stderr) > BOUND:
            raise ValueError("job_output_bound")
        try:
            report = json.loads(stdout) if stdout.strip() else None
        except (ValueError, UnicodeError):
            report = None
        receipt = {"head": approved_head, "lookup_day": day.isoformat(),
                   "started_at": started.isoformat(), "ended_at": clock().isoformat(),
                   "returncode": result.returncode, "report": report,
                   "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                   "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                   "stderr_bytes": len(stderr), "stderr_text_retained": False}
        attempt = state / f"attempt-{started.astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        if attempt.exists():
            raise ValueError("attempt_receipt_already_exists")
        atomic_receipt(attempt, receipt)
        atomic_receipt(attempt_path, receipt)
        if result.returncode != 0 or not successful(report):
            return {"status": "INCOMPLETE_NO_AUTOMATIC_RETRY", "receipt": str(attempt)}
        atomic_receipt(success_path, receipt)
        return {"status": "SUCCESS", "receipt": str(success_path)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--approved-head", required=True)
    args = parser.parse_args()
    try:
        from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
        result = run_job(args.repo, args.state, args.approved_head,
                         clock=lambda: datetime.now(UTC), holidays=US_MARKET_HOLIDAYS)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] in {"SUCCESS", "SKIPPED_SUCCESS_ALREADY_RETAINED"} else 2
    except Exception as error:
        # No environment, provider response, contact or exception body in public logs.
        print(json.dumps({"status": "REFUSED_OR_UNMEASURED", "exception_class": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
