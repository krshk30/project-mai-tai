"""Fail-closed release/receipt validation; this module never mutates the host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

from policy import (ARTIFACTS, BROKER_DISPOSITION, CATALOGS, COUNTS, COUNT_DISPOSITION,
                    ENV, FLAGS, FLAT, FLAT_HASH, IDLE_DISPOSITION, JOB, MONDAY,
                    MONDAY_TIMER, OWNERS, PREOPEN, RESET_DISPOSITION, ROOT, SERVICES,
                    WINDOW, active, digest, need, now, stamp, symbols, unit, window)

STATUS_CONTRACT = {
    'broker_orders': ['canceled', 'cancelled', 'expired', 'filled', 'rejected'],
    'trade_intents': ['cancelled', 'filled', 'rejected'],
    'nfq': ['retired'],
    'rpg': ['expired', 'filled', 'placed', 'refused'],
}
FILE_PATHS = frozenset(map(str, (ENV, PREOPEN, MONDAY, FLAT))) | {
    '/home/trader/restart_evidence/' + name for name in CATALOGS}
UNIT_NAMES = frozenset(unit(x) for x in SERVICES + ('tv-alerts',)) | {
    MONDAY_TIMER, MONDAY_TIMER.replace('.timer', '.service')}
DISPOSITIONS = {'broker_inventory': BROKER_DISPOSITION, 'idle': IDLE_DISPOSITION,
                'reset_failed': RESET_DISPOSITION, 'denominator': COUNT_DISPOSITION}


def hex_value(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value)


def keys(value, expected, label):
    need(isinstance(value, dict) and set(value) == set(expected), label + ' fields differ')


def unique_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def secure_file(path, *, mode=None):
    path = Path(path)
    need(not path.is_symlink(), 'symlink: ' + str(path))
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0,
         'not a root-owned regular file: ' + str(path))
    permissions = stat.S_IMODE(info.st_mode)
    need(permissions == mode if mode is not None else permissions in {0o600, 0o700, 0o644},
         'file permissions: ' + str(path))
    need(info.st_size < 8 * 1024 * 1024, 'oversized approval artifact')


def validate_release(release):
    keys(release, ('schema_version', 'ready', 'job', 'window', 'denominators', 'flags',
                   'application', 'plan', 'reviews', 'host', 'artifacts'), 'release')
    need(release['schema_version'] == 1 and release['ready'] is True and release['job'] == JOB,
         'release NOT READY')
    need(release['window'] == WINDOW and release['denominators'] == COUNTS
         and release['flags'] == FLAGS, 'window/count/flag boundary changed')
    app = release['application']
    keys(app, ('box_sha', 'approved_sha', 'tree', 'rpg_merge_sha', 'coldstart_merge_sha',
               'commits', 'changed_paths', 'source_sha256'), 'application')
    for key in ('box_sha', 'approved_sha', 'tree', 'rpg_merge_sha', 'coldstart_merge_sha'):
        need(hex_value(app[key], 40), 'unbound application ' + key)
    need(app['box_sha'] == '250ab18458f4d806aa8bcdf98d787fff5bb57df4', 'BOX_SHA changed')
    need(app['commits'] and all(hex_value(x, 40) for x in app['commits'])
         and len(app['commits']) == len(set(app['commits'])), 'commit inventory absent/invalid')
    need(app['rpg_merge_sha'] in app['commits'] and app['coldstart_merge_sha'] in app['commits'],
         'both merged PR commits must be in exact range')
    need(app['changed_paths'] and app['changed_paths'] == sorted(set(app['changed_paths']))
         and all(safe_repo_path(p) for p in app['changed_paths']), 'changed-path inventory')
    need(app['source_sha256'] and all(p.startswith('src/') and safe_repo_path(p)
         and hex_value(h, 64) for p, h in app['source_sha256'].items()), 'source hashes absent')
    plan = release['plan']
    keys(plan, ('commit', 'path', 'sha256'), 'plan')
    need(hex_value(plan['commit'], 40) and hex_value(plan['sha256'], 64)
         and plan['path'] == 'docs/review-artifacts/rpg1/JOINT_INSTALL_PLAN_2026-10-03.md', 'plan pin')
    need(isinstance(release['reviews'], list) and len(release['reviews']) == 2, 'two PR reviews required')
    need({r['pr'] for r in release['reviews']} == {1085, 1088}, 'wrong PR reviews')
    for review in release['reviews']:
        keys(review, ('pr', 'head', 'base', 'record_commit', 'pin_path', 'pin_sha256', 'ci_urls'), 'review')
        need(all(hex_value(review[k], 40) for k in ('head', 'base', 'record_commit'))
             and safe_repo_path(review['pin_path']) and review['pin_path'].startswith('records/')
             and hex_value(review['pin_sha256'], 64), 'review pin unbound')
        need(len(review['ci_urls']) == 2 and len(set(review['ci_urls'])) == 2
             and all(re.fullmatch(r'https://github\.com/[^/]+/[^/]+/actions/runs/\d+', u)
                     for u in review['ci_urls']), 'two exact-head green Validate references required')
    host = release['host']
    keys(host, ('captured_at', 'services', 'tv_alerts', 'files', 'units', 'subscription_inputs',
                'accepted_unexplained_orders', 'intent_ids', 'status_contract', 'pager_url'), 'host')
    need(stamp(host['captured_at']).astimezone(__import__('policy').ET).date().isoformat() == WINDOW['date'],
         'host evidence wrong date')
    need(set(host['services']) == set(SERVICES) and all(active(v) for v in host['services'].values()),
         'twelve healthy identities required')
    need(host['tv_alerts'].get('MainPID') == '0' and host['tv_alerts'].get('ActiveState') == 'inactive',
         'tv-alerts must remain inactive')
    need(set(host['files']) == FILE_PATHS, 'host file inventory incomplete')
    for path, record in host['files'].items():
        keys(record, ('sha256', 'uid', 'gid', 'mode'), 'file pin')
        need(hex_value(record['sha256'], 64) and all(type(record[k]) is int for k in ('uid', 'gid', 'mode')),
             'file pin metadata')
    need(host['files'][str(FLAT)]['sha256'] == FLAT_HASH, 'strict helper pin changed')
    need(set(host['units']) == UNIT_NAMES and all(hex_value(h, 64) for h in host['units'].values()),
         'unit inventory incomplete')
    need(host['status_contract'] == STATUS_CONTRACT, 'terminal-state inventory not reviewed')
    inputs = host['subscription_inputs']
    need(set(inputs) == OWNERS, 'five read-only input proofs required')
    for owner, proof in inputs.items():
        keys(proof, ('symbols', 'evidence', 'sha256'), 'subscription input')
        symbols(proof['symbols'])
        need(isinstance(proof['evidence'], str) and len(proof['evidence']) >= 20
             and hex_value(proof['sha256'], 64), 'independent restored/current input evidence required')
    need(inputs['momentum-paper']['symbols'] == [], 'paper must be weekend-empty')
    need(isinstance(host['accepted_unexplained_orders'], list)
         and all(isinstance(x, dict) and set(x) == {'broker', 'id', 'status'}
                 and x['broker'] in {'schwab', 'webull'} and x['id'] and x['status']
                 for x in host['accepted_unexplained_orders']), 'unexplained-order inventory')
    need(isinstance(host['intent_ids'], list) and len(host['intent_ids']) <= 2000
         and all(re.fullmatch(r'\d+-\d+', x) for x in host['intent_ids'])
         and len(host['intent_ids']) == len(set(host['intent_ids'])), 'retained intent inventory')
    need(isinstance(host['pager_url'], str)
         and re.fullmatch(r'https://ntfy\.sh/[A-Za-z0-9_-]+', host['pager_url']), 'pager URL must be bound')
    need(set(release['artifacts']) == set(ARTIFACTS)
         and all(hex_value(h, 64) for h in release['artifacts'].values()), 'artifact inventory')


def safe_repo_path(path):
    return isinstance(path, str) and not path.startswith('/') and '..' not in Path(path).parts


def validate_approval(approval, release_hash):
    need(approval == {'schema_version': 1, 'reviewer': 'claude-1', 'decision': 'APPROVED',
                      'release_sha256': release_hash, 'window': WINDOW, 'dispositions': DISPOSITIONS},
         'approval hash/window/explicit dispositions do not match')


def verify(root=ROOT, *, prepare=False, instant=None, installed_units=Path('/etc/systemd/system'),
           approval_required=True):
    need(os.geteuid() == 0, 'root approval gate required')
    window(instant or now(), prepare=prepare)
    need(not root.is_symlink() and root.is_dir(), 'job directory is a symlink/missing')
    info = root.stat()
    need(info.st_uid == 0 and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         'job directory must be root:root 0700')
    for name in (('release.json', 'approval.json') if approval_required else ('release.json',)):
        secure_file(root / name, mode=0o600)
    release = unique_json((root / 'release.json').read_bytes())
    validate_release(release)
    if approval_required:
        validate_approval(unique_json((root / 'approval.json').read_bytes()), digest(root / 'release.json'))
    plan = root / Path(release['plan']['path']).name
    secure_file(plan, mode=0o600)
    need(digest(plan) == release['plan']['sha256'], 'staged plan changed')
    for name, expected in release['artifacts'].items():
        secure_file(root / name)
        need(digest(root / name) == expected, 'artifact changed: ' + name)
        if name.endswith(('.service', '.timer')):
            secure_file(installed_units / name, mode=0o644)
            need(digest(installed_units / name) == expected, 'installed unit changed: ' + name)
    return release


if __name__ == '__main__':
    try:
        need(sys.argv[1:] in ([], ['--prepare'], ['--validate-release']), 'unsupported gate argument')
        release_only = '--validate-release' in sys.argv
        verify(prepare='--prepare' in sys.argv or release_only, approval_required=not release_only)
        print(('RELEASE VALID (approval still required)' if release_only else 'APPROVAL VERIFIED')
              + '; exact release, 140/140, no clock override')
    except Exception as exc:
        print('NOT READY: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
