"""Oct6 operator-authorized continuation of the preserved phase-three stop."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

JOB = Path('/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job-archive64')
RELEASE = '3a55a2ee8be35f32e7b7148b76d36520280d802d5904585e875f492919a63ba0'


def resume(fx):
    # OMS owns book freshness: do not require its stopped writer to refresh itself.
    for completed, name in ((4, 'oms'), (5, 'schwab-1m-v2'), (6, 'strategy')):
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
    release = verify(JOB, RELEASE, datetime.now(timezone.utc))
    attempt = JOB / 'attempt-install2-oct6-attended'
    with Path('/run/lock/project-mai-tai-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exclusive(attempt / 'CONTINUATION-oms-first.json', canonical(dict(
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
        states(fx.before, fx.fleet(), 3)
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
            resume(fx)
        except BaseException as exc:
            actual = fx.fleet()
            exclusive(attempt / 'CONTINUATION_STOP.json', canonical(dict(
                actual=actual, error_type=type(exc).__name__, reason=str(exc), at_utc=fx.now().isoformat())))
            fx.command(['/home/trader/project-mai-tai/ops/health/preopen_alert.sh', 'ERROR',
                        'Oct6 install2 continuation stopped', attempt / 'CONTINUATION_STOP.json'], check=False)
            raise


if __name__ == '__main__':
    main()
