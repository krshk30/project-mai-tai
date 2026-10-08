"""CLI assertion controls on disposable mechanics copies, never the live job."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

CASES = [
    ('offhours_flow_waived', 'health_view.py', "detail.get('data_flow') == 'stalled_offhours_rest_dry'", 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_session_waived', 'health_view.py', "detail.get('market_session') == session", 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_loop_waived', 'health_view.py', "detail.get('loop_health') == 'healthy'", 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_exception_waived', 'health_view.py', 'exceptions == 0', 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_connection_waived', 'health_view.py', "true(detail.get('streamer_connected'))", 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_enabled_waived', 'health_view.py', "true(detail.get('enabled'))", 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_warmup_waived', 'health_view.py', 'warmed == count', 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_bar_age_waived', 'health_view.py', '0 <= age <= bound', 'True',
     'test_health_view.py::test_each_shape_guard_blocks_without_allowance'),
    ('offhours_freshness_waived', 'health_view.py', 'not 0 <= (now - observed).total_seconds() <= 120', 'False',
     'test_health_view.py::test_unreadable_stale_future_heartbeat_not_admitted'),
    ('offhours_other_service_changed', 'health_view.py', "row.get('service_name') == 'schwab-1m-v2'", 'True',
     'test_health_view.py::test_exact_shape_copy_admitted_native_gate_and_other_service_unchanged'),
    ('false_flip_activation_omitted', 'runner.py', 'for key in (GAP, FALSE_FLIP):', 'for key in (GAP,):',
     'test_runner.py::test_env_add_changes_only_gap_line_and_retains_rpg_false'),
    ('unrelated_catalog_reset', 'runner.py', 'current = json.loads(raw)', 'current = json.loads(canonical(approved))',
     'test_runner.py::test_catalog_only_two_additions_line_on_and_preserves_box_inventory_rulings'),
    ('wrong_migration_target', 'migration0023.py', "TARGET = '20261008_0023'", "TARGET = '20261005_0022'",
     'test_migration0023.py::test_actual_alembic_additive_nullable_json_and_exact_target'),
    ('native_rehearsal_skipped', 'runner.py', 'self.native_rehearsal()', 'pass',
     'test_sequence.py::test_literal_deploy_sequence_fresh_gate_before_each_restart_retirement_and_five_service_repin'),
    ('non_p1_candidate_admitted', 'make_release.py', 'if not set(rows) <= P1_PRS:', 'if False:',
     'test_stage.py::test_release_refuses_non_p1_candidate_even_with_claimed_review'),
    ('h_landing_not_required', 'make_release.py', "REQUIRED_PRS = {'1126', '1127', '1130', '1134', '1135'}",
     "REQUIRED_PRS = {'1126', '1127', '1130', '1134'}",
     'test_stage.py::test_h_requires_its_own_reviewed_landing'),
    ('first_write_before_close', 'runner.py', 'local.hour >= 16', 'local.hour >= 0',
     'test_runner.py::test_first_write_date_and_after_close_only'),
    ('retry_measured_blocker', 'runner.py', 'if rc != 2 or attempt == 3:', 'if rc != 1 or attempt == 3:',
     'test_runner.py::test_rc_two_only_bounded_read_retries_preserve_every_attempt'),
    ('rpg_turned_on', 'runner.py', "need(bindings[HANDOFF][0][2] == 'false',", "need(True,",
     'test_runner.py::test_env_cannot_turn_handoff_on'),
    ('future_data_source_admitted', 'make_release.py', "if row['source_prs'] != [pr]:",
     'if False:', 'test_stage.py::test_release_rejects_future_wb_data_source_provenance'),
    ('changed_union_accepted', 'make_release.py',
     "if git('rev-parse', row['landing'] + '^{tree}') != git('rev-parse', row['reviewed_head'] + '^{tree}'):",
     'if False:', 'test_stage.py::test_release_rejects_changed_union_tree_and_unreviewed_final_head'),
    ('stage_before_close', 'stage.py', "now.hour >= 16, 'NO STAGING", "now.hour >= 0, 'NO STAGING",
     'test_stage.py::test_staging_clock_refuses_before_any_remote_directory_or_unit_write'),
    ('runtime_dependency_omitted', 'repin_preopen.py',
     'need(REQUIRED_ARTIFACTS <= set(old_runtime.get("artifacts", {})),', 'need(True,',
     'test_repin_preopen.py::test_omitting_runtime_dependency_never_rehashes_to_green'),
    ('runtime_evidence_omitted', 'repin_preopen.py',
     'need(REQUIRED_INPUTS <= set(old_runtime.get("evidence_inputs", {})),', 'need(True,',
     'test_repin_preopen.py::test_omitting_runtime_evidence_dependency_never_rehashes_to_green'),
    ('untouched_ack_changed', 'repin_preopen.py', "need(all(observations['upgrade_state'].get(key) == value for key, value in ack['state'].items()),",
     'need(True,', 'test_repin_preopen.py::test_upgrade_ack_never_adopts_another_restart'),
    ('retired_orb_adopted', 'repin_preopen.py', "and 'orb' not in before['services']", 'and True',
     'test_repin_preopen.py::test_old_collector_or_retired_orb_population_not_adopted'),
    ('other_owner_changed', 'retire_orb.py',
     "need(all(before[name] == after[name] for name in FIELDS - {'orb', '_last_applied_id'}),",
     'need(True,', 'test_retire_orb.py::test_retirement_proof_each_deciding_field[other_owner]'),
    ('orb_not_empty', 'retire_orb.py', "need(json.loads(after['orb']) == [],", 'need(True,',
     'test_retire_orb.py::test_retirement_proof_each_deciding_field[live_orb]'),
    ('unrelated_cursor_accepted', 'retire_orb.py',
     "redis_id(after['_last_applied_id']) >= redis_id(receipt.get('request_id'))", 'True',
     'test_retire_orb.py::test_orb_already_empty_unrelated_older_cursor_is_not_our_application'),
    ('duplicate_empty_replace', 'retire_orb.py', "receipt.get('normal_replace_count') == 1", 'True',
     'test_retire_orb.py::test_retirement_proof_each_deciding_field[duplicate]'),
    ('live_orb_not_retired', 'proof_readonly.py', 'elif role == "orb":', 'elif False:',
     'test_proof_readonly.py::test_complete_evidence_passes_without_granting_admission'),
    ('restart_gate_skipped', 'runner.py', "need(self.gate('before-deploy-' + target) == 0,", 'need(True,',
     'test_sequence.py::test_measured_work_stops_before_target_never_skips_gate_or_recovers'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    rows = []
    for name, filename, old, new, target in CASES:
        with tempfile.TemporaryDirectory(prefix='oct8-mechanics-mutation-') as temporary:
            job = Path(temporary) / 'job'
            shutil.copytree(source, job, ignore=shutil.ignore_patterns('__pycache__'))
            path = job / filename
            raw = path.read_text()
            if raw.count(old) != 1:
                raise RuntimeError('mutation anchor changed: ' + name)
            # Generated mutation, isolated and discarded after this one run.
            path.write_text(raw.replace(old, new, 1))
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', target, '--tb=short'],
                cwd=job, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, timeout=60, check=False)
            rows.append(dict(mutation=name, test=target, rc=result.returncode,
                verdict='RED' if result.returncode == 1 and b'FAILED' in result.stdout else 'SURVIVED_OR_INVALID',
                stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
                failed_lines=[line for line in result.stdout.decode().splitlines() if line.startswith('FAILED ')]))
    receipt = dict(scope='isolated runner/gate mechanics; not trading replay', mutations=rows,
                   red=sum(row['verdict'] == 'RED' for row in rows), population=len(rows))
    with args.output.open('x') as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write('\n')
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt['red'] == len(rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
