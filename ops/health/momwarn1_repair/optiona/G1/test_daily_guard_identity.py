"""Local controls only; fake service states, Redis and paper start/stop paths."""

from copy import deepcopy
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("optiona_local_guard", Path(__file__).with_name("daily_guard.py"))
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


@pytest.fixture
def identity(tmp_path):
    pin = {"version": 1, "unit": "project-mai-tai-market-data.service",
           "main_pid": 3036178, "start_ticks": 79383409,
           "exec_main_start_timestamp": "Tue 2026-10-13 02:11:59 UTC"}
    current = {"MainPID": "3036178", "ActiveState": "active", "NRestarts": "0",
               "ExecMainStartTimestamp": pin["exec_main_start_timestamp"]}
    proc = tmp_path / "proc"
    row = proc / str(pin["main_pid"])
    row.mkdir(parents=True)
    (row / "stat").write_text("3036178 (gateway (name)) " + " ".join(["S"] + ["0"] * 18 + ["79383409"]))
    return {"gateway_identity": pin}, current, proc


def test_exact_reviewed_instance_is_accepted(identity):
    manifest, state, proc = identity
    GUARD.verify_gateway_identity(manifest, state, proc_root=proc)


@pytest.mark.parametrize("field,value", [
    ("MainPID", "2907"), ("MainPID", "0"), ("ActiveState", "inactive"),
    ("ActiveState", "activating"), ("NRestarts", "1"), ("NRestarts", "unknown"),
    ("ExecMainStartTimestamp", "Tue 2026-10-13 04:00:00 UTC"),
    ("ExecMainStartTimestamp", ""),
])
def test_unit_drift_is_not_rebaselined_at_guard_runtime(identity, field, value):
    manifest, state, proc = identity
    state[field] = value
    before = deepcopy(manifest)
    with pytest.raises(ValueError):
        GUARD.verify_gateway_identity(manifest, state, proc_root=proc)
    assert manifest == before


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("unit", "another.service"),
    ("main_pid", "3036178"), ("main_pid", 0), ("main_pid", True),
    ("start_ticks", "79383409"), ("start_ticks", 0), ("start_ticks", True),
    ("exec_main_start_timestamp", ""), ("exec_main_start_timestamp", None),
])
def test_malformed_identity_fails_closed(identity, field, value):
    manifest, state, proc = identity
    manifest["gateway_identity"][field] = value
    with pytest.raises(ValueError):
        GUARD.verify_gateway_identity(manifest, state, proc_root=proc)


def test_missing_identity_does_not_capture_current_pid(identity):
    _, state, proc = identity
    with pytest.raises(ValueError):
        GUARD.verify_gateway_identity({}, state, proc_root=proc)


@pytest.mark.parametrize("kind", ["reused_pid", "zombie", "dead", "missing", "truncated"])
def test_kernel_process_identity_remains_required(identity, kind):
    manifest, state, proc = identity
    path = proc / "3036178/stat"
    if kind == "missing":
        path.unlink()
    elif kind == "truncated":
        path.write_text("3036178 (gateway) S")
    else:
        raw = path.read_text()
        path.write_text(raw.replace("79383409", "999") if kind == "reused_pid"
                        else raw.replace(" S ", " Z " if kind == "zombie" else " X "))
    with pytest.raises((ValueError, IndexError, OSError)):
        GUARD.verify_gateway_identity(manifest, state, proc_root=proc)


def test_manifest_artifact_tamper_still_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(GUARD, "ROOT", tmp_path)
    (tmp_path / "guard_rules.py").write_text("tampered")
    (tmp_path / "manifest.json").write_text(json.dumps({"artifacts": {"guard_rules.py": "0" * 64}}))
    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        GUARD.verify_manifest()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("case", ["valid", "owners_missing", "owner_cap", "heartbeat_stale",
                                   "heartbeat_unhealthy", "redis_unsafe", "inactive", "restarts"])
def test_actual_main_keeps_normal_stop_and_all_admission_guards(identity, monkeypatch, tmp_path, case, dry_run):
    manifest, current, proc = identity
    calls = []
    owners = {key: set() for key in ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper")}
    if case == "owners_missing":
        owners.pop("orb")
    if case == "owner_cap":
        owners["momentum-paper"] = {str(i) for i in range(17)}
    if case == "inactive":
        current["ActiveState"] = "inactive"
    if case == "restarts":
        current["NRestarts"] = "1"
    redis = SimpleNamespace(close=lambda: calls.append("redis_closed"))
    rules = SimpleNamespace(
        Blind=RuntimeError, HEARTBEAT_AGE_BOUND=10,
        _owners=lambda *_: owners,
        RedisSafety=lambda _: SimpleNamespace(sample=lambda: ("unsafe" if case == "redis_unsafe" else None, {})),
        LiveSignals=lambda *_: SimpleNamespace(heartbeat=lambda _: (
            "unhealthy" if case == "heartbeat_unhealthy" else "healthy",
            11 if case == "heartbeat_stale" else 1, {})),
        _audit=lambda *_: calls.append("audit"),
        run_guard=lambda *_: calls.append("sampler") or 0,
        stop_paper=lambda reason, *_: calls.append(reason) or 1,
    )
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: redis)))
    monkeypatch.setitem(sys.modules, "project_mai_tai.settings", SimpleNamespace(Settings=lambda **k: SimpleNamespace(redis_url="fixture", redis_stream_prefix="mai_tai", market_data_momentum_warning_enabled=False)))
    monkeypatch.setitem(sys.modules, "project_mai_tai.strategy_core.time_utils", SimpleNamespace(US_MARKET_HOLIDAYS=set()))
    monkeypatch.setattr(GUARD, "verify_manifest", lambda: manifest)
    monkeypatch.setattr(GUARD, "load_guard", lambda: rules)
    monkeypatch.setattr(GUARD, "state", lambda _: current)
    verify = GUARD.verify_gateway_identity
    monkeypatch.setattr(GUARD, "verify_gateway_identity", lambda m, s: verify(m, s, proc_root=proc))
    monkeypatch.setattr(GUARD, "Path", lambda value: tmp_path if value == "/home/trader/after-hours" else Path(value))
    monkeypatch.setattr(GUARD, "datetime", SimpleNamespace(
        now=lambda _: datetime(2026, 10, 13, 7, 40, tzinfo=UTC), combine=datetime.combine))
    if dry_run and case != "valid":
        with pytest.raises(RuntimeError):
            GUARD.main(["--dry-run-prestart"])
    else:
        GUARD.main(["--dry-run-prestart"] if dry_run else [])
    assert calls[-1] == "redis_closed"
    if dry_run:
        assert calls == ["redis_closed"]
        assert not (tmp_path / "2026-10-13").exists()
    elif case == "valid":
        assert "sampler" in calls and "scheduled_session_close" in calls
    else:
        assert "sampler" not in calls
        assert any(item.startswith("daily_guard_UNKNOWN:") for item in calls)


def owners():
    return {key: set() for key in ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper")}


def test_warning_flag_on_admits_known_sixth_owner():
    current = owners()
    current["momentum-warning"] = {"TEST"}
    GUARD.verify_owners(current, warning_enabled=True)


def test_warning_flag_off_refuses_even_empty_warning_owner():
    current = owners()
    current["momentum-warning"] = set()
    with pytest.raises(ValueError):
        GUARD.verify_owners(current, warning_enabled=False)


@pytest.mark.parametrize("enabled", [False, True])
def test_required_five_accept_without_optional_owner(enabled):
    GUARD.verify_owners(owners(), warning_enabled=enabled)


def test_unknown_seventh_owner_refuses():
    current = owners()
    current["momentum-warning"] = set()
    current["unknown"] = set()
    with pytest.raises(ValueError):
        GUARD.verify_owners(current, warning_enabled=True)


@pytest.mark.parametrize("missing", ["strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"])
def test_missing_core_owner_refuses_with_warning_enabled(missing):
    current = owners()
    current.pop(missing)
    current["momentum-warning"] = set()
    with pytest.raises(ValueError):
        GUARD.verify_owners(current, warning_enabled=True)


def test_union_17_refuses_not_independent_16_caps():
    current = owners()
    current["momentum-paper"] = {f"P{i}" for i in range(9)}
    current["momentum-warning"] = {f"W{i}" for i in range(8)}
    with pytest.raises(ValueError):
        GUARD.verify_owners(current, warning_enabled=True)


def test_shared_symbols_count_once_at_16_union():
    current = owners()
    current["momentum-paper"] = {f"P{i}" for i in range(16)}
    current["momentum-warning"] = {"P0"}
    GUARD.verify_owners(current, warning_enabled=True)


@pytest.mark.parametrize("value", [None, "true", 1])
def test_unreadable_flag_does_not_allow_warning_role(value):
    current = owners()
    current["momentum-warning"] = set()
    with pytest.raises(ValueError):
        GUARD.verify_owners(current, warning_enabled=value)
