from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "ops/health/momlive1_owner_compat.py"
SPEC = importlib.util.spec_from_file_location("momlive1_owner_compat_test", PATH)
COMPAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPAT)
NOW = 1791997200000000000
ACCOUNTS = {"live:schwab_1m_v2", "live:orb"}


def proof(enabled=True):
    return {"version": 1, "source_service": "oms", "producer_instance_id": "123:456",
            "strategy_code": "momentum_warn_live", "observed_ns": NOW,
            "restore_ready": True, "entry_admission_enabled": enabled,
            "account_names": sorted(ACCOUNTS), "quote_symbols": ["SCNI"],
            "episodes": [{"episode_id": "00000000-0000-0000-0000-000000000001",
                          "symbol": "SCNI", "account_name": "live:orb", "state": "held",
                          "filled_quantity": "112", "exited_quantity": "0"}]}


def validate(row, symbols=None, enabled=True):
    return COMPAT.validate_live_owner({"SCNI"} if symbols is None else symbols, row,
                                     enabled=enabled, now_ns=NOW, max_age_ns=30_819_000_000,
                                     expected_instance="123:456", expected_accounts=ACCOUNTS)


def test_exact_owner_is_scope_admission_not_health():
    assert validate(proof())["status"] == "ADMITTED_OWNER_SCOPE_ONLY"


@pytest.mark.parametrize("state", sorted(COMPAT.ACTIVE))
def test_manual_off_retains_owned_protection_quotes(state):
    row = proof(False)
    row["episodes"][0]["state"] = state
    assert validate(row, enabled=False)["mode"] == "owned_exit_or_idle_only"


@pytest.mark.parametrize("enabled", [True, False])
def test_empty_owner_has_verified_empty_restore(enabled):
    row = proof(enabled)
    row.update(quote_symbols=[], episodes=[])
    assert validate(row, symbols=set(), enabled=enabled)["episode_count"] == 0


@pytest.mark.parametrize("key,value", [
    ("version", True), ("version", 2), ("source_service", "strategy-engine"),
    ("producer_instance_id", "123:999"), ("strategy_code", "schwab_1m_v2"),
    ("restore_ready", False), ("entry_admission_enabled", "true"),
    ("observed_ns", NOW + 1), ("observed_ns", NOW - 30_819_000_001),
    ("account_names", ["live:orb", "foreign"]), ("quote_symbols", ["SCNI", "SCNI"]),
])
def test_unproven_source_never_admitted(key, value):
    row = proof()
    row[key] = value
    with pytest.raises(ValueError):
        validate(row)


@pytest.mark.parametrize("key,value", [
    ("episode_id", "bad"), ("account_name", "foreign"), ("symbol", "GLND"),
    ("state", "closed"), ("filled_quantity", "NaN"), ("filled_quantity", "-1"),
    ("exited_quantity", "113"), ("filled_quantity", None),
])
def test_foreign_or_corrupt_episode_never_admitted(key, value):
    row = proof()
    row["episodes"][0][key] = value
    with pytest.raises(ValueError):
        validate(row)


def test_two_owned_symbols_and_three_refused():
    row = proof()
    second = deepcopy(row["episodes"][0])
    second.update(episode_id="00000000-0000-0000-0000-000000000002", symbol="DLXY")
    row["episodes"].append(second)
    row["quote_symbols"].append("DLXY")
    assert validate(row, symbols={"SCNI", "DLXY"})["episode_count"] == 2
    with pytest.raises(ValueError):
        validate(row, symbols={"SCNI", "DLXY", "GENK"})


def test_unowned_symbol_and_flag_mismatch_refuse():
    with pytest.raises(ValueError):
        validate(proof(), symbols={"SCNI", "GENK"})
    with pytest.raises(ValueError):
        validate(proof(), enabled=False)


def test_duplicate_episode_refuses():
    row = proof()
    row["episodes"].append(deepcopy(row["episodes"][0]))
    with pytest.raises(ValueError):
        validate(row)


@pytest.mark.parametrize("raw", [None, "[1]", "bad", "x" * 16_385])
def test_missing_malformed_or_oversize_proof_refuses(raw):
    with pytest.raises(ValueError):
        COMPAT.decode_proof(raw)


def test_actual_decode_and_inclusive_age_boundary():
    row = COMPAT.decode_proof(json.dumps(proof()))
    row["observed_ns"] = NOW - 30_819_000_000
    assert validate(row)["episode_count"] == 1


@pytest.fixture
def daily_context(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import hashlib
    path = PATH.parent / "momwarn1_repair/optiona/G1/daily_guard.py"
    spec = importlib.util.spec_from_file_location("momlive1_daily_integration", path)
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    repo = PATH.parents[2]
    monkeypatch.setattr(daily, "REPO", repo)
    monkeypatch.setattr(daily.wall_time, "time_ns", lambda: NOW)
    unit = {"MainPID": "123", "ActiveState": "active", "NRestarts": "0",
            "ExecMainStartTimestamp": "valid-existing-unit-start"}
    monkeypatch.setattr(daily, "state", lambda name: dict(unit))
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123/stat").write_text("123 (oms (name)) " + " ".join(["S"] + ["0"] * 18 + ["456"]))
    settings = SimpleNamespace(momentum_live_enabled=True, redis_stream_prefix="mai_tai",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb")
    guard = SimpleNamespace(HEARTBEAT_AGE_BOUND=30.819)
    manifest = {"momlive_owner_compat_sha256": hashlib.sha256(PATH.read_bytes()).hexdigest()}
    class Redis:
        def __init__(self):
            self.row = proof()
            self.reads = 0
        def xrevrange(self, name, count):
            assert name == "mai_tai:momentum-live-owner-proof" and count == 1
            self.reads += 1
            return [("1-0", {"data": json.dumps(self.row)})]
    owners = {name: set() for name in
              ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper", "momentum-warning")}
    owners["momentum-live"] = {"SCNI"}
    return daily, manifest, settings, Redis(), guard, owners, proc, unit


def scope(context):
    daily, manifest, settings, redis, guard, owners, proc, _ = context
    return daily.verify_live_owner_scope(manifest, settings, redis, guard, owners, proc_root=proc)


def test_daily_chain_verifies_then_preserves_old_owner_gate(daily_context):
    daily, _, _, redis, _, owners, _, _ = daily_context
    reduced = scope(daily_context)
    assert redis.reads == 1 and "momentum-live" not in reduced
    assert owners["momentum-live"] == {"SCNI"}
    daily.verify_owners(reduced, warning_enabled=True)
    reduced["unreviewed-owner"] = set()
    with pytest.raises(ValueError):
        daily.verify_owners(reduced, warning_enabled=True)


def test_daily_chain_keeps_warning_cap(daily_context):
    reduced = scope(daily_context)
    reduced["momentum-paper"] = {f"S{i}" for i in range(17)}
    with pytest.raises(ValueError):
        daily_context[0].verify_owners(reduced, warning_enabled=True)


def test_daily_chain_requires_restore_proof_even_when_enabled_owner_absent(daily_context):
    daily_context[5].pop("momentum-live")
    daily_context[3].row.update(quote_symbols=[], episodes=[], restore_ready=False)
    with pytest.raises(ValueError):
        scope(daily_context)


def test_daily_chain_disabled_owner_absent_preserves_original_without_new_io(daily_context):
    daily_context[2].momentum_live_enabled = False
    daily_context[5].pop("momentum-live")
    assert scope(daily_context) == daily_context[5]
    assert daily_context[3].reads == 0


def test_daily_chain_disabled_held_owner_preserves_quotes(daily_context):
    daily_context[2].momentum_live_enabled = False
    daily_context[3].row["entry_admission_enabled"] = False
    assert "momentum-live" not in scope(daily_context)
    assert daily_context[3].reads == 1


@pytest.mark.parametrize("kind", ["source", "instance", "manifest", "pid", "account"])
def test_daily_chain_cannot_bypass_actual_scope_checks(daily_context, kind):
    _, manifest, settings, redis, _, _, _, unit = daily_context
    if kind == "source":
        redis.row["source_service"] = "strategy-engine"
    elif kind == "instance":
        redis.row["producer_instance_id"] = "123:999"
    elif kind == "manifest":
        manifest["momlive_owner_compat_sha256"] = "wrong"
    elif kind == "pid":
        unit["MainPID"] = "0"
    else:
        settings.strategy_schwab_1m_v2_account_name = "foreign-account"
    with pytest.raises(ValueError):
        scope(daily_context)
