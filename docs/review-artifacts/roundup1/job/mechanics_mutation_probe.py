"""Fresh-process in-memory mutations. Never edits files, stages or calls production."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent
PROBES = (
    ("retry_explicit_raw_values", "retry_zero_readonly", 'need(found == [(key, expected)],', 'need(True,',
     "import test_retry_zero_mechanics as t; import release_policy as p; t.test_raw_missing_default_nonzero_negative_malformed_each_key_blocks(p.RETRY_MAX, None)"),
    ("retry_env_zero_write", "release_policy", 'RETRY_MAX: "0"}', 'RETRY_MAX: "1"}',
     "import test_retry_zero_mechanics as t; t.test_env_zero_update_preserves_enabled_and_protected_bytes()"),
    ("retry_receipt_value_binding", "retry_zero_readonly", 'row["values"] == EXPECTED and', 'True and',
     "import test_retry_zero_mechanics as t; import release_policy as p; t.test_receipt_stale_incomplete_changed_values_never_pass(lambda r: r['rows'][0]['values'].update({p.RETRY_MAX:'1'}))"),
    ("retry_catalog_hash", "retry_zero_readonly", 'need(digest(raw) == NUMERIC_SHA,', 'need(True,',
     "import test_retry_zero_mechanics as t; t.test_catalog_semantically_equal_unreviewed_bytes_block()"),
    ("armed_field_presence", "armed_readonly", 'need("cw_armed_segments" in payload and type(payload["cw_armed_segments"]) is list,',
     'payload.setdefault("cw_armed_segments", []); need(True,',
     "import test_armed_readonly as t; t.test_missing_malformed_nonzero_wrong_identity_stale_never_zero(lambda e: e['payload'].pop('cw_armed_segments'))"),
    ("paper_clean_shape", "closeout", 'and after["ExecMainCode"] == 1 and after["ExecMainStatus"] == 0,',
     'and after["ExecMainCode"] == 1,',
     "import test_paper_coverage as t; t.test_any_unexpected_paper_state_or_stop_time_blocks('ExecMainStatus', 1)"),
    ("row47_pid0", "release_policy", 'after["MainPID"] == 0', 'after["MainPID"] >= 0',
     "import test_attended_release as t; t.test_row47_unit_state_negatives('MainPID', 1)"),
    ("row47_sigterm", "release_policy", 'need("SIGTERM received" in own,', 'need(True,',
     "import test_attended_release as t; t.test_row47_each_signature_required(0)"),
    ("untouched_identity", "release_policy", 'need(state == old,', 'need(True,',
     "import test_attended_release as t; t.test_each_untouched_or_early_identity_drift_blocks('strategy')"),
    ("raw_gate_wire_quantity", "raw_gate_admission", 'Decimal(match[1]) == 1000', 'Decimal(match[1]) >= 1',
     "import test_raw_gate_admission as t; t.test_raw_other_quantity_symbol_extra_position_or_block_always_stops('v2', t.V2, lambda s: s.replace('IPDN=1000', 'IPDN=132'))"),
    ("fresh_direct_proof", "raw_gate_admission", 'residual = ipdn_residual(snapshot, snapshot["allowance_findings"], now)',
     'residual = snapshot["dated_operator_residual"]',
     "import test_raw_gate_admission as t; t.test_pre_or_post_live_proof132_66_owned_sell_stale_unknown_blocks(1, lambda s: s.__setitem__('proof_completed_at_utc', (t.NOW - t.timedelta(seconds=121)).isoformat()))"),
    ("timeout_not_green", "daily", 'if rc not in (0, 1, 2):\n        return 2', 'if rc not in (0, 1, 2):\n        return 0',
     "import test_daily_release as t; t.test_checker_crash_timeout_are_unknown_not_pass(124)"),
    ("fresh_report", "daily", 'need(before.replace(microsecond=0) <= stamp <= after and before.timestamp() <= mtime <= after.timestamp(),',
     'need(True,',
     "import test_daily_release as t; t.test_missing_stale_wrong_date_duplicate_or_disagreeing_report_blocks(t.report(), t.NOW.timestamp()-1)"),
    ("paper_today_floor", "daily", 'floor <= started <= now and guarded <= started', 'started <= now and guarded <= started',
     "import test_daily_release as t; t.test_paper_floor_independent_when_guard_older()"),
)


def run(module, source, call):
    script = "import sys; sys.path.insert(0," + repr(str(ROOT)) + ")\nimport " + module + " as target\n"
    script += "exec(compile(" + repr(source) + ", target.__file__, 'exec'), target.__dict__)\n" + call + "\n"
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)


def main():
    results = []
    for name, module, old, new, call in PROBES:
        source = (ROOT / (module + ".py")).read_text()
        if source.count(old) != 1:
            raise RuntimeError("mutation anchor not unique: " + name)
        control = run(module, source, call)
        if control.returncode != 0:
            raise RuntimeError("negative control not green: " + name + " " + control.stderr[-1000:])
        mutant = run(module, source.replace(old, new), call)
        assertion_red = mutant.returncode != 0 and any(token in mutant.stderr for token in ("Failed: DID NOT RAISE", "AssertionError"))
        if not assertion_red:
            raise RuntimeError("mutation not assertion RED: " + name + " " + mutant.stderr[-1000:])
        results.append(dict(name=name, control="PASS", mutant="ASSERTION_RED", source_written=False))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
