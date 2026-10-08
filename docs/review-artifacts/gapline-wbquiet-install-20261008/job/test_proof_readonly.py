"""Synthetic operating-system/evidence fixtures, never invented trading replays."""
import copy
from datetime import datetime, timedelta, timezone
import gzip
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("oct8_proof", Path(__file__).with_name("proof_readonly.py"))
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)
UTC = timezone.utc
SHA = "a" * 40
START = datetime(2026, 10, 8, 22, 0, tzinfo=UTC)


def logged(line, seconds=0):
    return (START + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S,000") + " INFO [oms-risk] " + line + "\n"


def records(text):
    return proof.log_records([{"path": "oms.log", "text": text}], START, START + timedelta(minutes=20))


def shadow(pass_id, pid=202, dropped=0):
    return logged("[WBQUIET-SHADOW] " + json.dumps({"pass_id": pass_id, "process_pid": pid,
        "dropped_observations": dropped, "policy_applied": False}), 3)


def sync_pair(pass_id=1, duration=100):
    return logged(f"[OMS-BROKER-SYNC-PASS] id={pass_id} phase=start accounts=all", 1) + shadow(pass_id) + logged(
        f"[OMS-BROKER-SYNC-PASS] id={pass_id} phase=end outcome=ok duration_ms={duration} exceeds_note_age=False", 4)


def identity(pid, invocation, start=START):
    return {"MainPID": pid, "NRestarts": 0, "ActiveState": "active", "SubState": "running", "Result": "success",
        "InvocationID": invocation, "ExecMainStartTimestamp": str(start), "start_utc": start.isoformat(),
        "ExecMainStartTimestampMonotonic": pid, "process_cwd": str(proof.REPO)}


@pytest.fixture
def good():
    before = {"captured_at_utc": (START - timedelta(minutes=1)).isoformat(), "errors": {},
              "services": {role: identity(100 + i, "old" + role) for i, role in enumerate(proof.SERVICES)},
              "process_flags": {}, "redis": {"evicted_keys": 0, "used_memory": 1000000}}
    after = copy.deepcopy(before)
    after.update({"captured_at_utc": (START + timedelta(minutes=11)).isoformat(), "checkout": {"sha": SHA, "clean": True},
        "scanner": {"verdict": "PASS"}, "bar_continuity": {"verdict": "MEASURED"},
        "transaction_rate": {"transactions_per_second": 1}, "logs": {},
        "journals": {role: {"error_count": 0} for role in proof.RESTARTED}})
    for i, role in enumerate(sorted(proof.RESTARTED)):
        after["services"][role] = identity(201 + i, "new" + role)
        after["logs"][role] = proof.summarize_logs(records(sync_pair() if role == "oms" else logged("healthy")))
    for role in ("oms", "schwab-1m-v2"):
        values = {key: ["true"] for key in proof.FLAG_KEYS}
        values.update({proof.LINE: ["true"], proof.HANDOFF: ["false"]})
        before["process_flags"][role] = {"values": values, "sha256": proof.digest(values)}
        after["process_flags"][role] = copy.deepcopy(before["process_flags"][role])
    return before, after


def test_complete_evidence_passes_without_granting_admission(good):
    result = proof.evaluate(*good, SHA)
    assert result["verdict"] == "PASS"
    assert "no trading admission" in result["scope"]


def test_paper_orb_unchanged_active_is_preserved_not_falsely_retired(good):
    before, after = good
    assert after['services']['orb'] == before['services']['orb']
    assert proof.evaluate(before, after, SHA)['verdict'] == 'PASS'
    after['services']['orb']['MainPID'] = 0
    after['services']['orb']['ActiveState'] = 'inactive'
    result = proof.evaluate(before, after, SHA)
    assert 'untouched_identity_changed:orb' in result['failures']
    assert proof.receipt_exit_code({'assessment': result}) == 1


def test_v2_worker_prefill_and_scanner_observations_are_not_fake_acceptance():
    summary = proof.summarize_logs(records(logged('[V2-ATR-PROBE] symbol=FLYE state=short')
        + logged('[OMS-FALSE-FLIP] reason=classification_unreadable')
        + logged('scanner added FLYE prefill warm alert')))
    assert summary['v2_by_symbol'] == {'FLYE': {'V2-ATR-PROBE': 1}}
    assert summary['observed_event_counts']['OMS-FALSE-FLIP'] == 1
    assert summary['keyword_line_counts'] == dict(prefill=1, warm=1, scanner=1, alert=1)
    assert summary['worker_or_scanner_acceptance'].startswith('UNMEASURED')


@pytest.mark.parametrize("role,key,values", [
    ("schwab-1m-v2", proof.GAP, ["false"]), ("schwab-1m-v2", proof.GAP, []),
    ("oms", proof.LINE, ["false"]), ("schwab-1m-v2", proof.LINE, ["false"]),
    ("oms", proof.HANDOFF, ["true"]), ("schwab-1m-v2", proof.HANDOFF, ["true"]),
    ("oms", proof.HANDOFF, ["false", "false"]),
])
def test_each_required_process_flag_is_deciding(good, role, key, values):
    good[1]["process_flags"][role]["values"][key] = values
    result = proof.evaluate(*good, SHA)
    assert result["verdict"] == "FAIL"
    assert "required_flag:" + role + ":" + key in result["failures"]


def test_retained_flag_hash_is_not_allowed_to_hide_changes(good):
    key = "MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED"
    good[1]["process_flags"]["oms"]["values"][key] = ["false"]
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


@pytest.mark.parametrize("key", sorted(proof.SLOT_FLAGS))
def test_old_oms_missing_slot_env_reloads_authorized_true_without_false_drift(good, key):
    good[0]["process_flags"]["oms"]["values"][key] = []
    result = proof.evaluate(*good, SHA)
    assert result["verdict"] == "PASS"
    assert "authorized_slot_env_loaded:oms:" + key in result["observations"]


@pytest.mark.parametrize("role", ["oms", "schwab-1m-v2"])
@pytest.mark.parametrize("key", sorted(proof.SLOT_FLAGS))
@pytest.mark.parametrize("values", [[], ["false"], ["true", "true"]])
def test_authorized_slot_env_must_be_true_on_each_new_process(good, role, key, values):
    good[0]["process_flags"][role]["values"][key] = []
    good[1]["process_flags"][role]["values"][key] = values
    result = proof.evaluate(*good, SHA)
    assert "required_flag:" + role + ":" + key in result["failures"]
    assert proof.receipt_exit_code({"assessment": result}) == 1


@pytest.mark.parametrize("field,value", [("NRestarts", 1), ("MainPID", 0), ("ActiveState", "failed"), ("SubState", "dead")])
def test_new_process_identity_requires_active_zero_restarts(good, field, value):
    good[1]["services"]["oms"][field] = value
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_untouched_gateway_changed_is_failure(good):
    good[1]["services"]["market-data"]["MainPID"] += 1
    assert "untouched_identity_changed:market-data" in proof.evaluate(*good, SHA)["failures"]


def test_successful_daily_paper_stop_is_observation_not_unchanged_claim(good):
    row = good[1]["services"]["momentum-paper"]
    row.update({"MainPID": 0, "ActiveState": "inactive", "SubState": "dead"})
    good[1]["paper_close_receipt"] = {"reason": "scheduled_session_close"}
    result = proof.evaluate(*good, SHA)
    assert result["verdict"] == "PASS"
    assert result["observations"] == ["daily_guard_paper_stop_observed_not_identity_unchanged:momentum-paper"]


def test_failed_paper_stop_never_uses_guard_exception(good):
    row = good[1]["services"]["momentum-paper"]
    row.update({"MainPID": 0, "ActiveState": "inactive", "SubState": "dead", "Result": "exit-code"})
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_paper_stop_without_positive_guard_receipt_never_admitted(good):
    good[1]["services"]["momentum-paper"].update({"MainPID": 0, "ActiveState": "inactive", "SubState": "dead"})
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_new_pid_journal_error_is_deciding(good):
    good[1]["journals"]["oms"]["error_count"] = 1
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_journal_receipt_filters_old_pid_and_never_exports_payload(monkeypatch):
    events = [{"_PID": "100", "MESSAGE": "ERROR old secret"},
              {"_PID": "201", "MESSAGE": "normal", "PRIORITY": "6"},
              {"_PID": "201", "MESSAGE": "ERROR new secret", "PRIORITY": "3", "__CURSOR": "public-cursor"}]
    monkeypatch.setattr(proof, "command", lambda *_: "\n".join(json.dumps(row) for row in events))
    result = proof.journal_receipt("oms", 201, START, START + timedelta(minutes=10))
    assert result["entries_for_new_pid"] == 2
    assert result["error_count"] == 1
    assert "secret" not in json.dumps(result)


def test_evictions_change_fails(good):
    good[1]["redis"]["evicted_keys"] = 1
    assert "redis_evictions_changed" in proof.evaluate(*good, SHA)["failures"]


def test_ten_minutes_not_yet_measured_is_unknown(good):
    good[1]["captured_at_utc"] = (START + timedelta(seconds=599)).isoformat()
    assert proof.evaluate(*good, SHA)["verdict"] == "UNKNOWN"


def test_partial_bar_and_telemetry_limits_never_gate_bookkeeping(good):
    before, after = good
    after["bar_continuity"]["verdict"] = "UNKNOWN"
    after.pop("transaction_rate")
    after["scanner"]["verdict"] = "UNKNOWN"
    after["logs"]["oms"]["complete_sync_ids"] = []
    after["assessment"] = proof.evaluate(before, after, SHA)
    assert after["assessment"]["verdict"] == "UNKNOWN"
    assert proof.receipt_exit_code(after) == 0


@pytest.mark.parametrize("failure", ["new_identity:oms", "required_flag:oms:KEY", "redis_evictions_changed",
    "post_start_journal_error:oms", "post_start_traceback_or_error:oms"])
def test_critical_measured_failures_return_one(failure):
    assert proof.receipt_exit_code({"assessment": {"failures": [failure]}}) == 1


@pytest.mark.parametrize("error", ["redis", "checkout", "service:oms", "proc:oms"])
def test_critical_read_unreadable_returns_two(error):
    assert proof.receipt_exit_code({"assessment": {}, "errors": {error: "unknown"}}) == 2


@pytest.mark.parametrize("error", ["database", "transaction_rate", "bar_continuity", "scanner", "logs:oms"])
def test_unmeasured_report_source_does_not_create_gate(error):
    assert proof.receipt_exit_code({"assessment": {}, "errors": {error: "unknown"}}) == 0


def test_stale_or_missing_evidence_is_unknown(good):
    good[1]["errors"]["database"] = "OperationalError"
    assert proof.evaluate(*good, SHA)["verdict"] == "UNKNOWN"


def test_log_cursor_normal_append_reads_only_new_bytes(tmp_path):
    path = tmp_path / "oms.log"
    path.write_text(logged("before"))
    cursor = proof.log_cursor(path)
    with path.open("a") as out:
        out.write(sync_pair())
    ranges = proof.log_ranges(cursor, directory=tmp_path)
    assert "before" not in ranges[0]["text"]
    assert proof.summarize_logs(records(ranges[0]["text"]))["complete_sync_ids"] == ["1"]


@pytest.mark.parametrize("compressed", [False, True])
def test_rotation_spans_exact_old_cursor_and_new_live_path(tmp_path, compressed):
    path = tmp_path / "oms.log"
    prefix = logged("before")
    path.write_text(prefix)
    cursor = proof.log_cursor(path)
    with path.open("a") as out:
        out.write(logged("[OMS-BROKER-SYNC-PASS] id=7 phase=start accounts=all", 1))
    rotated = tmp_path / ("oms.log-20261009.gz" if compressed else "oms.log-20261009")
    if compressed:
        with gzip.open(rotated, "wb") as stream:
            stream.write(path.read_bytes())
        path.unlink()
    else:
        path.rename(rotated)
    time.sleep(.002)
    path.write_text(shadow(7) + logged("[OMS-BROKER-SYNC-PASS] id=7 phase=end outcome=ok duration_ms=72", 4))
    ranges = proof.log_ranges(cursor, directory=tmp_path)
    assert [Path(item["path"]).name for item in ranges] == [rotated.name, "oms.log"]
    assert "before" not in ranges[0]["text"]
    report = proof.summarize_logs(proof.log_records(ranges, START, START + timedelta(minutes=10)))
    assert report["complete_sync_ids"] == ["7"]
    assert "shadows" not in report


def test_old_archive_population_not_materialized(tmp_path):
    for i in range(30):
        path = tmp_path / f"oms.log-{i:02}.gz"
        with gzip.open(path, "wb") as stream:
            stream.write(b"irrelevant" * 100)
        os.utime(path, ns=(1, 1))
    live = tmp_path / "oms.log"
    live.write_text(logged("before"))
    cursor = proof.log_cursor(live)
    with live.open("a") as stream:
        stream.write(logged("after"))
    assert len(proof.log_ranges(cursor, directory=tmp_path)) == 1


def test_cursor_lost_copytruncate_is_unknown(tmp_path):
    path = tmp_path / "oms.log"
    path.write_text(logged("before"))
    cursor = proof.log_cursor(path)
    path.write_text(logged("new"))
    with pytest.raises(proof.Unknown, match="baseline_log_cursor_not_unique"):
        proof.log_ranges(cursor, directory=tmp_path)


def test_duplicate_sync_not_deduplicated_into_pass(good):
    report = proof.summarize_logs(records(sync_pair() + logged("[OMS-BROKER-SYNC-PASS] id=1 phase=start", 1)))
    good[1]["logs"]["oms"] = report
    assert report["duplicate_pass_ids"] == ["1"]
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_p1_receipt_does_not_require_or_certify_future_wb_shadow_data(good):
    report = proof.summarize_logs(records(sync_pair(1) + logged("[OMS-BROKER-SYNC-PASS] id=2 phase=start", 1)
        + logged("[OMS-BROKER-SYNC-PASS] id=2 phase=end outcome=ok duration_ms=17", 4) + shadow(3, dropped=2)))
    good[1]["logs"]["oms"] = report
    assert "shadows" not in report and "dropped_observations_max" not in report
    assert proof.evaluate(*good, SHA)["verdict"] == "PASS"


def test_missing_sync_duration_is_unknown_not_fake_measurement(good):
    good[1]["logs"]["oms"] = proof.summarize_logs(records(sync_pair().replace(" duration_ms=100", "")))
    assert "sync_duration_measurement_incomplete" in proof.evaluate(*good, SHA)["unknown"]


def test_failed_sync_pass_reported_even_without_logger_error(good):
    good[1]["logs"]["oms"] = proof.summarize_logs(records(sync_pair().replace("outcome=ok", "outcome=failed")))
    assert "new_oms_sync_pass_failed" in proof.evaluate(*good, SHA)["failures"]


def test_sync_duration_max_and_p95_measured():
    report = proof.summarize_logs(records("".join(sync_pair(i, i) for i in range(1, 21))))
    assert report["sync_duration_ms"] == {"samples": 20, "max": 20, "p95": 19}


def test_old_process_error_excluded_new_traceback_counted(good):
    old = (START - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S,000") + " ERROR old failure\n"
    text = old + logged("new log") + "Traceback (most recent call last):\n  fake operating-system error\n"
    report = proof.summarize_logs(records(text))
    assert report["traceback_or_error_count"] == 1
    good[1]["logs"]["strategy"] = report
    assert proof.evaluate(*good, SHA)["verdict"] == "FAIL"


def test_untimestamped_record_before_any_timestamp_is_unknown():
    with pytest.raises(proof.Unknown, match="untimestamped_postcursor_log"):
        records("Traceback (most recent call last):\n")


def test_whitelisted_flags_do_not_export_secrets(monkeypatch):
    monkeypatch.setattr(proof, "bounded_file", lambda *_: b"SCHWAB_TOKEN=do-not-publish\0" + proof.GAP.encode() + b"=true\0")
    result = proof.process_flags(1)
    assert result["values"][proof.GAP] == ["true"]
    assert "do-not-publish" not in json.dumps(result)
    assert "SCHWAB_TOKEN" not in json.dumps(result)


def test_duplicate_flag_not_hidden(monkeypatch):
    monkeypatch.setattr(proof, "bounded_file", lambda *_: (proof.GAP.encode() + b"=true\0") * 2)
    assert proof.process_flags(1)["values"][proof.GAP] == ["true", "true"]


def overview(at=START, status="healthy"):
    return {"errors": [], "services": [{"service_name": "strategy-engine", "observed_at_raw": at.isoformat(),
        "effective_status": status, "details": {"main_loop_health": "healthy", "main_loop_exceptions_total": "0"}}],
        "scanner": {"status": "active"}}


def test_scanner_freshness_not_fake_rule9b_pass():
    result = proof.scanner_receipt(overview(), START + timedelta(seconds=30))
    assert result["verdict"] == "PASS"
    assert result["rule_9b_acceptance"] == "UNMEASURED"
    assert proof.scanner_receipt(overview(), START + timedelta(seconds=121))["verdict"] == "UNKNOWN"


def test_scanner_main_loop_fault_is_reported():
    value = overview()
    value["services"][0]["details"]["main_loop_exceptions_total"] = "1"
    assert proof.scanner_receipt(value, START)["verdict"] == "FAIL"


def test_db_transaction_rate_and_reset():
    before = {"at_utc": START.isoformat(), "stats_reset": "x", "xact_commit": 100, "xact_rollback": 2}
    after = {"at_utc": (START + timedelta(seconds=600)).isoformat(), "stats_reset": "x", "xact_commit": 7100, "xact_rollback": 202}
    result = proof.transaction_rate(before, after)
    assert result["transactions_per_second"] == 12
    assert "whole database" in result["scope"]
    after["stats_reset"] = "different"
    with pytest.raises(proof.Unknown):
        proof.transaction_rate(before, after)


class RedisFixture:
    def __init__(self, stamp):
        self.owners = {key: "[]" for key in proof.OWNERS} | {"_migration_complete": "1", "_last_applied_id": "1-0"}
        self.event = {"source_service": "schwab-1m-v2", "produced_at": stamp.isoformat(), "payload": {"watchlist": ["TEST"]}}
        self.calls = []

    def type(self, key):
        return "hash"

    def hlen(self, key):
        return len(self.owners)

    def hstrlen(self, key, field):
        return len(self.owners[field])

    def hgetall(self, key):
        self.calls.append("hgetall")
        return self.owners

    def info(self, section):
        return {"evicted_keys": 0} if section == "stats" else {"used_memory": 10000}

    def xrevrange(self, stream, **kwargs):
        self.calls.append(kwargs)
        return [("1-0", {"event": json.dumps(self.event)})]

    def close(self):
        self.closed = True


def redis_setup(monkeypatch, client):
    module = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *args, **kwargs: client))
    monkeypatch.setitem(sys.modules, "redis", module)
    return SimpleNamespace(redis_url="unused-fixture", redis_stream_prefix="mai_tai")


def test_redis_five_owners_marker_watchlist_bounded_per_event(monkeypatch):
    now = datetime.now(UTC)
    client = RedisFixture(now)
    client.owners["orb"] = '["TEST"]'
    settings = redis_setup(monkeypatch, client)
    result = proof.redis_capture(settings, now)
    assert result["active_union"] == ["TEST"]
    assert set(result["owners"]) == proof.OWNER_FIELDS
    assert client.calls[-1] == {"max": "+", "min": "-", "count": 1}
    assert client.closed


@pytest.mark.parametrize("change", ["missing_owner", "marker", "duplicate_symbol", "paper_cap", "oversized"])
def test_redis_bad_population_never_fake_pass(monkeypatch, change):
    now = datetime.now(UTC)
    client = RedisFixture(now)
    if change == "missing_owner":
        del client.owners["orb"]
    elif change == "marker":
        client.owners["_migration_complete"] = "0"
    elif change == "duplicate_symbol":
        client.owners["orb"] = '["TEST","TEST"]'
    elif change == "paper_cap":
        client.owners["momentum-paper"] = json.dumps([f"T{i}" for i in range(17)])
    else:
        client.owners["orb"] = "x" * 100001
    settings = redis_setup(monkeypatch, client)
    with pytest.raises(proof.Unknown):
        proof.redis_capture(settings, now)
    assert client.closed


def test_oversized_hash_is_never_materialized(monkeypatch):
    now = datetime.now(UTC)
    client = RedisFixture(now)
    client.owners["orb"] = "x" * 100001
    settings = redis_setup(monkeypatch, client)
    with pytest.raises(proof.Unknown):
        proof.redis_capture(settings, now)
    assert "hgetall" not in client.calls


def test_guard_receipt_matches_exact_invocation_and_clean_close(tmp_path, monkeypatch, good):
    before, after = good
    before["captured_at_utc"] = START.replace(hour=12).isoformat()
    for role in ("momentum-paper", "option-a-daily-guard"):
        old = before["services"][role]
        old.update({"start_utc": START.replace(hour=7, minute=40).isoformat(), "ExecMainStatus": 0})
        new = after["services"][role] = copy.deepcopy(old)
        new.update({"MainPID": 0, "ActiveState": "inactive", "SubState": "dead", "ExecMainStatus": 0,
                    "inactive_utc": START.replace(hour=13, minute=40, second=2 if role == "option-a-daily-guard" else 0).isoformat()})
    path = tmp_path / "2026-10-08" / "option-a-daily" / "run-fixture" / "option-a-guard.jsonl"
    path.parent.mkdir(parents=True)
    row = {"action": "stop_paper", "reason": "scheduled_session_close", "systemctl_rc": 0,
           "at_utc": START.replace(hour=13, minute=40, second=1).isoformat()}
    path.write_text(json.dumps(row) + "\n")
    original_stat = Path.stat
    def root_audit_stat(p, *args, **kwargs):
        result = original_stat(p, *args, **kwargs)
        return SimpleNamespace(st_uid=0, st_mode=0o100600, st_size=result.st_size) if p == path else result
    monkeypatch.setattr(Path, "stat", root_audit_stat)
    result = proof.paper_close_receipt(before, after, START + timedelta(minutes=11), root=tmp_path)
    assert result["reason"] == "scheduled_session_close"
    after["services"]["option-a-daily-guard"]["InvocationID"] = "different"
    with pytest.raises(proof.Unknown, match="paper_scheduled_close_shape_unproven"):
        proof.paper_close_receipt(before, after, START + timedelta(minutes=11), root=tmp_path)


def bar(stamp, symbol="TEST", created=None):
    return {"symbol": symbol, "bar_time": stamp.isoformat(), "created_at": (created or stamp + timedelta(minutes=1)).isoformat(),
            "updated_at": (created or stamp + timedelta(minutes=1)).isoformat()}


def test_after20_continuity_is_honest_not_zero_hole_claim():
    stop = datetime(2026, 10, 9, 0, 5, tzinfo=UTC)  # Oct 8 20:05 ET.
    result = proof.bar_continuity([], ["TEST"], stop, stop + timedelta(seconds=20), stop + timedelta(minutes=11))
    assert result["verdict"] == "N/A"
    assert result["scheduled_minutes_utc"] == []
    assert not result["per_symbol"][0]["newly_persisted_post_bar"]


def test_before20_exact_missing_minutes_and_fresh_post_bar():
    stop = START + timedelta(seconds=30)
    start = START + timedelta(minutes=2, seconds=20)
    rows = [bar(START - timedelta(minutes=1)), bar(START + timedelta(minutes=3), created=start + timedelta(minutes=2))]
    result = proof.bar_continuity(rows, ["TEST"], stop, start, start + timedelta(minutes=11))
    assert result["verdict"] == "MEASURED"
    assert result["per_symbol"][0]["scheduled_missing_minutes_utc"] == [
        (START + timedelta(minutes=i)).isoformat() for i in range(3)]


def test_unreadable_post_bar_not_measured_or_na():
    result = proof.bar_continuity([], ["TEST"], START, START + timedelta(seconds=20), START + timedelta(minutes=11))
    assert result["verdict"] == "UNKNOWN"


def test_bar_duplicate_never_hidden():
    rows = [bar(START), bar(START)]
    with pytest.raises(proof.Unknown, match="duplicate_foreign_or_nonminute_bar"):
        proof.bar_continuity(rows, ["TEST"], START, START + timedelta(seconds=20), START + timedelta(minutes=11))


def test_bar_grading_cost_measured_without_running_gapline_or_sql():
    stop = START + timedelta(seconds=30)
    started = START + timedelta(minutes=1, seconds=10)
    symbols = [f"T{i}" for i in range(128)]
    rows = [bar(START - timedelta(minutes=1), symbol) for symbol in symbols] + [
        bar(START + timedelta(minutes=2), symbol, created=START + timedelta(minutes=3)) for symbol in symbols]
    begin = time.perf_counter()
    result = proof.bar_continuity(rows, symbols, stop, started, START + timedelta(minutes=12))
    elapsed = time.perf_counter() - begin
    assert result["verdict"] == "MEASURED"
    assert len(result["per_symbol"]) == 128
    assert elapsed >= 0  # Cost is measured, not a flaky scheduler threshold.


def test_cli_baseline_output_exclusive_and_secret_free(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(proof, "collect_baseline", lambda: {"errors": {}})
    output = tmp_path / "before.json"
    assert proof.main(["baseline", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["assessment"]["scope"] == "baseline capture only; not a gate pass"
    with pytest.raises(FileExistsError):
        proof.main(["baseline", "--output", str(output)])
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"


def test_cli_collector_exception_returns_unknown_without_secret(tmp_path, monkeypatch):
    def fail():
        raise ValueError("secret token must not leak")
    monkeypatch.setattr(proof, "collect_baseline", fail)
    output = tmp_path / "unknown.json"
    assert proof.main(["baseline", "--output", str(output)]) == 2
    assert "secret token" not in output.read_text()


def test_cli_after_receives_runner_boundary_and_sha(tmp_path, monkeypatch):
    baseline = tmp_path / "before.json"
    baseline.write_text("{}")
    interval = tmp_path / "interval.json"
    interval.write_text(json.dumps({"stop_started_utc": START.isoformat()}))
    called = []
    monkeypatch.setattr(proof, "collect_after", lambda *args, **kwargs: called.append((args, kwargs)) or {"assessment": {"verdict": "UNKNOWN"}})
    retirement = tmp_path / 'retirement.json'
    retirement.write_text('{"fixture":"retirement"}')
    assert proof.main(["after", "--baseline", str(baseline), "--approved-sha", SHA,
        '--line-enabled', 'true', '--retirement', str(retirement),
        "--restart-window", str(interval), "--output", str(tmp_path / "after.json")]) == 0
    assert called == [(({}, SHA, {"stop_started_utc": START.isoformat()}),
                       dict(line_enabled=True, retirement={'fixture': 'retirement'}))]
