"""Pure receipt validation for the parent's ORB completion; no production API."""
import json
import re

OWNERS = {'strategy-engine', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper'}
FIELDS = OWNERS | {'_migration_complete', '_last_applied_id'}


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def cursor(value):
    need(isinstance(value, str) and re.fullmatch(r'[0-9]{1,20}-[0-9]{1,20}', value) is not None,
         'malformed Redis cursor')
    parts = tuple(map(int, value.split('-')))
    need(all(part <= 2**64 - 1 for part in parts), 'Redis cursor overflow')
    return parts


def validate_retirement(receipt):
    need(receipt.get('verdict') == 'RETIRED' and receipt.get('normal_replace_count') == 1,
         'retirement not proven')
    before, after = receipt['before']['owners'], receipt['after']['owners']
    for owners in (before, after):
        need(set(owners) == FIELDS and owners['_migration_complete'] == '1', 'owner population/marker differs')
        for name in OWNERS:
            symbols = json.loads(owners[name])
            need(isinstance(symbols, list) and len(symbols) <= 1000
                 and all(isinstance(symbol, str) for symbol in symbols)
                 and len(symbols) == len(set(symbols)), 'malformed owner symbols')
    need(json.loads(after['orb']) == [], 'retired ORB owner not empty')
    need(all(before[name] == after[name] for name in FIELDS - {'orb', '_last_applied_id'}),
         'retirement changed another owner')
    state = receipt['after']['state']
    need(state['MainPID'] == '0' and state['ActiveState'] == 'inactive'
         and state['UnitFileState'] == 'disabled', 'paper ORB retirement identity unproven')
    need(cursor(after['_last_applied_id']) >= cursor(receipt.get('request_id'))
         and cursor(receipt['request_id']) > cursor(before['_last_applied_id']), 'retirement request not applied')


def validate_restart(receipt, app, historical, actual, moment, system_time, now):
    need(receipt.get('approved_sha') == app
         and receipt.get('unit') == 'project-mai-tai-orb-schwab.service'
         and receipt.get('command_argv') == ['systemctl', 'restart', 'project-mai-tai-orb-schwab.service'],
         'authorized ORB-Schwab restart receipt absent')
    if receipt.get('rc') != 0:
        # The parent's actual command return code was not retained. Preserve
        # that limit rather than fabricate rc 0; bind its successful new start.
        need(receipt.get('rc') is None
             and receipt.get('rc_observation') == 'UNMEASURED'
             and receipt.get('observation_basis') == 'systemd_start_journal_and_live_identity'
             and actual.get('Result') == 'success' and actual.get('ExecMainStatus') == '0',
             'restart command failed or successful start unproven')
        journal_start = moment(receipt['journal_started_at_utc'])
        start_second = system_time(actual['ExecMainStartTimestamp'])
        need(start_second <= journal_start <= now and (journal_start - start_second).total_seconds() < 1,
             'journal does not bind the new restart second')
    old, new = receipt['before'], receipt['after']
    need(all(old.get(key) == value for key, value in historical.items()), 'restart does not bind historical ACK')
    keys = ('MainPID', 'NRestarts', 'ActiveState', 'SubState', 'ExecMainStartTimestamp', 'InvocationID')
    need(all(new.get(key) == actual.get(key) for key in keys), 'restart receipt/current identity differs')
    need(new['MainPID'].isdigit() and int(new['MainPID']) > 0 and new['MainPID'] != old['MainPID']
         and new['NRestarts'] == '0' and new['ActiveState'] == 'active' and new['SubState'] == 'running'
         and new['InvocationID'] and new['InvocationID'] != old['InvocationID'], 'new ORB-Schwab identity unproven')
    # systemctl's existing official identity pin uses whole seconds. Require
    # the actual recorded command second, never adopt a prior or future start.
    invoked = moment(receipt['at_utc']).replace(microsecond=0)
    need(invoked <= system_time(new['ExecMainStartTimestamp']) <= now, 'restart start outside receipt window')
