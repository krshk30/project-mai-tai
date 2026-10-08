"""Committed-blob packaging boundaries; review/CI inputs here are isolated fixtures."""
import copy
import json
from pathlib import Path

import pytest

import make_release
import runner
import stage


@pytest.fixture
def prepared(monkeypatch):
    box, approved, plan, reviewed = runner.BASELINE_SHA, 'a'*40, 'b'*40, 'd'*40
    row = dict(base=box, reviewed_head=reviewed, landing=approved, pin_verdict='PASS', validate_verdict='PASS',
               source_prs=['1137'], pin_receipt='isolated pin receipt', validate_receipts=['isolated push', 'isolated PR'])
    review = dict(approved_sha=approved, line_enabled=True, candidate_prs=['1137'], landed_prs={'1137': row},
                  postgres_test_receipt='isolated real-PG receipt contract, not live evidence', prior_install={'bound': 'fixture'},
                  box_baseline=dict(sha=box, captured_at_utc='2026-10-08T21:00:00+00:00', authorization_source='isolated fixture',
                      services={n: dict(MainPID=100+i, NRestarts=0, ActiveState='active', SubState='running',
                          InvocationID='old-'+n, ExecMainStartTimestampMonotonic=100+i)
                          for i, n in enumerate(sorted(make_release.BASELINE_ROLES))}))
    names = ('runner.py', 'run.sh', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py', 'flag_audit.py',
             'catalog_inventory.py', 'bookkeeping.py', 'removed_wait_readonly.py', 'health_view.py',
             'project-mai-tai-falseflip-pg-r2-20261008.service', 'project-mai-tai-falseflip-pg-r2-20261008.timer')
    calls = []
    def git(*args):
        calls.append(args)
        if args[0] == 'rev-parse':
            value = args[1]
            return ((value.split('^')[0] if value.endswith('^{commit}') else 'e'*40) + '\n').encode()
        if args[0] == 'merge-base':
            return b''
        if args[:2] == ('diff', '--name-only'):
            return b'src/project_mai_tai/falseflip1_runtime.py\ntests/integration/test_falseflip1_postgres_epochs.py\n'
        if args[:2] == ('diff', '--binary'):
            return b'isolated same patch'
        if args[0] == 'ls-tree':
            return ('\n'.join(make_release.PREFIX+n for n in names) + '\n').encode()
        if args[0] == 'show':
            return args[1].encode()
        raise AssertionError(args)
    monkeypatch.setattr(make_release, 'git', git)
    monkeypatch.setattr(make_release, 'verify_pin', lambda pr, row, ledger: {'verdict': 'isolated PASS'})
    return plan, approved, box, review, calls


def test_manifest_only_pg_hotfix_no_migration_archive_and_distinct_plan_payload(prepared):
    plan, approved, box, review, calls = prepared
    raw, approval_raw, artifacts = make_release.release(plan, approved, box, review, Path('/isolated/ledger'))
    manifest, approval = json.loads(raw), json.loads(approval_raw)
    assert manifest['scope'] == runner.SCOPE
    assert manifest['landed_reviews'].keys() == {'1137'}
    assert manifest['prior_install'] == review['prior_install']
    assert not {'migration0023.py', 'approved-migrations.tar', 'retire_orb.py'} & artifacts.keys()
    assert not any(args[0] == 'archive' for args in calls)
    assert approval['manifest_sha256'] == make_release.digest(raw)
    assert '/hotfix-job/' in make_release.PREFIX
    assert manifest['box_sha'] == runner.BASELINE_SHA
    assert stage.REMOTE.count('falseflip-pg-hotfix-r2/job') == 1


@pytest.mark.parametrize('defect', ['wrong_base', 'other_pr', 'no_pg', 'no_prior', 'no_pin', 'no_ci', 'foreign_source'])
def test_missing_review_or_scope_expansion_cannot_generate_release(prepared, defect, monkeypatch):
    plan, approved, box, initial, _ = prepared
    review = copy.deepcopy(initial)
    row = review['landed_prs']['1137']
    if defect == 'wrong_base':
        row['base'] = 'c'*40
    elif defect == 'other_pr':
        review['candidate_prs'].append('1136')
        review['landed_prs']['1136'] = copy.deepcopy(row)
    elif defect == 'no_pg':
        del review['postgres_test_receipt']
    elif defect == 'no_prior':
        del review['prior_install']
    elif defect == 'no_pin':
        row['pin_verdict'] = 'FAIL'
    elif defect == 'no_ci':
        row['validate_verdict'] = 'FAIL'
    else:
        row['source_prs'] = ['1137', '1136']
    with pytest.raises(ValueError):
        make_release.release(plan, approved, box, review, Path('/isolated/ledger'))
