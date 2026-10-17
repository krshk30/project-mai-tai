"""Authored-only application controls; no native service-manager acceptance.

All process/provider effects are replaced with controlled local fakes. These
controls do not establish installed wrapper provenance or native hook results.
"""
from datetime import UTC, date, datetime, time
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momwarn_validation_window import MomentumValidationWindow

ROOT = Path(__file__).resolve().parents[2]
G1 = ROOT / "ops/health/momwarn1_repair/optiona/G1"
ET = ZoneInfo("America/New_York")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


daily = load("momguard_nextbatch_daily", G1 / "daily_guard.py")
rules = load("momguard_nextbatch_rules", G1 / "guard_rules.py")


def forbidden(*args, **kwargs):
    raise AssertionError("operational side effect forbidden in this control")


@pytest.fixture
def condition(monkeypatch, tmp_path):
    import redis
    import project_mai_tai.settings as settings_module
    import project_mai_tai.strategy_core.time_utils as time_module

    clock = [datetime(2026, 10, 16, 12, tzinfo=ET)]
    settings = SimpleNamespace(momentum_validation_session_date=None)
    checks = []
    real_verify = daily.verify_manifest
    manifest = {"artifacts": {name: "controlled-hash-already-verified" for name in (
        "daily_guard.py", "guard_rules.py", "project-mai-tai-option-a-daily-guard.service")}}
    manifest["gateway_identity"] = {
        "version": 1, "unit": "project-mai-tai-market-data.service", "main_pid": 123,
        "start_ticks": 456, "exec_main_start_timestamp": "Fri 2026-10-16 12:00:00 UTC"}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            checks.append("clock")
            return clock[0].astimezone(tz) if tz else clock[0]

    def verified():
        checks.append("manifest")
        return manifest

    def configured(**kwargs):
        checks.append("settings")
        assert kwargs == {"_env_file": "/etc/project-mai-tai/project-mai-tai.env"}
        return settings

    original_latch = rules.require_no_safety_stop

    def latch():
        checks.append("latch")
        original_latch()

    monkeypatch.setattr(daily, "datetime", Clock)
    monkeypatch.setattr(daily, "verify_manifest", verified)
    monkeypatch.setattr(daily, "load_guard", lambda: rules)
    monkeypatch.setattr(settings_module, "Settings", configured)
    monkeypatch.setattr(time_module, "US_MARKET_HOLIDAYS", {date(2026, 12, 25)})
    monkeypatch.setattr(rules, "SAFETY_STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(rules, "require_no_safety_stop", latch)
    monkeypatch.setattr(redis.Redis, "from_url", forbidden)
    monkeypatch.setattr(daily, "prestart_checks", forbidden)
    monkeypatch.setattr(daily, "state", forbidden)
    monkeypatch.setattr(daily.os, "kill", forbidden)
    monkeypatch.setattr(rules.subprocess, "run", forbidden)
    monkeypatch.setattr(rules.subprocess, "check_output", forbidden)
    monkeypatch.setattr(rules, "_notify_systemd", forbidden)
    monkeypatch.setattr(rules, "_page", forbidden)
    monkeypatch.setattr(rules, "_audit", forbidden)
    monkeypatch.setattr(rules, "run_guard", forbidden)
    monkeypatch.setattr(rules, "retain_safety_stop", forbidden)
    return SimpleNamespace(clock=clock, settings=settings, checks=checks,
                           manifest=manifest, latch=rules.SAFETY_STOP_FILE,
                           real_verify=real_verify)


@pytest.mark.parametrize("stamp, expected", [
    ("2026-10-19T03:40:00", 0), ("2026-10-19T04:00:00", 0),
    ("2026-10-19T07:30:00", 0), ("2026-10-19T12:00:00", 0),
    ("2026-10-19T18:09:54", 0), ("2026-10-19T18:09:55", 255),
    ("2026-10-19T18:09:59", 255), ("2026-10-19T18:10:00", 1),
    ("2026-10-19T23:59:00", 1), ("2026-10-17T03:40:00", 1),
    ("2026-10-17T18:10:00", 1), ("2026-12-25T03:40:00", 1),
    ("2026-03-06T03:40:00", 0), ("2026-03-09T03:40:00", 0),
    ("2026-10-30T03:40:00", 0), ("2026-11-02T03:40:00", 0),
])
def test_condition_shared_clock_boundaries_not_manager_proof(condition, stamp, expected):
    condition.clock[0] = datetime.fromisoformat(stamp).replace(tzinfo=ET)
    assert daily.main(["--exec-condition"]) == expected
    assert condition.checks == ["manifest", "settings", "latch", "clock"]
    assert not condition.latch.exists()


def test_mon0340_keeps_original_0400_treatment_route(condition):
    condition.clock[0] = datetime(2026, 10, 19, 3, 40, tzinfo=ET)
    assert daily.main(["--exec-condition"]) == 0
    window = MomentumValidationWindow.from_settings(condition.settings)
    start, end = rules.treatment_clock(date(2026, 10, 19), condition.clock[0], window)
    assert start.astimezone(ET).time() == time(4)
    assert end.astimezone(ET).time() == time(18, 10)


def test_custom_end_and_historical_auto_continuation_unchanged(condition):
    condition.settings.momentum_paper_guard_end_et = time(19)
    condition.clock[0] = datetime(2026, 10, 19, 18, 10, tzinfo=ET)
    assert daily.main(["--exec-condition"]) == 0
    condition.clock[0] = datetime(2026, 10, 19, 19, tzinfo=ET)
    assert daily.main(["--exec-condition"]) == 1
    condition.settings.momentum_validation_session_date = date(2026, 10, 13)
    condition.clock[0] = datetime(2026, 10, 13, 7, tzinfo=ET)
    assert daily.main(["--exec-condition"]) == 0
    assert daily.continuation_session(condition.clock[0],
        MomentumValidationWindow.from_settings(condition.settings))


@pytest.mark.parametrize("hour", [12, 21])
@pytest.mark.parametrize("content", ["", "malformed", '{"session_date":"2026-10-15"}',
                                     '{"session_date":"2026-10-16"}'])
def test_any_retained_stop_inside_or_closed_fails_not_skips(condition, hour, content):
    condition.clock[0] = datetime(2026, 10, 16, hour, tzinfo=ET)
    condition.latch.write_text(content)
    assert daily.main(["--exec-condition"]) == 255
    assert condition.latch.read_text() == content
    assert "clock" not in condition.checks


@pytest.mark.parametrize("hour", [12, 21])
@pytest.mark.parametrize("mode", ["symlink", "unreadable", "ioerror"])
def test_stop_lookup_unknown_never_closed_skip(condition, monkeypatch, hour, mode):
    condition.clock[0] = datetime(2026, 10, 16, hour, tzinfo=ET)
    if mode == "symlink":
        condition.latch.symlink_to(condition.latch.parent / "missing")
    else:
        original = Path.lstat

        def lstat(path):
            if path == condition.latch:
                raise PermissionError("controlled") if mode == "unreadable" else OSError("controlled")
            return original(path)
        monkeypatch.setattr(Path, "lstat", lstat)
    assert daily.main(["--exec-condition"]) == 255
    assert "clock" not in condition.checks


@pytest.mark.parametrize("broken", ["manifest", "settings", "calendar", "empty-manifest",
                                   "identity-missing", "identity-clock"])
def test_config_unknown_before_closed_classification(condition, monkeypatch, broken):
    import project_mai_tai.settings as settings_module
    condition.clock[0] = datetime(2026, 10, 16, 21, tzinfo=ET)

    def fail(*args, **kwargs):
        raise ValueError("controlled invalid configuration")
    if broken == "manifest":
        monkeypatch.setattr(daily, "verify_manifest", fail)
    elif broken == "settings":
        monkeypatch.setattr(settings_module, "Settings", fail)
    elif broken == "calendar":
        monkeypatch.setattr(daily, "session_day", fail)
    elif broken == "empty-manifest":
        condition.manifest["artifacts"].clear()
    elif broken == "identity-missing":
        condition.manifest.pop("gateway_identity")
    else:
        condition.manifest["gateway_identity"]["exec_main_start_timestamp"] = "unknown clock"
    assert daily.main(["--exec-condition"]) == 255


def test_actual_manifest_mismatch_is_failure_before_skip(condition, monkeypatch, tmp_path):
    # Use the real verifier, not a mock that simply reports a hash mismatch.
    package = tmp_path / "package"
    package.mkdir()
    (package / "daily_guard.py").write_text("changed source")
    (package / "manifest.json").write_text(json.dumps({
        "artifacts": {"daily_guard.py": "0" * 64}}))
    monkeypatch.setattr(daily, "ROOT", package)
    monkeypatch.setattr(daily, "verify_manifest", condition.real_verify)
    assert daily.main(["--exec-condition"]) == 255


@pytest.mark.parametrize("args", [["--exec-condition", "--paper-prestart"],
                                  ["--exec-condition", "--continue-oct13"]])
def test_condition_has_no_operational_argument_combination(condition, args):
    assert daily.main(args) == 255
    assert not condition.checks


@pytest.mark.parametrize("change", ["clock", "latch"])
def test_condition_pass_is_not_authority_for_later_main(condition, change):
    assert daily.main(["--exec-condition"]) == 0
    if change == "clock":
        condition.clock[0] = datetime(2026, 10, 16, 18, 10, tzinfo=ET)
    else:
        condition.latch.write_text("new STOP between processes")
    # Main's own gate, not cached condition facts. This can still be a native
    # notify protocol failure; this control never asserts clean manager state.
    assert daily.main([]) == 78


def test_unit_retains_stoppost_on_skip_and_other_policy():
    unit = (ROOT / "ops/systemd/project-mai-tai-option-a-daily-guard.service").read_text()
    lines = unit.splitlines()
    condition_line = "ExecCondition=/home/trader/project-mai-tai/.venv/bin/python -B /home/trader/option-a-daily-guard/daily_guard.py --exec-condition"
    assert lines.count(condition_line) == 1
    assert "ExecStopPost=/bin/systemctl stop project-mai-tai-momentum-paper.service" in lines
    assert "ExecStartPost=/bin/systemctl start project-mai-tai-momentum-paper.service" in lines
    for line in ("Type=notify", "WatchdogSec=15s", "Restart=on-failure",
                 "RestartPreventExitStatus=78", "StartLimitBurst=3",
                 "OnFailure=project-mai-tai-option-a-daily-guard-failure.service"):
        assert line in lines
    assert "SuccessExitStatus=" not in unit
    # Unit text cannot establish actual ExecStopPost/owner/native behavior.


@pytest.fixture
def stop_case(monkeypatch, tmp_path):
    calls = []
    now = [0]
    audits = []
    pages = []
    c = SimpleNamespace(calls=calls, audits=audits, pages=pages, stop_rc=0,
                        release=True, audit_failure=None, page_results=[False, False],
                        owner_error=False, stop_error=None)
    c.real_page = rules._page
    log = tmp_path / "gateway.log"
    log.write_text("controlled pre-stop log\n")
    settings = SimpleNamespace(redis_stream_prefix="controlled")

    def owners(*args):
        if c.owner_error:
            raise OSError("controlled owner unknown")
        return {"momentum-paper": {"CONTROL"}}

    def command(argv, **kwargs):
        assert argv == ["systemctl", "stop", rules.PAPER_UNIT]
        assert kwargs["timeout"] == 35
        calls.append("stop")
        if c.stop_error:
            raise c.stop_error
        return SimpleNamespace(returncode=c.stop_rc)

    def release(*args, **kwargs):
        assert kwargs["before"] == {"momentum-paper": {"CONTROL"}}
        calls.append("release-proof-SHIM")
        return c.release

    def audit(path, item):
        audits.append(item)
        if c.audit_failure == item["action"]:
            raise OSError("controlled audit unknown")

    def page(*args, **kwargs):
        pages.append(kwargs.get("timeout", 15))
        result = c.page_results[min(len(pages) - 1, len(c.page_results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    class RedisSHIM:
        def xadd(self, stream, payload):
            calls.append(("fallback", stream, json.loads(payload["data"])))
            return "controlled-1"

    monkeypatch.setattr(rules, "GATEWAY_LOG", log)
    monkeypatch.setattr(rules, "_owners", owners)
    monkeypatch.setattr(rules, "_release_confirmed", release)
    monkeypatch.setattr(rules.subprocess, "run", command)
    monkeypatch.setattr(rules, "_audit", audit)
    monkeypatch.setattr(rules, "_page", page)
    monkeypatch.setattr(rules, "_notify_systemd", forbidden)
    monkeypatch.setattr(rules, "retain_safety_stop", forbidden)
    monkeypatch.setattr(rules.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(rules.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    c.args = ("controlled_failure", RedisSHIM(), settings, tmp_path / "audit.jsonl")
    return c


@pytest.mark.parametrize("results, expected_timeouts", [
    ([True], [15]), ([False, True], [15, 3]), ([False, False], [15, 3]),
    ([TimeoutError("controlled"), OSError("controlled")], [15, 3]),
])
def test_failure_page_only_keeps_proven_stop_zero(stop_case, caplog, results, expected_timeouts):
    c = stop_case
    c.page_results = results
    assert rules.stop_paper_for_failure(*c.args) == 0
    assert c.pages == expected_timeouts
    assert c.calls == ["stop", "release-proof-SHIM"]
    assert [row["action"] for row in c.audits] == [
        "stop_paper", "owner_release_confirmed", "page_delivery"]
    assert ("MOMGUARDEND1-PAGE-ERROR phase=delivery count=1" in caplog.text) == (len(results) > 1)


@pytest.mark.parametrize("failure", ["stop-nonzero", "stop-timeout", "owner", "owner-read",
                                    "stop_paper", "owner_release_confirmed", "page_delivery"])
@pytest.mark.parametrize("delivered", [True, False])
def test_real_stop_owner_or_audit_unknown_not_waived(stop_case, failure, delivered):
    c = stop_case
    c.page_results = [delivered, delivered]
    if failure == "stop-nonzero":
        c.stop_rc = 5
    elif failure == "stop-timeout":
        c.stop_error = rules.subprocess.TimeoutExpired("controlled", 35)
    elif failure == "owner":
        c.release = False
    elif failure == "owner-read":
        c.owner_error = True
    else:
        c.audit_failure = failure
    assert rules.stop_paper_for_failure(*c.args) != 0
    assert c.calls.count("stop") == 1
    for call in c.calls:
        if isinstance(call, tuple):
            _, stream, event = call
            assert stream == "controlled:market-data-subscriptions"
            assert event["payload"]["consumer_name"] == "momentum-paper"
            assert event["payload"]["symbols"] == []


@pytest.mark.parametrize("delivered, rc", [(True, 0), (False, 2)])
def test_legacy_public_stop_retains_page_policy_no_retry(stop_case, delivered, rc):
    stop_case.page_results = [delivered]
    assert rules.stop_paper(*stop_case.args) == rc
    assert stop_case.pages == [15]
    assert stop_case.calls == ["stop", "release-proof-SHIM"]


def test_failure_setup_exception_propagates_not_zero(stop_case, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("controlled setup failure")
    monkeypatch.setattr(rules.subprocess, "run", broken)
    with pytest.raises(RuntimeError, match="controlled setup failure"):
        rules.stop_paper_for_failure(*stop_case.args)


def test_generic_result_never_manufactures_complete_stop():
    page = rules.FailurePageResult(False, True, 2)
    for rc in (0, 2, 78):
        result = rules.PaperStopResult(rc, None, False, False, False, page, False)
        assert result.failure_exit_code() != 0
    assert rules.PaperStopResult(2, 0, True, True, True, page, False).failure_exit_code() == 2
    assert rules.PaperStopResult(78, 0, True, True, True, page, True).failure_exit_code() == 78


@pytest.mark.parametrize("status", [200, 503])
def test_real_page_transport_timeout_and_retry_without_network(stop_case, monkeypatch, status):
    # Undo only the fixture's _page override; keep every process/owner fake.
    timeouts = []

    class Response:
        def __enter__(self):
            return SimpleNamespace(status=status)

        def __exit__(self, *args):
            return False

    def urlopen(request, *, timeout):
        assert request.method == "POST"
        timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(rules.request, "urlopen", urlopen)
    monkeypatch.setattr(rules, "_page", stop_case.real_page)
    assert rules.stop_paper_for_failure(*stop_case.args) == 0
    assert timeouts == ([15] if status == 200 else [15, 3])
    assert stop_case.calls == ["stop", "release-proof-SHIM"]


def test_existing_safety_shutdown_still_latches_and_returns78(stop_case, monkeypatch):
    calls = []
    monkeypatch.setattr(rules, "retain_safety_stop", lambda *args: calls.append("latch"))
    monkeypatch.setattr(rules, "stop_paper", lambda *args: calls.append("stop") or 0)
    assert rules.safety_shutdown("real STOP", date(2026, 10, 16), *stop_case.args[1:]) == 78
    assert calls == ["latch", "stop"]


@pytest.fixture
def failure_wrapper(stop_case, monkeypatch, tmp_path):
    import redis
    import project_mai_tai.settings as settings_module

    calls = []
    client = stop_case.args[1]
    settings = stop_case.args[2]
    monkeypatch.setattr(client, "close", lambda: calls.append("close"), raising=False)

    def from_url(url, **kwargs):
        assert url == "controlled://no-network"
        assert kwargs == {"decode_responses": True, "socket_timeout": 5,
                          "socket_connect_timeout": 5}
        calls.append("client")
        return client

    def configured(**kwargs):
        assert kwargs == {"_env_file": "/etc/project-mai-tai/project-mai-tai.env"}
        calls.append("settings-fake-no-env-read")
        return settings

    settings.redis_url = "controlled://no-network"
    monkeypatch.setattr(settings_module, "Settings", configured)
    monkeypatch.setattr(redis, "Redis", SimpleNamespace(from_url=from_url))
    original_mkdir = Path.mkdir
    original_open = Path.open

    def local_mkdir(path, *args, **kwargs):
        if str(path).startswith("/home/trader/after-hours/"):
            assert kwargs == {"parents": True, "mode": 0o700}
            calls.append("output-path-fake-no-home-operation")
            return None
        return original_mkdir(path, *args, **kwargs)

    def local_open(path, *args, **kwargs):
        if str(path).startswith("/home/trader/after-hours/"):
            assert path.name == "option-a-guard.jsonl"
            return original_open(tmp_path / "wrapper-local-audit.jsonl", *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", local_mkdir)
    monkeypatch.setattr(Path, "open", local_open)

    def run(guard):
        monkeypatch.setitem(sys.modules, "daily_guard",
                            SimpleNamespace(ET=ET, load_guard=lambda: guard))
        return runpy.run_path(str(G1 / "failure_stop.py"), run_name="__main__")

    return SimpleNamespace(run=run, calls=calls, client=client, settings=settings)


def test_wrapper_exact_current_provenance_except_function_name():
    source = (G1 / "failure_stop.py").read_bytes()
    replacement = b"guard.stop_paper_for_failure("
    assert source.count(replacement) == 1
    current_beforeimage = source.replace(replacement, b"guard.stop_paper(")
    # Reviewer-retained CURRENT installed source, not the dated reference.
    assert hashlib.sha256(current_beforeimage).hexdigest() == (
        "d00e4a53992de9a01300ecf16e134a445ba86594f7a8c1fb85bc41ace7a7d58a")


@pytest.mark.parametrize("delivered", [True, False])
def test_wrapper_actual_shared_page_only_exit0_and_close_once(failure_wrapper, stop_case, delivered):
    stop_case.page_results = [delivered, delivered]
    with pytest.raises(SystemExit) as exc:
        failure_wrapper.run(rules)
    assert exc.value.code == 0
    assert stop_case.calls == ["stop", "release-proof-SHIM"]
    assert stop_case.pages == ([15] if delivered else [15, 3])
    assert failure_wrapper.calls == ["output-path-fake-no-home-operation",
        "settings-fake-no-env-read", "client", "close"]


@pytest.mark.parametrize("failure", ["stop", "owner", "stop_paper",
                                    "owner_release_confirmed", "page_delivery"])
def test_wrapper_actual_shared_unknown_nonzero_and_close(failure_wrapper, stop_case, failure):
    stop_case.page_results = [True]
    if failure == "stop":
        stop_case.stop_rc = 5
    elif failure == "owner":
        stop_case.release = False
    else:
        stop_case.audit_failure = failure
    with pytest.raises(SystemExit) as exc:
        failure_wrapper.run(rules)
    assert exc.value.code != 0
    assert stop_case.calls.count("stop") == 1
    assert failure_wrapper.calls.count("client") == failure_wrapper.calls.count("close") == 1


@pytest.mark.parametrize("outcome", [0, 2, 78, RuntimeError("controlled helper exception")])
def test_wrapper_only_calls_new_helper_once_and_preserves_cleanup(failure_wrapper, outcome):
    calls = []

    def stop(reason, client, settings, audit):
        assert reason == "daily_guard_process_died"
        assert client is failure_wrapper.client
        assert settings is failure_wrapper.settings
        assert str(audit).startswith("/home/trader/after-hours/")
        assert audit.name == "option-a-guard.jsonl"
        calls.append("new-helper")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    guard = SimpleNamespace(stop_paper_for_failure=stop, stop_paper=forbidden)
    expected = RuntimeError if isinstance(outcome, Exception) else SystemExit
    with pytest.raises(expected) as exc:
        failure_wrapper.run(guard)
    if expected is SystemExit:
        assert exc.value.code == outcome
    assert calls == ["new-helper"]
    assert failure_wrapper.calls.count("close") == 1
