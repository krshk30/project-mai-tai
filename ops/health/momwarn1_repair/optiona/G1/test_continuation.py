"""Controlled guard execution; no real sampler, paper, Redis or live proof."""

from datetime import UTC, date, datetime, time, timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
from test_daily_guard_identity import GUARD, identity as identity_fixture

identity = identity_fixture

SPEC = importlib.util.spec_from_file_location("continuation_rules", Path(__file__).with_name("guard_rules.py"))
rules = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rules
SPEC.loader.exec_module(rules)


def test_oct13_after07_new_baseline_hard1810_and_oct14_normal_defaults():
    now = datetime(2026, 10, 13, 15, 23, 4, 123456, tzinfo=UTC)
    start, end = rules.treatment_clock(now.date(), now, MomentumValidationWindow(), continuation=True)
    assert start == datetime(2026, 10, 13, 15, 23, 9, tzinfo=UTC)
    assert end.astimezone(rules.ET).time() == time(18, 10)
    next_day = date(2026, 10, 14)
    before07 = datetime(2026, 10, 14, 7, 40, tzinfo=UTC)
    start, end = rules.treatment_clock(next_day, before07, MomentumValidationWindow())
    assert start.astimezone(rules.ET).time() == time(7)
    assert end.astimezone(rules.ET).time() == time(9, 40)


@pytest.mark.parametrize("now,day", [
    (datetime(2026, 10, 14, 15, tzinfo=UTC), date(2026, 10, 14)),
    (datetime(2026, 10, 13, 15, tzinfo=UTC), date(2026, 10, 14)),
    (datetime(2026, 10, 13, 10, tzinfo=UTC), date(2026, 10, 13)),
    (datetime(2026, 10, 13, 22, 10, tzinfo=UTC), date(2026, 10, 13)),
])
def test_explicit_continuation_refuses_other_day_early_or_closed_clock(now, day):
    with pytest.raises(rules.Blind):
        rules.treatment_clock(day, now, MomentumValidationWindow(), continuation=True)


def test_normal_late_start_remains_refused():
    with pytest.raises(rules.Blind, match="at or after"):
        rules.treatment_clock(date(2026, 10, 14), datetime(2026, 10, 14, 11, tzinfo=UTC), MomentumValidationWindow())


@pytest.mark.parametrize("stop_rc", [0, 1])
def test_actual_continuation_running_stop_still_stops_paper_and_audits_gap(monkeypatch, tmp_path, stop_rc):
    now = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
    times = iter([now, now, now, now, now, now + timedelta(seconds=5)])
    monkeypatch.setattr(rules, "datetime", SimpleNamespace(
        now=lambda _: next(times, now + timedelta(seconds=5)), combine=datetime.combine))
    redis = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(rules.Redis, "from_url", lambda *_args, **_kwargs: redis)
    # Settings must retain the native constructor's local Redis field.
    monkeypatch.setattr(rules, "Settings", lambda **_: SimpleNamespace(
        redis_url="controlled", redis_stream_prefix="mai_tai"))
    signals = SimpleNamespace(sample_snapshot_id=lambda _: None, read_logs=lambda *_: None,
                              heartbeat=lambda _: ("unhealthy", 0, {}))
    monkeypatch.setattr(rules, "LiveSignals", lambda *_: signals)
    monkeypatch.setattr(rules, "RedisSafety", lambda _: SimpleNamespace(sample=lambda: (None, {})))
    monkeypatch.setattr(rules, "SlowdownRules", lambda: SimpleNamespace(heartbeat=lambda *_: "controlled_stop"))
    sampler = SimpleNamespace(pid=123, stderr=None, poll=lambda: None, terminate=lambda: None, wait=lambda **_: 0)
    monkeypatch.setattr(rules.subprocess, "Popen", lambda *_, **__: sampler)
    monkeypatch.setattr(rules, "SamplerEvidence", lambda *_, **__: SimpleNamespace(read=lambda _: None))
    monkeypatch.setattr(rules, "_route_sampler_exit", lambda *_: None)
    monkeypatch.setattr(rules, "_notify_systemd", lambda _: None)
    monkeypatch.setattr(rules.signal, "signal", lambda *_: None)
    monkeypatch.setattr(rules.time, "sleep", lambda _: None)
    monkeypatch.setattr(rules.os, "getloadavg", lambda: (0, 0, 0))
    audit, stops = [], []
    monkeypatch.setattr(rules, "_audit", lambda _, row: audit.append(row))
    monkeypatch.setattr(rules, "stop_paper", lambda reason, *_: stops.append(reason) or stop_rc)
    assert rules.run_guard(date(2026, 10, 13), tmp_path, continuation=True) == 1
    assert stops == ["controlled_stop"]
    assert "earlier gap NOT covered" in audit[0]["coverage"]
    assert audit[0]["hard_end_et"].endswith("18:10:00-04:00")


@pytest.mark.parametrize("case", ["valid", "identity", "owners", "union", "heartbeat", "redis", "restart"])
def test_continuation_actual_entry_keeps_fresh_prestart_checks(identity, monkeypatch, tmp_path, case):
    manifest, current, proc = identity
    owners = {k: set() for k in ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper", "momentum-warning")}
    if case == "owners":
        owners["unapproved"] = set()
    if case == "union":
        owners["momentum-warning"] = {str(n) for n in range(17)}
    if case == "identity":
        current["MainPID"] = "2907"
    if case == "restart":
        current["NRestarts"] = "1"
    calls, audit = [], []
    redis = SimpleNamespace(close=lambda: calls.append("closed"))
    settings = SimpleNamespace(redis_url="controlled", redis_stream_prefix="mai_tai",
        market_data_momentum_warning_enabled=True, momentum_validation_session_date=date(2026, 10, 13),
        market_data_momentum_warning_end_et=time(18), momentum_paper_detection_end_et=time(18),
        momentum_paper_guard_end_et=time(18, 10))
    guard = SimpleNamespace(Blind=rules.Blind, treatment_clock=rules.treatment_clock, HEARTBEAT_AGE_BOUND=10,
        _owners=lambda *_: owners, RedisSafety=lambda _: SimpleNamespace(sample=lambda:
            ("unsafe" if case == "redis" else None, {})),
        LiveSignals=lambda *_: SimpleNamespace(heartbeat=lambda _: ("healthy", 11 if case == "heartbeat" else 1, {})),
        _audit=lambda _, row: audit.append(row),
        run_guard=lambda *_, **kw: calls.append(("sampler", kw)) or 0,
        stop_paper=lambda reason, *_: calls.append(reason) or 1)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: redis)))
    monkeypatch.setitem(sys.modules, "project_mai_tai.settings", SimpleNamespace(Settings=lambda **_: settings))
    monkeypatch.setattr(GUARD, "verify_manifest", lambda: manifest)
    monkeypatch.setattr(GUARD, "load_guard", lambda: guard)
    monkeypatch.setattr(GUARD, "state", lambda _: current)
    verify = GUARD.verify_gateway_identity
    monkeypatch.setattr(GUARD, "verify_gateway_identity", lambda m, s: verify(m, s, proc_root=proc))
    monkeypatch.setattr(GUARD, "Path", lambda value: tmp_path if value == "/home/trader/after-hours" else Path(value))
    monkeypatch.setattr(GUARD, "datetime", SimpleNamespace(
        now=lambda _: datetime(2026, 10, 13, 15, tzinfo=UTC), combine=datetime.combine))
    GUARD.main([])  # Date-bound configuration selects continuation; service unit is unchanged.
    assert calls[-1] == "closed"
    if case == "valid":
        assert ("sampler", {"continuation": True}) in calls
        assert "scheduled_session_close" in calls
        assert audit[0]["end_et"].endswith("18:10:00-04:00")
    else:
        assert not any(isinstance(v, tuple) for v in calls)
        assert any(str(v).startswith("daily_guard_UNKNOWN:") for v in calls)


def test_explicit_oct14_entry_refused_even_when_dry_run(monkeypatch):
    settings = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "project_mai_tai.settings", SimpleNamespace(Settings=lambda **_: settings))
    monkeypatch.setattr(GUARD, "verify_manifest", lambda: {})
    monkeypatch.setattr(GUARD, "load_guard", lambda: rules)
    monkeypatch.setattr(GUARD, "datetime", SimpleNamespace(now=lambda _: datetime(2026, 10, 14, 15, tzinfo=UTC)))
    with pytest.raises(rules.Blind, match="another date"):
        GUARD.main(["--continue-oct13", "--dry-run-prestart"])
