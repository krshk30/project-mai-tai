from datetime import datetime
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sec_nightly_runner", ROOT / "ops/momlive1/sec_nightly_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
HEAD = "a" * 40
ET = ZoneInfo("America/New_York")


def clock(text="2026-10-14T20:30:00"):
    return datetime.fromisoformat(text).replace(tzinfo=ET)


@pytest.mark.parametrize("stamp,day", [
    ("2026-10-14T20:30:00", "2026-10-15"),
    ("2026-10-15T01:59:59", "2026-10-15"),
    ("2026-10-16T20:30:00", "2026-10-19"),
    ("2026-11-25T20:30:00", "2026-11-27"),
])
def test_target_is_next_trading_session_not_utc_or_blind_tomorrow(stamp, day):
    assert runner.target_session(clock(stamp), US_MARKET_HOLIDAYS).isoformat() == day


@pytest.mark.parametrize("stamp", ["2026-10-15T02:00:00", "2026-10-14T03:54:00", "2026-10-14T15:59:59"])
def test_late_or_market_hour_start_refused(stamp):
    with pytest.raises(ValueError, match="window_closed"):
        runner.target_session(clock(stamp), US_MARKET_HOLIDAYS)


def test_missing_calendar_coverage_and_naive_time_refused():
    with pytest.raises(ValueError, match="coverage_UNKNOWN"):
        runner.target_session(clock("2027-12-31T20:30:00"), US_MARKET_HOLIDAYS)
    with pytest.raises(ValueError, match="aware_clock"):
        runner.target_session(datetime(2026, 10, 14), US_MARKET_HOLIDAYS)


def report(**changes):
    return {"status": "FULL", "cohort": "SEC_INDEX", "security_type": "TYPE_UNKNOWN",
            "http_errors": {"403": 0, "429": 0}, "counts": {"FETCH_FAILED": 0},
            "sec_index_count": 2, "requested_symbols": 2, "completed_symbols": 2,
            "pending_symbols": 0, "sec_index_sha256": "b" * 64, **changes}


class Job:
    def __init__(self, data=None, returncode=0, head=HEAD):
        self.data = report() if data is None else data
        self.returncode = returncode
        self.head = head
        self.calls = []

    def __call__(self, args, **kwargs):
        if args[0] == "git":
            return SimpleNamespace(stdout=self.head + "\n")
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=self.returncode, stdout=json.dumps(self.data), stderr="")


def execute(tmp_path, job):
    return runner.run_job(tmp_path / "repo", tmp_path / "private", HEAD,
                          clock=clock, holidays=US_MARKET_HOLIDAYS, run=job)


def test_first_run_and_same_session_success_dedupe(tmp_path):
    job = Job()
    assert execute(tmp_path, job)["status"] == "SUCCESS"
    assert execute(tmp_path, job)["status"] == "SKIPPED_SUCCESS_ALREADY_RETAINED"
    assert len(job.calls) == 1
    args, kwargs = job.calls[0]
    assert args[args.index("--lookup-day") + 1] == "2026-10-15"
    assert args[args.index("--cohort") + 1] == "SEC_INDEX"
    assert args[args.index("--security-type") + 1] == "TYPE_UNKNOWN"
    assert kwargs["timeout"] == 3660 and kwargs["env"]["PYTHONPATH"] == str(tmp_path / "repo/src")


@pytest.mark.parametrize("data,code", [
    (report(status="PARTIAL"), 2),
    (report(http_errors={"403": 1, "429": 0}), 2),
    (report(http_errors={"403": 0, "429": 1}), 2),
    (report(counts={"FETCH_FAILED": 1}), 2),
    (report(security_type="COMMON"), 0),
    (report(http_errors={"403": False, "429": 0}), 0),
    (report(), 2),
    (report(requested_symbols=0, completed_symbols=0, sec_index_count=0), 0),
    (report(completed_symbols=1), 0),
    (report(pending_symbols=1), 0),
    (report(sec_index_count=True), 0),
    (report(sec_index_sha256=None), 0),
])
def test_full_or_failed_result_never_invents_success_or_retries(tmp_path, data, code):
    job = Job(data, code)
    result = execute(tmp_path, job)
    assert result["status"] == "INCOMPLETE_NO_AUTOMATIC_RETRY"
    assert len(job.calls) == 1
    assert len(list((tmp_path / "private").glob("attempt-*.json"))) == 1
    assert not list((tmp_path / "private").glob("*-success.json"))
    with pytest.raises(ValueError, match="no_auto_retry"):
        execute(tmp_path, job)
    assert len(job.calls) == 1


def test_source_head_drift_stops_before_network_job(tmp_path):
    job = Job(head="b" * 40)
    with pytest.raises(ValueError, match="head_drift"):
        execute(tmp_path, job)
    assert not job.calls


def test_corrupt_success_receipt_refuses_instead_of_replaying(tmp_path):
    job = Job()
    execute(tmp_path, job)
    saved = next((tmp_path / "private").glob("*-success.json"))
    saved.write_text(json.dumps({"head": HEAD, "lookup_day": "2026-10-15", "returncode": 0,
                                 "report": report(status="PARTIAL")}))
    with pytest.raises(ValueError, match="receipt_UNKNOWN"):
        execute(tmp_path, job)
    assert len(job.calls) == 1


def test_busy_runner_does_not_launch_second_job(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    with (state / "runner.lock").open("w") as lock:
        runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        job = Job()
        with pytest.raises(BlockingIOError):
            execute(tmp_path, job)
        assert not job.calls


def test_public_state_and_symlink_state_refused(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="permissions"):
        execute(tmp_path, Job())
    state.rmdir()
    state.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        execute(tmp_path, Job())


def test_malformed_job_stdout_still_retains_failed_attempt(tmp_path):
    job = Job()

    def malformed(args, **kwargs):
        if args[0] == "git":
            return job(args, **kwargs)
        job.calls.append((args, kwargs))
        return SimpleNamespace(stdout="not-json", stderr="sensitive text withheld", returncode=2)

    result = execute(tmp_path, malformed)
    saved = json.loads(Path(result["receipt"]).read_bytes())
    assert result["status"] == "INCOMPLETE_NO_AUTOMATIC_RETRY"
    assert saved["report"] is None and saved["stderr_text_retained"] is False
    assert "sensitive" not in Path(result["receipt"]).read_text()


def test_systemd_timer_has_no_daytime_catchup_or_automatic_retry():
    unit = (ROOT / "ops/momlive1/project-mai-tai-momlive-sec.service").read_text()
    timer = (ROOT / "ops/momlive1/project-mai-tai-momlive-sec.timer").read_text()
    assert "Type=oneshot" in unit and "Restart=no" in unit
    assert "StateDirectoryMode=0700" in unit and "CPUQuota=100%" in unit
    assert "${MOMLIVE_SEC_APPROVED_HEAD}" in unit
    assert "Persistent=false" in timer and "20:30:00 America/New_York" in timer


def test_ambiguous_success_temp_refuses_before_second_job(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    (state / f"2026-10-15-{HEAD}-success.tmp").write_text("unknown interrupted success")
    job = Job()
    with pytest.raises(ValueError, match="no_auto_retry"):
        execute(tmp_path, job)
    assert not job.calls


def test_timeout_retains_unknown_attempt_and_timer_cannot_retry(tmp_path):
    job = Job()

    def timeout(args, **kwargs):
        if args[0] == "git":
            return job(args, **kwargs)
        job.calls.append((args, kwargs))
        raise runner.subprocess.TimeoutExpired(args, 3660)

    with pytest.raises(runner.subprocess.TimeoutExpired):
        execute(tmp_path, timeout)
    marker = next((tmp_path / "private").glob("*-attempt.json"))
    assert json.loads(marker.read_bytes())["status"] == "STARTED_UNMEASURED"
    with pytest.raises(ValueError, match="no_auto_retry"):
        execute(tmp_path, timeout)
    assert len(job.calls) == 1


def test_dirty_reviewed_job_source_refuses_before_child(tmp_path):
    job = Job()

    def dirty(args, **kwargs):
        if args[:2] == ["git", "diff"]:
            raise runner.subprocess.CalledProcessError(1, args)
        return job(args, **kwargs)

    with pytest.raises(runner.subprocess.CalledProcessError):
        execute(tmp_path, dirty)
    assert not job.calls
