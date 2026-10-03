"""Exact artifact approval for the Sunday resize; never approves itself."""
import hashlib
import json
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/home/trader/after-hours/2026-10-04/resize-job')
BASE = '608339894a1cfb33284e695196df55c18f312889'
PIN = 'bc59b220656b2ff24c4a557f4aa7b864334d4b14'
TREE = 'fde43f820650e1237d860b84a6cde9c5166faa96'
FILES = {'RESIZE_PLAN_2026-10-04.md', 'review_gate.py', 'resize.py', 'run_resize.sh',
         'merge_in_window.sh', 'project-mai-tai-resize-prepare-20261004.service',
         'project-mai-tai-resize-prepare-20261004.timer',
         'project-mai-tai-resize-postboot-20261004.service'}
# Do not convert reviewer acceptance of owner replay into a waiver for unrelated
# lost orders/controls. These need evidence and a new reviewed commit, not JSON.
READINESS_BLOCKERS = (
    'complete broker open-order enumeration not proven',
    'Redis transport/draft disposition and manual-stop preservation not proven',
)


def read(path):
    if path.is_symlink() or path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError(f'not root-only: {path}')
    return json.loads(path.read_text())


def require_execution_ready():
    if READINESS_BLOCKERS:
        raise ValueError('resize NOT EXECUTABLE: '+'; '.join(READINESS_BLOCKERS))


def verify(root=ROOT, now=None):
    now = now or datetime.now(ZoneInfo('America/New_York'))
    if now.astimezone(ZoneInfo('America/New_York')).date().isoformat() != '2026-10-04':
        raise ValueError('Sunday-only approval expired or not yet in window')
    release = read(root/'release.json')
    if (release['base_sha'], release['pinned_head'], release['pinned_tree']) != (BASE, PIN, TREE):
        raise ValueError('application binding changed')
    if set(release['artifacts']) != FILES or not re.fullmatch('[0-9a-f]{40}',release['plan_commit']):
        raise ValueError('release shape changed')
    for name, sha in release['artifacts'].items():
        path = root/name
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise ValueError(f'unsafe artifact: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError(f'artifact changed: {name}')
        if name.endswith(('.service', '.timer')):
            installed = Path('/etc/systemd/system')/name
            if hashlib.sha256(installed.read_bytes()).hexdigest() != sha:
                raise ValueError(f'installed unit changed: {name}')
    expected = {**release, 'reviewer':'claude-1', 'decision':'APPROVED',
                'owner_replay':'five_preserved_replace_events_only',
                'rdb_treatment':'archive_offline_keep_persistence_off',
                'provider_resize':'operator_only_2026-10-04_10:00_ET',
                'boot_registration':'explicit_stack_and_orb_schwab',
                'clock_override':'Sun 2026-10-04, no session; zero armed, zero managed, both brokers flat',
                'recovery':'one_failed_app_unit_start_once_no_redis_or_db_restart'}
    if read(root/'approval.json') != expected:
        raise ValueError('exact reviewed approval including cold-boot actions absent')
    require_execution_ready()
    return release


if __name__ == '__main__':
    try:
        verify()
        print('RESIZE_REVIEW_GATE APPROVED')
    except Exception as exc:
        print(f'RESIZE_REVIEW_GATE STOP {exc}')
        sys.exit(1)
