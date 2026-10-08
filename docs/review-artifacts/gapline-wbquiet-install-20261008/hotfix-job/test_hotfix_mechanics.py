"""Isolated receipts and OS fixtures, not fabricated trading outcomes."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location('hotfix_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load('runner')
proof = load('proof_readonly')
inventory = load('catalog_inventory')
book = load('bookkeeping')
restore = load('removed_wait_readonly')
UTC = timezone.utc
START = datetime(2026, 10, 8, 20, 40, tzinfo=UTC)


def test_literal_hotfix_has_no_migration_control_or_environment_write():
    tree = ast.parse((HERE / 'runner.py').read_text())
    run_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Run')
    install = next(node for node in run_class.body if isinstance(node, ast.FunctionDef) and node.name == 'install')
    assert runner.UNITS == ('oms', 'schwab-1m-v2', 'strategy')
    assert proof.RESTARTED == set(runner.UNITS)
    loops = [node for node in ast.walk(install) if isinstance(node, ast.For) and ast.unparse(node.target) == 'target']
    assert len(loops) == 1 and ast.literal_eval(loops[0].iter) == ('oms', 'schwab-1m-v2')
    writes = [node for node in ast.walk(install) if isinstance(node, ast.Call) and ast.unparse(node.func) == 'self.backup_write']
    assert len(writes) == 1 and ast.unparse(writes[0].args[0]) == 'CATALOG'
    text = ast.unparse(install)
    assert 'MAI_TAI_RUN_MIGRATIONS=0' in text
    assert 'upgrade' not in text and 'migration0023.py' not in text
    assert 'self.gate' in text and 'time.sleep(600)' in text
    assert '/run/lock/project-mai-tai-deploy.lock' in (HERE / 'runner.py').read_text()


def test_distinct_job_unit_path_not_original_sealed_package():
    stage = (HERE / 'stage.py').read_text()
    service = (HERE / 'project-mai-tai-falseflip-pg-20261008.service').read_text()
    assert '/2026-10-08/falseflip-pg-hotfix/job' in stage and '/2026-10-08/falseflip-pg-hotfix/job' in service
    assert '/2026-10-08/gapline-wbquiet/job' not in stage + service
    assert 'project-mai-tai-falseflip-pg-20261008' in stage
    assert not (HERE / 'migration0023.py').exists()
    assert not (HERE / 'retire_orb.py').exists()


@pytest.mark.parametrize('outcomes,sleeps', [([2, 0], [60]), ([2, 2, 2], [60, 60]), ([1], []), ([124], [])])
def test_unknown_only_three_bounded_retries(outcomes, sleeps):
    seen, waited = [], []
    assert runner.retry_read(lambda n: outcomes[n-1], sleep=waited.append,
                             record=lambda n, rc: seen.append((n, rc))) == outcomes[-1]
    assert waited == sleeps and len(seen) == len(outcomes)


def catalogs():
    approved = {'flags': [dict(name='current_' + str(i), expected=True, owning_service='oms') for i in range(132)]}
    box = copy.deepcopy(approved)
    box['flags'] += [dict(name=n, expected=False, owning_service='orb') for n in sorted(inventory.RETIRED)]
    return box, approved


def test_current_inventory_preserves_all_boolean_values_and_numeric_policy():
    box, approved = catalogs()
    approved['flags'][0]['also_check_services'] = ['schwab-1m-v2']
    raw = json.dumps(approved).encode()
    candidate, receipt = inventory.candidate(json.dumps(box).encode(), raw)
    assert candidate == raw
    assert receipt['before_boolean_rows'] == 139 and receipt['after_boolean_rows'] == 132
    assert set(receipt['removed_settings_fields']) == inventory.RETIRED
    assert receipt['expected_value_changes'] == []
    assert 'retry-budget=0' in receipt['numeric_policy']
    assert 'none' in receipt['legacy_orb_service_action']


@pytest.mark.parametrize('defect', ['expected', 'extra', 'missing', 'partial_retirement', 'duplicate', 'unreadable'])
def test_inventory_cannot_loosen_values_or_drop_unreviewed_rows(defect):
    box, approved = catalogs()
    if defect == 'expected':
        approved['flags'][0]['expected'] = False
    elif defect == 'extra':
        box['flags'].append(dict(name='unreviewed', expected=True))
    elif defect == 'missing':
        box['flags'].pop(0)
    elif defect == 'partial_retirement':
        box['flags'].pop()
    elif defect == 'duplicate':
        approved['flags'].append(approved['flags'][0])
    else:
        approved['flags'][0]['expected'] = 1
    with pytest.raises(ValueError):
        inventory.candidate(json.dumps(box).encode(), json.dumps(approved).encode())


def control_identity():
    return dict(MainPID=2075090, NRestarts=0, InvocationID='authorized-control-invocation',
                ExecMainStartTimestampMonotonic=123456, ActiveState='active', SubState='running')


def test_untimestamped_access_and_errors_keep_null_time_and_exact_unchanged_process():
    raw = 'INFO:     127.0.0.1:123 - "GET /api/overview HTTP/1.1" 200 OK\nERROR: actual failure\nTraceback (most recent call last):\n'
    records = proof.control_window_records([dict(path='control.log', text=raw)], control_identity(),
                                           control_identity(), START, START + timedelta(minutes=10))
    assert len(records) == 3 and all(row['at_utc'] is None for row in records)
    assert all(row['time_basis'] == 'bounded_cursor_window' and row['process_pid'] == 2075090 for row in records)
    assert len(proof.summarize_logs(records)['errors']) == 2


@pytest.mark.parametrize('field,value', [('MainPID', 99), ('InvocationID', 'other'), ('ExecMainStartTimestampMonotonic', 999)])
def test_untimestamped_window_cannot_adopt_another_identity(field, value):
    after = control_identity()
    after[field] = value
    with pytest.raises(proof.Unknown):
        proof.control_window_records([dict(path='control.log', text='INFO: access\n')], control_identity(),
                                     after, START, START + timedelta(minutes=10))


def test_foreign_server_marker_refuses_window_instead_of_filtering_it():
    with pytest.raises(proof.Unknown, match='foreign_process'):
        proof.control_window_records([dict(path='control.log', text='INFO: Started server process [1464286]\n')],
                                     control_identity(), control_identity(), START, START + timedelta(minutes=10))


def test_four_closed_fill_errors_remain_errors_not_tracebacks_or_buys():
    lines = '\n'.join('2026-10-08 20:25:11,357 ERROR V2-CONFIRMATION-EXIT-UNANSWERABLE line_unproven id=' + fill
                      for fill in ('871c4868', '27660cee', '31a558a7', '44a2770a'))
    records = proof.log_records([dict(path='/var/log/project-mai-tai/schwab-1m-v2.log', text=lines)],
                               datetime(2026, 10, 8, 20, 25, tzinfo=UTC), START)
    summary = proof.summarize_logs(records)
    assert len(summary['errors']) == 4
    assert all('Traceback' not in row['line'] for row in records)


@pytest.mark.parametrize('count', [0, 1, 8])
def test_removed_wait_restore_reports_actual_count_never_archives_or_forces_zero(count):
    statements = []
    class Connection:
        def exec_driver_sql(self, statement):
            statements.append(statement)
        def execute(self, query):
            statements.append(str(query))
            return SimpleNamespace(scalars=lambda: iter(['20261008_0023']))
    class Store:
        def __init__(self, factory):
            assert factory == 'isolated-reader'
        def restore(self):
            return {str(i): SimpleNamespace(token='t' + str(i)) for i in range(count)}
    result = restore.inspect_store(Connection(), Store, 'isolated-reader')
    assert result['restored_count'] == count and result['writes_performed'] is False
    assert result['direct_boot_count'].startswith('UNMEASURED')
    assert statements[0] == 'SET TRANSACTION READ ONLY'
    assert '5000ms' in statements[1] and '500ms' in statements[2]
    assert not any(word in ' '.join(statements).lower() for word in ('update', 'delete', 'insert', 'upgrade'))


def test_schema_other_than_installed_0023_is_reported_not_upgraded():
    statements = []
    class Connection:
        def exec_driver_sql(self, statement):
            statements.append(statement)
        def execute(self, query):
            return SimpleNamespace(scalars=lambda: iter(['20261005_0022']))
    with pytest.raises(ValueError, match='no upgrade authorized'):
        restore.inspect_store(Connection(), lambda factory: pytest.fail('must not restore on another schema'), None)
    assert not any('upgrade' in statement for statement in statements)


def test_combined_record_uses_actual_prior_control_restart_without_restarting_it():
    prior = dict(snapshot=dict(captured_at_utc=START.isoformat(), services={n: {} for n in ('oms', 'schwab-1m-v2', 'strategy', 'control', 'orb')}),
                 service_identities={'control': control_identity()}, hashes={'proof': 'immutable'}, original_verdict='ABORT; never COMPLETE')
    before = dict(services={'control': control_identity(), **{n: {'MainPID': 10} for n in ('oms', 'schwab-1m-v2', 'strategy')}})
    after = {n: dict(MainPID=100+i, NRestarts=0, ActiveState='active', SubState='running')
             for i, n in enumerate(('oms', 'schwab-1m-v2', 'strategy'))}
    events = [dict(deploy_finished=n) for n in ('oms', 'schwab-1m-v2')]
    record, journal = book.combined_record(prior, before, after, events)
    assert record['service_actions']['control'] == 'restarted'
    assert record['service_actions']['orb'] == 'deliberately_untouched'
    assert journal['hotfix_restarted_group'] == ['oms', 'schwab-1m-v2', 'strategy']
    assert journal['prior_install_verdict'] == 'ABORT; never COMPLETE'
    before['services']['control']['MainPID'] = 1234
    with pytest.raises(ValueError, match='no arbitrary adoption'):
        book.combined_record(prior, before, after, events)
