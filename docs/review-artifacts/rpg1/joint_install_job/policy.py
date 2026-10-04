"""Fixed October 3 deployment boundary. No recovery or broker-write capability."""
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re

ROOT = Path('/home/trader/after-hours/2026-10-03/rpg1-coldstart1-job')
RUN = Path('/home/trader/after-hours/2026-10-03/rpg1-coldstart1-run')
REPO = Path('/home/trader/project-mai-tai')
ENV = Path('/etc/project-mai-tai/project-mai-tai.env')
PREOPEN = Path('/home/trader/preopen.sh')
MONDAY = Path('/home/trader/after-hours/2026-10-05/guard-start-job/start-guard-20261005.sh')
JOURNAL = Path('/home/trader/fleet_health/deployments-20261003.md')
FLAT = Path('/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py')
FLAT_HASH = '831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e'
JOB = 'project-mai-tai-rpg1-coldstart1-install-20261003'
MONDAY_TIMER = 'project-mai-tai-option-a-guard-start-20261005.timer'
ET = ZoneInfo('America/New_York')
WINDOW = {'date': '2026-10-03', 'timezone': 'America/New_York',
          'prepare_deadline': '2026-10-03T23:00:00-04:00',
          'run_deadline': '2026-10-03T23:59:00-04:00'}
STOP = ('schwab-1m-v2', 'strategy', 'orb-schwab', 'orb', 'momentum-paper')
START = ('strategy', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper')
RESTARTED = ('oms',) + START
UNTOUCHED = ('market-data', 'control', 'market-capture', 'reconciler',
             'redis-server', 'postgresql@16-main')
SERVICES = RESTARTED + UNTOUCHED
CONSUMERS = {'strategy': 'strategy-engine', 'schwab-1m-v2': 'schwab-1m-v2',
             'orb': 'orb', 'orb-schwab': 'orb-schwab', 'momentum-paper': 'momentum-paper'}
OWNERS = frozenset(CONSUMERS.values())
FLAGS = {'MAI_TAI_MARKET_DATA_SUBSCRIPTION_STARTUP_ENABLED': 'true',
         'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED': 'true'}
COUNTS = {'boolean': 132, 'numeric': 8, 'combined': 140}
CATALOGS = ('expected_flags_check.py', 'expected_flags.json', 'expected_numeric.json',
            'v2_restart_evidence.py')
ARTIFACTS = ('policy.py', 'review_gate.py', 'proofs.py', 'install.py', 'run_install.sh',
             JOB + '.service', JOB + '.timer', 'test_joint_install.py', 'README.md')
BROKER_DISPOSITION = ('ACCEPT bounded 12h Schwab discovery and one-page Webull open-order read; '
                      'these do not prove complete broker inventory. No manual orders authorized. '
                      'Only the exact reviewed unexplained-order inventory may remain.')
IDLE_DISPOSITION = ('ACCEPT Saturday-idle disposition, not an OMS drain ACK: fresh weekend v2/ORB/'
                    'paper probes, no pending durable work, retained intent IDs archived without '
                    'replay, and no new intents after quiescence. Prior resize approval is not reused.')
RESET_DISPOSITION = ('ALLOW only orb-schwab reset-failed after this attempt SIGTERM stop, PID0, '
                     'ExecMainCode=1, ExecMainStatus=1, Result=exit-code and a bounded '
                     'same-invocation CancelledError shutdown traceback; no other reset or retry.')
COUNT_DISPOSITION = 'APPROVE 140/140 = 132 boolean + 8 numeric; numeric-only 8/8, not 136.'
# Recorded /var/log/project-mai-tai/orb-schwab.log-20261004, 10-03 SIGTERM.
# Line numbers/source text changed during checkout; file/function order did not.
KNOWN_CANCELLED_STACKS = ((
    ('mai-tai-orb-schwab', '<module>'),
    ('orb_schwab_app.py', 'run'),
    ('runners.py', 'run'),
    ('runners.py', 'run'),
    ('base_events.py', 'run_until_complete'),
    ('orb_schwab_app.py', 'main'),
    ('orb_schwab_app.py', 'run'),
    ('tasks.py', 'sleep'),
),)


class Refusal(RuntimeError):
    pass


def need(value, reason):
    if not value:
        raise Refusal(reason)


def now():
    return datetime.now(UTC)


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(value.tzinfo is not None, 'naive timestamp')
    return value


def window(instant, *, prepare=False):
    local = instant.astimezone(ET)
    need(local.date().isoformat() == WINDOW['date'], 'wrong install date')
    need(local.hour >= 18, 'no clock override: before 18 ET')
    key = 'prepare_deadline' if prepare else 'run_deadline'
    need(instant <= stamp(WINDOW[key]), key + ' expired')


def unit(name):
    need(name in SERVICES + ('tv-alerts',), 'unknown service ' + name)
    return name + '.service' if name in UNTOUCHED[-2:] else 'project-mai-tai-' + name + '.service'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def symbols(value):
    need(isinstance(value, list) and all(isinstance(x, str) and
         re.fullmatch(r'[A-Z0-9.\-^]{1,16}', x) for x in value), 'malformed symbols')
    need(len(value) == len(set(value)) and len(value) <= 2000, 'duplicate/oversized symbols')
    return sorted(value)


def active(row):
    return (row.get('ActiveState') == 'active' and row.get('SubState') == 'running'
            and int(row.get('MainPID', 0)) > 0 and row.get('NRestarts') == '0'
            and bool(row.get('InvocationID')))


def identity_key(row):
    return tuple(row[k] for k in ('MainPID', 'ExecMainStartTimestamp', 'InvocationID', 'NRestarts'))


def service_start(row):
    return datetime.strptime(row['ExecMainStartTimestamp'], '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)


def check_service_action(action, name):
    need((action == 'stop' and name in STOP) or (action == 'start' and name in START)
         or (action == 'restart' and name == 'oms')
         or (action == 'reset-failed' and name == 'orb-schwab'), 'forbidden service action')
