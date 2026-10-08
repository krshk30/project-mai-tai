"""Run the unchanged native gate with isolated command fixtures, never the box."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import shlex
import subprocess

import pytest

import runner

HOST = os.environ.get('OCT8_NATIVE_GATE_SSH_HOST')
MODERN_BASH = not subprocess.check_output(['bash', '--version'], text=True).startswith('GNU bash, version 3.')


@pytest.mark.parametrize('stamp,override', [
    ('2026-10-08T19:59:59+00:00', False), ('2026-10-08T20:00:00+00:00', True),
    ('2026-10-08T21:59:59+00:00', True), ('2026-10-08T22:00:00+00:00', False),
    ('2026-10-09T20:00:00+00:00', False),
])
def test_named_clock_only_authority_date_and_after_close_bound(stamp, override):
    command = runner.native_v2_command(datetime.fromisoformat(stamp))
    assert ('--clock-override' in command) is override
    if override:
        assert command[2:] == ['--clock-override', runner.CLOCK_REASON, '--i-accept-clock']
    assert '--operator-override' not in command and '--i-accept-bug2' not in command


def test_no_clock_abort_for_already_started_sequence_but_no_new_date_authority():
    now = datetime(2026, 10, 9, 4, 1, tzinfo=timezone.utc)
    assert '--clock-override' not in runner.native_v2_command(now)
    assert '--clock-override' in runner.native_v2_command(now, continuation=True)


@pytest.mark.parametrize('evidence,rc,deciding', [
    ({}, 0, 'GO. Zero armed segments AND flat'),
    ({'GATE_STATE': 'STATE_OK 1.0 FLYE'}, 1, '1 ARMED SEGMENT'),
    ({'GATE_STATE': 'STATE_STALE 90'}, 1, 'published state is STALE'),
    ({'GATE_STATE': 'STATE_ERROR ValueError'}, 1, 'cannot read published state'),
    ({'GATE_POSITIONS': 'POSOK|FLYE=1000'}, 1, 'broker not flat'),
    ({'GATE_POSITIONS': 'unreadable'}, 1, 'broker-position query FAILED'),
    ({'GATE_ROWS': '1'}, 1, '1 open managed row'),
])
@pytest.mark.skipif(not MODERN_BASH and not HOST, reason='Native Linux gate requires Bash4+; opt-in SSH runs only in-memory fixtures')
def test_real_unmodified_native_clock_override_never_waives_trading_proof(tmp_path, evidence, rc, deciding):
    job = Path(__file__).parent
    fixture = job / 'fixtures/native_gate_stub.sh'
    private = tmp_path / 'bin'
    private.mkdir()
    for name in ('sudo', 'date', 'psql', 'ls'):
        target = private / name
        shutil.copyfile(fixture, target)
        target.chmod(0o700)
    # Read the real Git checkout's native gate, never a rewritten gate fixture.
    repo = job.parents[3]
    command = [shutil.which('bash'), str(repo / 'ops/preflight/preflight_v2_restart.sh'),
               '--clock-override', runner.CLOCK_REASON, '--i-accept-clock']
    if HOST:
        # Explicit opt-in: source is sent in memory and all privileged/data
        # commands are functions. No remote file, state read or production write.
        source = (job / 'fixtures/native_gate_functions.sh').read_text() + '\n' + Path(command[1]).read_text()
        remote = shlex.join(['env', *[key + '=' + value for key, value in evidence.items()],
                             'bash', '-s', '--', *command[2:]])
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST, remote],
                                input=source, capture_output=True, text=True, timeout=30)
    else:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
            env={**os.environ, **evidence, 'PATH': str(private) + ':/usr/bin:/bin'})
    assert result.returncode == rc, result.stdout + result.stderr
    assert deciding in result.stdout
    assert '[OVERRIDE] clock gate' in result.stdout
    assert 'ARMED SEGMENT(S) accepted by the OPERATOR' not in result.stdout


def test_native_block_after_write_is_a_real_stop_not_prewrite_wait(tmp_path):
    run = runner.Run(tmp_path, {}, tmp_path)
    run.command = lambda *args, **kwargs: (1, None, None)
    run.note = lambda **kwargs: None
    with pytest.raises(RuntimeError, match='no armed or trading-gate override') as error:
        run.native_rehearsal(('v2',), continuation=True)
    assert not isinstance(error.value, runner.WaitWork)
