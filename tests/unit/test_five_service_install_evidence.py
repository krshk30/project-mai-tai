"""Pure evidence guards and private log files; never invokes the installer.

No permissions are stubbed and no service/process/provider/DB is called. These
controls qualify guard logic, not a production installation or a 600s watch.
"""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(os.environ.get('FSI_REVIEWED_ROOT', Path(__file__).resolve().parents[2]))
SOURCE = ROOT / 'ops/install'
LIBRARY = (SOURCE / 'restart_evidence_install_lib.sh').read_text().split("<<'PY'\n", 1)[1].split('\nmode, *args = sys.argv[1:]', 1)[0]
LIB = {}
exec(compile(LIBRARY, str(SOURCE / 'restart_evidence_install_lib.sh'), 'exec'), LIB)
SPEC = importlib.util.spec_from_file_location('reviewed_install_proof', SOURCE / 'five_service_install_proof.py')
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)
TARGET = 'a' * 40
SERVICES = 'oms,schwab-1m-v2,strategy,control,orb-schwab'


def plan():
    return dict(version=1, target_sha=TARGET, expected_current_sha='b' * 40,
        expected_env_sha256='c' * 64, install_date_et='2026-10-30',
        expected_alembic_head='reviewed_migration',
        evidence_directory='/home/trader/after-hours/2026-10-30/reviewed-one-install',
        updates={'MOMLIVE_SEC_APPROVED_HEAD': TARGET, 'MAI_TAI_WBEXIT3_ENABLED': 'true'},
        remove=['MAI_TAI_RETIRED_LINE_ENABLED'], expect_flags=['oms:WBEXIT3_ENABLED=true'],
        clock_override_reason=None, verification_hook='/root/reviewed-install/proof.py',
        verification_hook_sha256='d' * 64)


def validate(value, services=SERVICES):
    return LIB['plan_payload'](json.dumps(value), TARGET, services)


def test_plan_requires_literal_reviewed_source_env_hook_and_service_flags():
    assert validate(plan()) == plan()
    for key, value in (
        ('target_sha', 'e' * 40), ('expected_current_sha', 'main'),
        ('expected_env_sha256', ''), ('verification_hook_sha256', 'unknown'),
        ('version', True), ('clock_override_reason', 'old\nwaiver'),
        ('evidence_directory', '/home/trader/after-hours/../old'),
        ('expect_flags', ['market-data:WBEXIT3_ENABLED=true']),
        ('updates', {'MOMLIVE_SEC_APPROVED_HEAD': TARGET, 'UNRELATED_SECRET': 'changed'}),
        ('remove', ['MOMLIVE_SEC_APPROVED_HEAD']),
    ):
        bad = plan()
        bad[key] = value
        with pytest.raises((ValueError, TypeError)):
            validate(bad)


@pytest.mark.parametrize('services', ['oms', 'control', SERVICES + ',oms', SERVICES + ',market-data', 'schwab-1m-v2,,oms'])
def test_plan_cannot_adopt_untouched_or_duplicate_restart(services):
    with pytest.raises(ValueError):
        validate(plan(), services)


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e9999}'])
def test_strict_inputs_refuse_duplicate_and_nonfinite_at_every_depth(raw):
    with pytest.raises(ValueError):
        LIB['load'](raw)
    with pytest.raises(ValueError):
        PROOF.load(raw)


def test_environment_preserves_secret_comments_unrelated_bytes_and_exact_removal():
    before = b'# fixed\r\nSECRET=literal-$HOME\nexport MOMLIVE_SEC_APPROVED_HEAD=old\nMAI_TAI_RETIRED_LINE_ENABLED=true\nOTHER=no-newline'
    candidate = LIB['environment_candidate'](before,
        {'MOMLIVE_SEC_APPROVED_HEAD': TARGET}, ['MAI_TAI_RETIRED_LINE_ENABLED'])
    assert candidate == b'# fixed\r\nSECRET=literal-$HOME\nMOMLIVE_SEC_APPROVED_HEAD=' + TARGET.encode() + b'\nOTHER=no-newline'
    assert before.endswith(b'OTHER=no-newline')


@pytest.mark.parametrize('before,remove', [
    (b'MOMLIVE_SEC_APPROVED_HEAD=x\nMOMLIVE_SEC_APPROVED_HEAD=y\n', []),
    (b'momlive_sec_approved_head=x\n', []),
    (b'OTHER=x\n', ['MAI_TAI_MISSING_ENABLED']),
])
def test_environment_alias_duplicate_or_missing_removal_refuses(before, remove):
    with pytest.raises(ValueError):
        LIB['environment_candidate'](before, {'MOMLIVE_SEC_APPROVED_HEAD': TARGET}, remove)


def journal():
    population = ['control', 'market-capture', 'market-data', 'oms', 'reconciler',
                  'schwab-1m-v2', 'strategy', 'tv-alerts', 'orb-schwab']
    before = dict(schema_version=3, captured_at_utc='2026-10-30T20:05:00+00:00',
                  services={name: {} for name in population})
    events = []
    for operation, names in [('stop', ['orb-schwab', 'schwab-1m-v2', 'strategy', 'oms', 'control']),
                             ('start', ['oms', 'schwab-1m-v2', 'strategy', 'control', 'orb-schwab'])]:
        for name in names:
            for op, result in [(operation + '-request', 'entered'), (operation, 'success')]:
                events.append(dict(at_utc='2026-10-30T20:05:01+00:00', operation=op, service=name, result=result))
    return before, events


def test_official_record_uses_complete_actual_snapshot_and_journal_population():
    before, events = journal()
    value = LIB['record_payload'](before, events, SERVICES.split(','), '/private/actual-actions.jsonl')
    assert len(value['service_actions']) == 9
    assert {key for key, action in value['service_actions'].items() if action == 'restarted'} == set(SERVICES.split(','))
    assert value['service_actions']['market-data'] == 'deliberately_untouched'
    assert value['snapshot_captured_at_utc'] == before['captured_at_utc']


@pytest.mark.parametrize('defect', ['missing-snapshot-service', 'duplicate', 'no-request', 'foreign', 'failure', 'before-snapshot', 'no-zone', 'consumer-before-oms'])
def test_report_cannot_fabricate_or_reorder_installation_history(defect):
    before, events = journal()
    if defect == 'missing-snapshot-service':
        before['services'].pop('market-data')
    elif defect == 'duplicate':
        events.append(deepcopy(events[-1]))
    elif defect == 'no-request':
        events.pop(0)
    elif defect == 'foreign':
        events[0]['service'] = 'market-data'
    elif defect == 'failure':
        events[1]['result'] = 'failed'
    elif defect == 'before-snapshot':
        events[0]['at_utc'] = '2026-10-30T20:04:59+00:00'
    elif defect == 'no-zone':
        events[0]['at_utc'] = '2026-10-30T20:05:01'
    elif defect == 'consumer-before-oms':
        events[10:14] = events[12:14] + events[10:12]
    with pytest.raises(ValueError):
        LIB['record_payload'](before, events, SERVICES.split(','), '/private/actual-actions.jsonl')


def owner(name, pid=100, invocation='a' * 32):
    return dict(Id='project-mai-tai-' + name + '.service', MainPID=str(pid),
        ActiveState='active', SubState='running', NRestarts='0',
        ExecMainStartTimestamp='Thu 2026-10-29 20:00:00 UTC', InvocationID=invocation, Result='success')


def fleet_pair():
    before = {name: owner(name) for name in PROOF.FLEET}
    current = deepcopy(before)
    for name in SERVICES.split(','):
        current[name] = owner(name, 200, 'b' * 32)
    return before, current


def test_complete_eleven_fleet_requires_new_selected_and_identical_untouched():
    before, current = fleet_pair()
    PROOF.verify_fleet(before, current, set(SERVICES.split(',')), changed=set(SERVICES.split(',')))
    assert len(before) == 11
    assert 'snapshot-worker' in before and 'option-a-daily-guard' in before


@pytest.mark.parametrize('defect', ['missing', 'untouched', 'old-pid', 'old-invocation', 'restart', 'inactive', 'future'])
def test_owner_epoch_unknown_or_changed_untouched_stops(defect):
    before, current = fleet_pair()
    if defect == 'missing':
        current.pop('trade-coach')
    elif defect == 'untouched':
        current['market-data']['MainPID'] = '999'
    elif defect == 'old-pid':
        current['oms']['MainPID'] = before['oms']['MainPID']
    elif defect == 'old-invocation':
        current['oms']['InvocationID'] = before['oms']['InvocationID']
    elif defect == 'restart':
        current['oms']['NRestarts'] = '1'
    elif defect == 'inactive':
        current['oms']['ActiveState'] = 'inactive'
    elif defect == 'future':
        current['oms']['ExecMainStartTimestamp'] = 'Thu 2099-10-29 20:00:00 UTC'
    with pytest.raises(ValueError):
        PROOF.verify_fleet(before, current, set(SERVICES.split(',')), changed=set(SERVICES.split(',')))


def test_log_split_error_and_rotation_are_never_dropped(tmp_path):
    path = tmp_path / 'oms.log'
    path.write_bytes(b'')
    info = path.stat()
    cursor = dict(path=str(path), device=info.st_dev, inode=info.st_ino, offset=0, bytes=0, tail='')
    path.write_bytes(b'2026-10-30 20:10:00,000 ER')
    PROOF.consume(tmp_path, 'oms', cursor)
    with path.open('ab') as stream:
        stream.write(b'ROR invalid\n')
    with pytest.raises(ValueError, match='error or traceback'):
        PROOF.consume(tmp_path, 'oms', cursor)
    assert (tmp_path / 'oms.startup-watch.log').read_bytes() == path.read_bytes()
    path.rename(tmp_path / 'old.log')
    path.write_bytes(b'new-file\n')
    with pytest.raises(ValueError, match='rotation/truncation'):
        PROOF.consume(tmp_path, 'oms', cursor)


def test_publish_and_state_parser_require_exact_positive_evidence():
    assert PROOF.PUBLISH.search(b'[BROKERDECIDES1-PUBLISH] generation=1 complete=True accounts=2 oldest_wire_age_ms=5')
    for raw in (b'complete=False accounts=2', b'[BROKERDECIDES1-PUBLISH] complete=True accounts=1', b'complete=True accounts=2'):
        assert not PROOF.PUBLISH.search(raw)
    raw = '\n'.join(key + '=' + value for key, value in owner('oms').items()).encode()
    assert PROOF.parse_state(raw) == owner('oms')
    with pytest.raises(ValueError):
        PROOF.parse_state(raw + b'\nMainPID=123')


def test_real_root_input_guard_cannot_be_bypassed_by_private_fixture(tmp_path):
    assert os.geteuid() != 0, 'this control must run as the actual unprivileged Main'
    path = tmp_path / 'plan.json'
    path.write_bytes(json.dumps(plan()).encode())
    path.chmod(0o600)
    with pytest.raises(ValueError, match='root-owned'):
        LIB['secure_read'](path)
    with pytest.raises(ValueError, match='root600'):
        PROOF.private(path)


def native_case():
    now = datetime(2026, 10, 30, 20, 5, 30, tzinfo=timezone.utc)
    at = int(now.timestamp() * 1000)
    accounts = ['live:schwab_1m_v2', 'live:orb']
    value = dict(schema=3, epoch='actual-publisher-epoch', generation=7,
        started_ms=at-2000, completed_ms=at-1000, local_complete=True,
        reason='complete_native_and_local_census', inflight=[],
        books=[dict(account=name, provider=provider, wire_started_ms=at-1900,
                    wire_completed_ms=at-1100, working=[], positions=[], complete=True,
                    scope_policy='all_native_working_including_previous_sessions', window_days=None)
               for name, provider in zip(accounts, ['schwab', 'webull'])])
    line = b'2026-10-30 20:05:29,001 INFO [BROKERDECIDES1-PUBLISH] generation=7 complete=True accounts=2 reason=complete_native_and_local_census'
    return value, line, accounts, owner('oms'), now


def test_existing_native_census_and_latest_log_must_agree_without_new_provider():
    value, line, accounts, instance, now = native_case()
    evidence = PROOF.native_census_evidence(json.dumps(value).encode(), line, accounts, instance, now)
    assert evidence['generation'] == 7
    assert evidence['all_native_working_symbol_counts'] == {name: 0 for name in accounts}
    assert evidence['all_native_position_symbol_counts'] == {name: 0 for name in accounts}
    assert evidence['source_sha256'] == PROOF.sha(json.dumps(value).encode())


@pytest.mark.parametrize('defect', ['native-working', 'manual-position', 'local-inflight', 'incomplete',
    'account', 'generation', 'stale', 'future', 'wire', 'unknown-log', 'publisher-before-owner'])
def test_native_census_unknown_never_becomes_flat_from_local_zero(defect):
    value, line, accounts, instance, now = native_case()
    if defect == 'native-working':
        value['books'][0]['working'] = ['NCPL']
    elif defect == 'manual-position':
        value['books'][1]['positions'] = ['MANUAL']
    elif defect == 'local-inflight':
        value['inflight'] = ['NCPL']
    elif defect == 'incomplete':
        value['books'][1]['complete'] = False
    elif defect == 'account':
        value['books'][1]['account'] = 'live:foreign'
    elif defect == 'generation':
        value['generation'] = 8
    elif defect == 'stale':
        value['started_ms'] -= 60000
    elif defect == 'future':
        value['completed_ms'] += 2000
    elif defect == 'wire':
        value['books'][0]['wire_started_ms'] = value['started_ms'] - 1
    elif defect == 'unknown-log':
        line = b'2026-10-30 20:05:29,001 WARNING [BROKERDECIDES1-PUBLISH] generation=7 UNKNOWN reason=read_failed'
    elif defect == 'publisher-before-owner':
        instance['ExecMainStartTimestamp'] = 'Fri 2026-10-30 20:05:29 UTC'
    with pytest.raises(ValueError):
        PROOF.native_census_evidence(json.dumps(value).encode(), line, accounts, instance, now)
