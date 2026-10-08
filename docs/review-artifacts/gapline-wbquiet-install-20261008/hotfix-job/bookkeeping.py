"""Carry actual prior restart evidence forward without claiming it passed."""
import hashlib
import json
from pathlib import Path

ORIGINAL_ATTEMPT = Path('/home/trader/after-hours/2026-10-08/gapline-wbquiet/job/attempt-20261008T202200963185Z')
ORIGINAL_JOB = ORIGINAL_ATTEMPT.parent
ORIGINAL_MANIFEST = 'c053e0324221a01b89236da3b0d6396b55099dd9c265acc2a85ffb4f9cd38d21'
ORIGINAL_RUNNER_LOG = '21802d9367527a4aec84c01e65db5c42c1b547b5c7577d95ea3e6f42488399a7'
ORIGINAL_FAILED_PROOF = '874f3b72cc83568993048daf53e11883f2d2ebb81e36fd42d73debc9937701a8'


def prior_install(bindings):
    required = {'release.json', 'ABORT.json', 'before-restart.json', 'service-identities.json',
                'runner.log', 'post-install-proof.json'}
    if set(bindings) != required:
        raise ValueError('complete prior FAILED install receipt bindings required')
    values = {}
    for name, expected in bindings.items():
        path = (ORIGINAL_JOB if name in {'release.json', 'ABORT.json'} else ORIGINAL_ATTEMPT) / name
        if path.is_symlink():
            raise ValueError('prior receipt symlink refused')
        raw = path.read_bytes()
        if len(raw) > 4_000_000 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('prior immutable receipt hash differs: ' + name)
        values[name] = raw
    if (bindings['release.json'] != ORIGINAL_MANIFEST or bindings['runner.log'] != ORIGINAL_RUNNER_LOG
            or bindings['post-install-proof.json'] != ORIGINAL_FAILED_PROOF):
        raise ValueError('foreign original install binding')
    release = json.loads(values['release.json'])
    abort = json.loads(values['ABORT.json'])
    if release['approved_sha'] != 'eced4599d05e72adab77551f18df8050a94028e2' or abort['stage'] != 'ten-minute-observation':
        raise ValueError('prior failed install scope differs')
    return dict(snapshot=json.loads(values['before-restart.json']),
                service_identities=json.loads(values['service-identities.json']),
                original_failed_proof=json.loads(values['post-install-proof.json']),
                original_abort=abort, hashes=bindings, original_verdict='ABORT; never COMPLETE')


def combined_record(prior, hotfix_before, hotfix_after, events):
    old_control = prior['service_identities']['control']
    control = hotfix_before['services']['control']
    if any(str(old_control[key]) != str(control[key]) for key in ('MainPID', 'NRestarts', 'ActiveState', 'SubState')):
        raise ValueError('prior authorized control identity changed; no arbitrary adoption')
    if control['ActiveState'] != 'active' or control['SubState'] != 'running' or int(control['NRestarts']) != 0:
        raise ValueError('control identity not healthy')
    completed = [event.get('deploy_finished') for event in events if 'deploy_finished' in event]
    if completed != ['oms', 'schwab-1m-v2']:
        raise ValueError('hotfix actual deploy sequence differs')
    for name in ('oms', 'schwab-1m-v2', 'strategy'):
        row = hotfix_after[name]
        if (int(row['MainPID']) <= 0 or str(row['MainPID']) == str(hotfix_before['services'][name]['MainPID'])
                or row['ActiveState'] != 'active' or row['SubState'] != 'running' or int(row['NRestarts']) != 0):
            raise ValueError('hotfix restarted identity unproven: ' + name)
    snapshot = prior['snapshot']
    record = dict(schema_version=1, snapshot_captured_at_utc=snapshot['captured_at_utc'],
                  service_actions={name: 'restarted' if name in {'oms', 'schwab-1m-v2', 'strategy', 'control'}
                                   else 'deliberately_untouched' for name in snapshot['services']})
    journal = dict(prior_install_verdict=prior['original_verdict'], prior_receipt_hashes=prior['hashes'],
                   prior_actual_control_restart=old_control, hotfix_events=events,
                   hotfix_restarted_group=['oms', 'schwab-1m-v2', 'strategy'],
                   bookkeeping_scope='original failed install plus PG hotfix; not a second control restart')
    return record, journal
