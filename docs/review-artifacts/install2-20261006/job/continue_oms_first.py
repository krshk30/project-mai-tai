"""Oct6 operator-authorized continuation of the preserved phase-three stop."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

ORIGINAL_JOB = Path('/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job-archive64')
JOB = Path('/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job-final-held-proof')


def resume(fx, completed_before=3):
    # OMS owns book freshness: do not require its stopped writer to refresh itself.
    for completed, name in ((4, 'oms'), (5, 'schwab-1m-v2'), (6, 'strategy')):
        if completed <= completed_before:
            continue
        if completed > 4:
            fx.flat()
            fx.redis()
            fx.sql(fx.install_started)
        try:
            fx.action('start', name)
            fx.checkpoint(completed)
        except Exception:
            fx.recover_start_failure(name)
            raise
        if name == 'oms':
            fx.flat()
    if completed_before < 7:
        fx.action('restart', 'control')
        fx.checkpoint(7)
    from post_proof import collect
    from release_policy import canonical
    owners = ('oms', 'schwab-1m-v2', 'strategy')
    proof = collect(fx, {name: fx.log_base[name] for name in owners}, fx.started, owners=owners)
    fx.receipt('post-start-proof.json', canonical(proof))
    fx.closeout()
    fx.complete()


def main():
    sys.path.insert(0, str(JOB))
    from attended import Real, verify, ENV, env_candidate
    from cumulative import load_install1
    from daily import exclusive
    from release_policy import canonical, need, states
    need(os.geteuid() == 0 and 'TZ' not in os.environ, 'root native UTC required')
    need(len(sys.argv) == 2, 'published continuation release hash required')
    release = verify(JOB, sys.argv[1], datetime.now(timezone.utc))
    attempt = ORIGINAL_JOB / 'attempt-install2-oct6-attended'
    with Path('/run/lock/project-mai-tai-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exclusive(attempt / 'CONTINUATION-final-held-proof.json', canonical(dict(
            authority='operator direct message: start OMS, wait healthy, v2, strategy, control; freshness after OMS up',
            original_stop=json.loads((attempt / 'STOP.json').read_bytes()),
            continuation_sha256=__import__('hashlib').sha256(Path(__file__).read_bytes()).hexdigest(),
            at_utc=datetime.now(timezone.utc).isoformat())))
        fx = Real(JOB, release, attempt)
        fx.counter = max(int(re.match(r'(\d+)-', path.name)[1]) for path in attempt.iterdir()
                         if re.match(r'(\d+)-', path.name))
        fx.before = json.loads((attempt / 'fleet-before.json').read_bytes())
        fx.install1 = load_install1(release['binding'], fx.before)
        fx.db_before = json.loads(sorted(attempt.glob('*-sql.json'), key=lambda p: int(p.name.split('-')[0]))[0].read_bytes())
        fx.redis_baseline = attempt / 'redis-baseline.json'
        fx.log_base = json.loads((attempt / 'logs-old-processes-stopped.json').read_bytes())
        fx.last = json.loads((attempt / 'phase-3.json').read_bytes())
        fx.install_started = datetime.fromisoformat(json.loads((attempt / 'before-restart.json').read_bytes())['captured_at_utc'])
        fx.env_before = (attempt / 'env.before').read_bytes()
        fx.env_after = env_candidate(fx.env_before)
        need(ENV.read_bytes() == fx.env_after, 'applied env drift')
        fx.source_advanced = True
        fx.old_helpers = release['binding']['old_helper_hashes']
        fx.source(release['approved_sha'], verify_objects=False)
        completed = 7 if (attempt / 'phase-7.json').exists() else 3
        states(fx.before, fx.fleet(), completed)
        if completed == 7:
            fx.last = json.loads((attempt / 'phase-7.json').read_bytes())
            fx.started = {name: fx.last[name] for name in ('oms', 'schwab-1m-v2', 'strategy', 'control')}
            for name in ('oms', 'schwab-1m-v2', 'strategy', 'control'):
                paths = sorted(attempt.glob('*-' + name + '-start-returned.json'), key=lambda p: int(p.name.split('-')[0]))
                fx.start_returned[name] = datetime.fromisoformat(json.loads(paths[-1].read_bytes())['post_return_utc'])
            paths = sorted(attempt.glob('*-control-startup.json'), key=lambda p: int(p.name.split('-')[0]))
            recorded = json.loads(paths[-1].read_bytes())['ranges']
            need(len(recorded) == 1, 'control source range ambiguous')
            span = recorded[0]
            path = Path(span['path'])
            from post_proof import read_range
            need(read_range(path, span['offset'], span['end'])[1]['sha256'] == span['sha256'], 'control recorded log source changed')
            stat = path.stat()
            fx.log_base['control'] = dict(path=str(path), inode=stat.st_ino, device=stat.st_dev, offset=span['offset'])
            fx.control_page()
        fx.redis()
        def recover_start_failure(name):
            from attended import REPO
            from release_policy import BOX
            fx.receipt('START_FAILURE-authorized-fallback.json', canonical(dict(
                failed_service=name, actual=fx.fleet(), fallback_sha=BOX,
                authority='operator direct message: if any start fails revert checkout and start three services')))
            for service in ('schwab-1m-v2', 'strategy', 'oms'):
                fx.command(['systemctl', 'stop', 'project-mai-tai-' + service + '.service'], check=False, timeout=120)
            fx.command(['runuser', '-u', 'trader', '--', 'git', '-C', REPO, 'switch', '--detach', BOX])
            fx.source(BOX, verify_objects=False)
            for service in ('oms', 'schwab-1m-v2', 'strategy'):
                fx.command(['systemctl', 'start', 'project-mai-tai-' + service + '.service'], check=False, timeout=120)
            fx.receipt('FALLBACK-actual.json', canonical(dict(actual=fx.fleet(), sha=BOX)))
        fx.recover_start_failure = recover_start_failure
        try:
            resume(fx, completed)
        except BaseException as exc:
            actual = fx.fleet()
            exclusive(attempt / 'CONTINUATION_FINAL_STOP.json', canonical(dict(
                actual=actual, error_type=type(exc).__name__, reason=str(exc), at_utc=fx.now().isoformat())))
            fx.command(['/home/trader/project-mai-tai/ops/health/preopen_alert.sh', 'ERROR',
                        'Oct6 install2 continuation stopped', attempt / 'CONTINUATION_FINAL_STOP.json'], check=False)
            raise


if __name__ == '__main__':
    main()
