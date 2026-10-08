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
    ('first_write_before_close', 'runner.py', 'local.hour >= 16', 'local.hour >= 0',
     'test_runner.py::test_first_write_date_and_after_close_only'),
    ('retry_measured_blocker', 'runner.py', 'if rc != 2 or attempt == 3:', 'if rc != 1 or attempt == 3:',
     'test_runner.py::test_rc_two_only_bounded_read_retries_preserve_every_attempt'),
    ('rpg_turned_on', 'runner.py', "need(bindings[HANDOFF][0][2] == 'false',", "need(True,",
     'test_runner.py::test_env_cannot_turn_handoff_on'),
    ('wb_union_not_required', 'make_release.py', "if not {'1124', '1125'} <= set(rows['1129']['source_prs']):",
     'if False:', 'test_stage.py::test_release_rejects_union_without_wb_source_provenance'),
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
    ('ack_remains_current', 'repin_preopen.py', "ack['active_for_current_install'] = False",
     "ack['active_for_current_install'] = True", 'test_repin_preopen.py::test_new_orb_schwab_control_identities_and_ack_history_not_adopted'),
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
