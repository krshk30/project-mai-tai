"""Assertion controls in private copies. Never mutate committed or box sources."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).parent
CONTROLS = [
    ('repin_schema_read_removed', 'repin_preopen.py',
     "need(observations.get('alembic_revision') == INSTALLED_SCHEMA, 'installed schema unreadable or not 0023')",
     'pass', 'test_repin_preopen.py::test_schema_repin_never_invents_migration_or_uses_stale_head[revision]'),
    ('repin_migration_hash_removed', 'repin_preopen.py',
     "and transition.get('source_runner_log_sha256') == MIGRATION_SOURCE", 'and True',
     'test_repin_preopen.py::test_schema_repin_never_invents_migration_or_uses_stale_head[source]'),
    ('repin_failed_migration_accepted', 'repin_preopen.py',
     "and transition['migration_receipt'].get('rc') == 0", 'and True',
     'test_repin_preopen.py::test_schema_repin_never_invents_migration_or_uses_stale_head[failed_migration]'),
    ('control_restart_added', 'runner.py', "for target in ('oms', 'schwab-1m-v2'):",
     "for target in ('oms', 'schwab-1m-v2', 'control'):",
     'test_hotfix_mechanics.py::test_literal_hotfix_has_no_migration_control_or_environment_write'),
    ('migration_enabled', 'runner.py', "'MAI_TAI_RUN_MIGRATIONS=0'", "'MAI_TAI_RUN_MIGRATIONS=1'",
     'test_hotfix_mechanics.py::test_literal_hotfix_has_no_migration_control_or_environment_write'),
    ('env_write_added', 'runner.py', 'self.backup_write(CATALOG, catalog)',
     'self.backup_write(ENV, ENV.read_bytes())\n        self.backup_write(CATALOG, catalog)',
     'test_hotfix_mechanics.py::test_literal_hotfix_has_no_migration_control_or_environment_write'),
    ('expected_value_changed', 'catalog_inventory.py', 'if changed:', 'if False:',
     'test_hotfix_mechanics.py::test_inventory_cannot_loosen_values_or_drop_unreviewed_rows[expected]'),
    ('force_restore_zero', 'removed_wait_readonly.py', 'restored_count=len(restored)', 'restored_count=0',
     'test_hotfix_mechanics.py::test_removed_wait_restore_reports_actual_count_never_archives_or_forces_zero[8]'),
    ('nonzero_restore_hidden', 'runner.py', "need(after_restore['restored_count'] == 0,", 'need(True,',
     'test_hotfix_sequence.py::test_recreated_removed_wait_rows_report_nonzero_and_prevent_complete_without_archiving'),
    ('schema_guard_removed', 'removed_wait_readonly.py', "if revisions != ['20261008_0023']:", 'if False:',
     'test_hotfix_mechanics.py::test_schema_other_than_installed_0023_is_reported_not_upgraded'),
    ('readonly_removed', 'removed_wait_readonly.py', "connection.exec_driver_sql('SET TRANSACTION READ ONLY')", 'pass',
     'test_hotfix_mechanics.py::test_removed_wait_restore_reports_actual_count_never_archives_or_forces_zero[0]'),
    ('control_adopted', 'bookkeeping.py', "if any(str(old_control[key]) != str(control[key]) for key in ('MainPID', 'NRestarts', 'ActiveState', 'SubState')):",
     'if False:', 'test_hotfix_mechanics.py::test_combined_record_uses_actual_prior_control_restart_without_restarting_it'),
    ('control_foreign_marker_hidden', 'proof_readonly.py', 'if marker and int(marker[1]) != after["MainPID"]:', 'if False:',
     'test_hotfix_mechanics.py::test_foreign_server_marker_refuses_window_instead_of_filtering_it'),
    ('old_checker_selected', 'flag_audit.py', '/home/trader/project-mai-tai/ops/health/expected_flags_check.py',
     '/home/trader/restart_evidence/expected_flags_check.py',
     'test_flag_audit.py::test_current_selector_is_reviewed_repo_source_not_old_box_registry'),
    ('numeric_policy_replaced', 'flag_audit.py', '/home/trader/restart_evidence/expected_numeric.json',
     '/home/trader/project-mai-tai/ops/health/expected_numeric.json',
     'test_flag_audit.py::test_current_selector_is_reviewed_repo_source_not_old_box_registry'),
    ('error_proof_waived', 'proof_readonly.py', 'if report["traceback_or_error_count"]:', 'if False:',
     'test_proof_readonly.py::test_old_process_error_excluded_new_traceback_counted'),
]


@pytest.mark.parametrize('name,source,old,new,test', CONTROLS, ids=[row[0] for row in CONTROLS])
def test_hotfix_removal_control_is_red(tmp_path, name, source, old, new, test):
    target = tmp_path / 'isolated-hotfix'
    shutil.copytree(HERE, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    path = target / source
    text = path.read_text()
    assert text.count(old) == 1, name
    # These writes are generated assertion mutations, not manual repository edits.
    path.write_text(text.replace(old, new))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(target) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', test, '--import-mode=importlib'],
                            cwd=target, env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and '1 failed' in result.stdout, name + '\n' + result.stdout + result.stderr
