"""SOURCE-only controls; Main applies reviewed external integration before run."""
from datetime import datetime, timezone
import json
import importlib
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

@pytest.fixture
def job(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return importlib.import_module('atrsimlive1_refresh_job')


def args(tmp_path):
    parent = (tmp_path / 'today').resolve()
    parent.mkdir(mode=0o700)
    return SimpleNamespace(destination=str(parent / 'current.json'), source_manifest='/CONTROLLED/source.json',
        policy='/CONTROLLED/policy.json', incremental_cache_root='/CONTROLLED/cache',
        evidence_root='/CONTROLLED/evidence', dsn_env='MAI_TAI_DATABASE_URL', cache_closed_components=True)


def test_default_OFF_does_not_open_paths_import_reader_or_invoke(monkeypatch, job):
    def forbidden(*a, **k):
        pytest.fail('OFF attempted reader/priority/filesystem work')
    monkeypatch.setattr(job, 'invoke_today', forbidden)
    monkeypatch.setattr(job, 'run_lock', forbidden)
    monkeypatch.setattr(job.os, 'getpriority', forbidden)
    assert job.DEFAULT_ENABLED is False
    assert job.run(SimpleNamespace(), environment={})['status'] == 'OFF'


@pytest.mark.parametrize('value', ['yes', '1', '', True])
def test_switch_rejects_ambiguous_configuration(value, job):
    with pytest.raises(ValueError, match='explicit_refresh_boolean'):
        job.enabled({job.FLAG: value})


def test_actual_lock_contention_SKIPS_without_reader_or_lastgood_change(monkeypatch, tmp_path, job):
    options = args(tmp_path)
    destination = Path(options.destination)
    destination.write_bytes(b'CONTROLLED_LASTGOOD')
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    monkeypatch.setattr(job, 'invoke_today', lambda *a: pytest.fail('overlap invoked reader'))
    with job.run_lock(options.destination):
        result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'SKIP_ACTIVE' and result['children'] == []
    assert destination.read_bytes() == b'CONTROLLED_LASTGOOD'


def test_complete_requires_actual_closed_child_observation(monkeypatch, tmp_path, job):
    options = args(tmp_path)
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    def controlled_reader(options, deadline, observed):
        # Explicit CONTROLLED process port; no actual reader/native model claim.
        observed(SimpleNamespace(pid=123, returncode=0, poll=lambda: 0,
            stdout=SimpleNamespace(closed=True), stderr=SimpleNamespace(closed=True)))
        return 0
    monkeypatch.setattr(job, 'invoke_today', controlled_reader)
    result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'COMPLETE' and result['children'][0]['joined'] is True
    monkeypatch.setattr(job, 'invoke_today', lambda *a: 0)
    assert job.run(options, environment={job.FLAG: 'true'})['status'] == 'ABORT'


def test_reader_failure_not_mislabeled_active_and_no_raw_error_logged(monkeypatch, tmp_path, job):
    options = args(tmp_path)
    Path(options.destination).write_bytes(b'CONTROLLED_LASTGOOD')
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    def failed(*a):
        raise BlockingIOError('CONTROLLED_SECRET_NEVER_LOGGED')
    monkeypatch.setattr(job, 'invoke_today', failed)
    result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'ABORT'
    assert 'CONTROLLED_SECRET_NEVER_LOGGED' not in json.dumps(result)
    assert Path(options.destination).read_bytes() == b'CONTROLLED_LASTGOOD'


def test_production_argv_is_real_TODAY_reader_and_has_no_historical_or_partial_source(tmp_path, job):
    values = job.production_arguments(args(tmp_path))
    assert '--cache-closed-components' in values
    assert '--dsn-env' in values and 'MAI_TAI_DATABASE_URL' in values
    assert not {'--child', '--phase', '--input-binding', '--deadline-partial-seconds',
                '--cache-raw-pool', '--cache-since-last-run'} & set(values)
    assert values[values.index('--incremental-version') + 1] == 'C1_20261028_200908_ATRSIM_INCREMENTAL_V1'


def test_literal_CLI_seam_passes_reduced_wall_deadline_and_closure_observer(monkeypatch, tmp_path, job):
    import atrsimlive1
    options = args(tmp_path)
    observed, calls = lambda process: None, []
    expected = job.hashlib.sha256(Path(job.__file__).read_bytes()).hexdigest()
    monkeypatch.setattr(atrsimlive1, 'source_binding', lambda path: {
        'source_pins': {'tools/atrsimlive1_refresh_job.py': expected}})
    def controlled_run(values, **kwargs):
        calls.append((values, kwargs))
        return 0
    monkeypatch.setattr(atrsimlive1, '_run_main', controlled_run)
    deadline = job.time.monotonic() + 150
    assert job.invoke_today(options, deadline, observed) == 0
    values, kwargs = calls[0]
    assert 0 < kwargs['_child_wall_seconds'] <= 146
    assert kwargs['_publication_deadline'] == deadline
    assert kwargs['_child_closure_observer'] is observed
    assert '--dsn-env' in values


def test_expired_publication_keeps_lastgood_and_cleans_only_own_stage(monkeypatch, tmp_path, job):
    # Runs only AFTER Main adopts the explicit runtime deadline diff.
    import atrsimlive1_runtime as runtime
    options = args(tmp_path)
    destination = Path(options.destination)
    destination.write_bytes(b'CONTROLLED_LASTGOOD')
    destination.chmod(0o600)
    now = datetime.now(timezone.utc)
    table = dict(updated_at=None, source_asof=now.isoformat(), read_status='UNKNOWN',
        source_id='CONTROLLED', issues=['CONTROLLED_UNKNOWN'], rows=[], known_pnl_label='UNKNOWN',
        skipped_count=None, unknown_count=None, truncated=False)
    document = dict(schema=runtime.SCHEMA, trading_day=now.astimezone(runtime.ET).date().isoformat(),
        timezone='America/New_York', generated_at=now.isoformat(), source_asof=now.isoformat(),
        producer={}, native_execution_qualified=False, tables={key: dict(table) for key in runtime.KEYS})
    clocks = iter((0.0, 2.0))
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: next(clocks)))
    with pytest.raises(TimeoutError, match='refresh_publication_deadline'):
        runtime.publish(document, destination, now, deadline_monotonic=1.0)
    assert destination.read_bytes() == b'CONTROLLED_LASTGOOD'
    assert list(destination.parent.glob('.current-*.tmp')) == []


def test_late_finished_reader_cannot_claim_complete(monkeypatch, tmp_path, job):
    options = args(tmp_path)
    Path(options.destination).write_bytes(b'CONTROLLED_LASTGOOD')
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    def late(options, deadline, observed):
        observed(SimpleNamespace(pid=123, returncode=0, poll=lambda: 0,
            stdout=SimpleNamespace(closed=True), stderr=SimpleNamespace(closed=True)))
        return 0
    monkeypatch.setattr(job, 'invoke_today', late)
    clocks = iter((0.0, 151.0, 151.0))
    monkeypatch.setattr(job, 'time', SimpleNamespace(monotonic=lambda: next(clocks)))
    result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'ABORT' and result['elapsed_seconds'] == 151.0
    assert Path(options.destination).read_bytes() == b'CONTROLLED_LASTGOOD'


def test_active_sim2_service_priority_hard_cap_and_exact_five_minute_timer(job):
    # The live timer now owns ATRSIM2. The old refresh job retains its independent
    # 150-second deadline controls above; it is no longer this unit's executable.
    service = (job.ROOT / 'ops/systemd/project-mai-tai-atr-sim-today.service').read_text()
    timer = (job.ROOT / 'ops/systemd/project-mai-tai-atr-sim-today.timer').read_bytes()
    required = ('Type=oneshot', 'Nice=19', 'IOSchedulingClass=idle', 'TimeoutStartSec=120',
                'TimeoutStopSec=4', 'KillMode=control-group', 'Restart=no',
                'ExecStart=/usr/bin/ionice -c 3 ', 'tools/atrsim2_job.py')
    assert all(value in service for value in required)
    assert 'RuntimeMaxSec=260' not in service
    assert 'Environment=' + job.FLAG + '=true' not in service
    assert job.HARD_SECONDS == 150 and job.DRAIN_RESERVE_SECONDS == 4
    assert hashlib.sha256(timer).hexdigest() == 'b8786bc77d144a2c74f6dc69ec5cece319e2a73169c2a16f319bbfed2b6b2cde'
    assert b'OnCalendar=*-*-* 04..19:00/5:00 America/New_York' in timer
    assert b'Unit=project-mai-tai-atr-sim-today.service' in timer


def test_owned_literal_child_timeout_joins_closes_pipes_and_keeps_lastgood(monkeypatch, tmp_path, job):
    import atrsimlive1_runtime as runtime
    options = args(tmp_path)
    Path(options.destination).write_bytes(b'CONTROLLED_LASTGOOD')
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    def controlled_literal_child(options, deadline, observed):
        # Main-confined fixture process ONLY. No APP/SQL/provider/model imports.
        runtime.bounded_child([sys.executable, '-B', '-c',
            "import time; print('CONTROLLED_ONLY', flush=True); time.sleep(5)"],
            seconds=0.15, closure_observer=observed)
        return 0
    monkeypatch.setattr(job, 'invoke_today', controlled_literal_child)
    result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'ABORT' and result['failure_type'] == 'TimeoutError'
    assert len(result['children']) == 1
    child = result['children'][0]
    assert child['pid'] > 0 and child['returncode'] != 0
    assert child['joined'] and child['stdout_closed'] and child['stderr_closed']
    assert Path(options.destination).read_bytes() == b'CONTROLLED_LASTGOOD'


def test_owned_literal_child_success_has_actual_join_and_pipe_closure(monkeypatch, tmp_path, job):
    import atrsimlive1_runtime as runtime
    options = args(tmp_path)
    monkeypatch.setattr(job.os, 'getpriority', lambda *a: 19)
    def controlled_literal_child(options, deadline, observed):
        raw, _ = runtime.bounded_child([sys.executable, '-B', '-c', "print('CONTROLLED_ONLY')"],
                                      seconds=2, closure_observer=observed)
        assert raw == b'CONTROLLED_ONLY\n'
        return 0
    monkeypatch.setattr(job, 'invoke_today', controlled_literal_child)
    result = job.run(options, environment={job.FLAG: 'true'})
    assert result['status'] == 'COMPLETE' and len(result['children']) == 1
    child = result['children'][0]
    assert child['returncode'] == 0 and child['joined']
    assert child['stdout_closed'] and child['stderr_closed']
    assert result['native_execution_qualified'] is False
