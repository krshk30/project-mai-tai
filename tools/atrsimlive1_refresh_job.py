"""Default-OFF production TODAY RR refresh; never uses historical input.

Deploy only with the reviewed external CLI/runtime/systemd integration patch.
C1 owns installation; Main owns integration/tests only. No install authority
is supplied by this source. C1 2026-10-29 13:47:46.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import stat
import time

FLAG = 'MAI_TAI_ATR_SIMULATION_REFRESH_JOB_ENABLED'
DEFAULT_ENABLED = False
HARD_SECONDS = 150.0
DRAIN_RESERVE_SECONDS = 4.0
ROOT = Path(__file__).resolve().parents[1]


class RefreshStopped(RuntimeError):
    pass


class RefreshActive(RuntimeError):
    pass


def enabled(environment):
    value = environment.get(FLAG)
    if value is None:
        return DEFAULT_ENABLED
    if type(value) is not str or value.strip().lower() not in ('true', 'false'):
        raise ValueError('explicit_refresh_boolean_required')
    return value.strip().lower() == 'true'


@contextmanager
def run_lock(destination):
    parent = Path(destination).parent
    if (not parent.is_absolute() or '..' in parent.parts
            or any(p.is_symlink() for p in (parent, *parent.parents))):
        raise ValueError('literal_refresh_destination_parent_required')
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('owned_nonwritable_refresh_parent_required')
    fd = os.open(parent / '.refresh.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('private_owned_refresh_lock_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RefreshActive('refresh_previous_job_active') from None
        yield
    finally:
        os.close(fd)


def production_arguments(args):
    # Literal original production reader: no file input, fake RR marker,
    # copied clock, synthetic route, partial output, or historical caller.
    values = ['--source-manifest', args.source_manifest, '--policy', args.policy,
        '--stored-null-policy', 'C1_20261028_195403_TODAY_PAPER_PRESENT_NULL_REGULAR_V1',
        '--incremental-version', 'C1_20261028_200908_ATRSIM_INCREMENTAL_V1',
        '--incremental-cache-root', args.incremental_cache_root,
        '--evidence-root', args.evidence_root, '--dsn-env', args.dsn_env,
        '--destination', args.destination]
    if args.cache_closed_components:
        values.append('--cache-closed-components')
    return values


def invoke_today(args, deadline, observed):
    today = importlib.import_module('atrsimlive1')
    if (today.ROOT != ROOT or Path(today.__file__).resolve() != ROOT / 'tools/atrsimlive1.py'):
        raise ValueError('literal_production_CLI_origin_required')
    producer = today.source_binding(args.source_manifest)
    actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if producer['source_pins'].get('tools/atrsimlive1_refresh_job.py') != actual:
        raise ValueError('reviewed_refresh_job_dependency_required')
    remaining = deadline - time.monotonic() - DRAIN_RESERVE_SECONDS
    if remaining <= 0:
        raise TimeoutError('refresh_pre_child_deadline')
    # These three private kwargs are the separate explicit integration diff.
    # A missing integration fails TypeError; NEVER fallback to old240seconds.
    return today._run_main(production_arguments(args), _failure={'directory': None, 'role': 'parent'},
        _child_wall_seconds=min(HARD_SECONDS - DRAIN_RESERVE_SECONDS, remaining),
        _publication_deadline=deadline, _child_closure_observer=observed)


def run(args, *, environment=None):
    began = time.monotonic()
    if not enabled(os.environ if environment is None else environment):
        return {'status': 'OFF', 'switch': FLAG, 'native_execution_qualified': False}
    if os.getpriority(os.PRIO_PROCESS, 0) != 19:
        raise ValueError('reviewed_systemd_nice19_required')
    closures = []
    def observed(process):
        closures.append(dict(pid=process.pid, returncode=process.returncode,
            joined=process.poll() is not None,
            stdout_closed=process.stdout.closed, stderr_closed=process.stderr.closed))
    try:
        with run_lock(args.destination):
            result = invoke_today(args, began + HARD_SECONDS, observed)
            if (result != 0 or len(closures) != 1 or closures[0]['returncode'] != 0
                    or not all(closures[0][key] for key in ('joined', 'stdout_closed', 'stderr_closed'))
                    or time.monotonic() - began > HARD_SECONDS):
                raise ValueError('complete_bounded_refresh_closure_required')
    except RefreshActive:
        return dict(status='SKIP_ACTIVE', elapsed_seconds=time.monotonic() - began,
                    children=closures, lastgood_preserved=True, native_execution_qualified=False)
    except Exception as error:
        # No SQL/DSN/provider/raw SDK text is logged. Actual child closure is
        # recorded; an empty witness is UNKNOWN, never fabricated joined=True.
        return dict(status='ABORT', failure_type=type(error).__name__, children=closures,
                    elapsed_seconds=time.monotonic() - began, native_execution_qualified=False)
    return dict(status='COMPLETE', children=closures, elapsed_seconds=time.monotonic() - began,
                native_execution_qualified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest', required=True)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--incremental-cache-root', required=True)
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--destination', default='/var/lib/project-mai-tai/atr-simulation/today/current.json')
    parser.add_argument('--dsn-env', choices=('MAI_TAI_DATABASE_URL', 'DATABASE_URL'), required=True)
    parser.add_argument('--cache-closed-components', action='store_true')
    args = parser.parse_args(argv)
    def stopped(signum, frame):
        raise RefreshStopped('refresh_owned_stop')
    previous = {signum: signal.signal(signum, stopped) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = run(args)
        print(json.dumps(dict(event='ATRSIMLIVE1_REFRESH', **result), sort_keys=True), flush=True)
        return 0 if result['status'] in ('OFF', 'SKIP_ACTIVE', 'COMPLETE') else 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
