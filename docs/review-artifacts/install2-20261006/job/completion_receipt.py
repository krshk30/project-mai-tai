"""Summarize actual immutable completion receipts; no service or trading actions."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path('/home/trader/after-hours/2026-10-06/install2-5b8b4f64')
ATTEMPT = ROOT / 'job-archive64/attempt-install2-oct6-attended'
JOB = ROOT / 'job-seeded-fallback-proof'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def numbered(pattern):
    return sorted(ATTEMPT.glob(pattern), key=lambda p: int(p.name.split('-')[0]))[-1]


def command_output(name):
    paths = sorted(ATTEMPT.glob('*-command.json'), key=lambda p: int(p.name.split('-')[0]), reverse=True)
    for path in paths:
        value = read(path)
        if any(Path(arg).name == name for arg in value['argv']):
            return ATTEMPT / (str(int(path.name.split('-')[0]) + 1) + '-stdout.txt')
    raise ValueError('command receipt absent: ' + name)


def main():
    assert os.geteuid() == 0
    complete = read(ATTEMPT / 'COMPLETE.json')
    assert complete['application'] == '5b8b4f642bbc3c312be436d0e92adbc22d9e9f95'
    for name in ('oms', 'schwab-1m-v2', 'strategy', 'control'):
        state = complete['actual'][name]
        assert state['MainPID'] > 0 and state['ActiveState'] == 'active' and state['NRestarts'] == 0
    proof_path = numbered('*-post-start-proof.json')
    proof = read(proof_path)
    flat_raw = command_output('strict_flat_readonly.py').read_text()
    flat = json.loads(flat_raw[0 if flat_raw.startswith('{') else flat_raw.index('\n{') + 1:])
    assert flat['rc'] == 0 and not flat['blockers']
    sql = read(numbered('*-sql.json'))
    assert not any(sql[key] for key in ('buys', 'buy_intents', 'buy_fills'))
    redis = read(command_output('redis_checkpoint.py'))
    baseline = read(ATTEMPT / 'redis-baseline.json')
    assert redis['evicted_keys'] == baseline['evicted_keys'] == 0
    flags_output = command_output('expected_flags_check.py')
    source_numeric = read(Path('/home/trader/project-mai-tai/ops/health/expected_numeric.json'))['settings']
    numeric_names = {row['name'] for row in source_numeric}
    numeric_rows = [line for line in flags_output.read_text().splitlines()
                    if any(line.startswith('PASS flag=' + name + ' ') for name in numeric_names)]
    assert len(numeric_rows) == 8, 'canonical numeric8 receipt incomplete'
    report = dict(application=complete['application'], tree=complete['tree'], completed_at_utc=complete['completed_at_utc'],
        actual=complete['actual'], final_plan=read(JOB / 'release.json')['plan_commit'], final_release_sha256=sha(JOB / 'release.json'),
        prepare_release_sha256=sha(ROOT / 'job-archive64/release.json'),
        process_flags=proof['process_flags'], flaggate=read(ATTEMPT / 'flaggate-coverage.json'),
        numeric_pass_lines=numeric_rows, retry_numeric_pass_lines=[line for line in flags_output.read_text().splitlines()
        if line.startswith('PASS flag=strategy_schwab_1m_v2_retry_one_max_retries ')],
        redis_before=baseline, redis_after=redis, flat=flat,
        migration=sql['revision'], tickets_path=str(numbered('*-ticket-dispositions.json')),
        ticket_dispositions=read(numbered('*-ticket-dispositions.json')),
        official_report=proof['official_report'], official_report_sha256=proof['official_report_sha256'],
        control_display=complete['control_display'], log_disposition={name: {key: value for key, value in row.items() if key != 'lines'}
        for name, row in complete['new_process_log_disposition'].items()},
        daily=read(ATTEMPT / 'daily-installed.json'), preopen_sha256=sha(Path('/home/trader/preopen.sh')),
        preopen_diff_path=str(ATTEMPT / 'preopen.diff'), final_runner_log=str(ROOT / 'continuation-seeded.log'),
        final_runner_log_sha256=sha(ROOT / 'continuation-seeded.log'),
        original_stop_log_sha256=sha(ROOT / 'runner-archive64.log'),
        journal='/home/trader/fleet_health/deployments-20261006.md',
        scanner_next_session='UNMEASURED; Wed07:00-07:15ET', daily_first_run='UNMEASURED; Wed06:20ET')
    output = ATTEMPT / 'completion-receipt.json'
    with output.open('xb') as stream:
        stream.write((json.dumps(report, indent=2, sort_keys=True) + '\n').encode())
    journal = Path(report['journal'])
    with journal.open('a') as stream:
        stream.write('\n' + datetime.now(timezone.utc).isoformat() + ' codex install2 COMPLETE evidence=' + str(output)
            + ' sha256=' + sha(output) + ' final_plan=' + report['final_plan']
            + ' final_release=' + report['final_release_sha256'] + ' runner=' + report['final_runner_log']
            + ' runner_sha256=' + report['final_runner_log_sha256']
            + '\nMechanics: preserve original stale-book STOP; direct operator OMS-first continuation; no duplicate service action; '
            + 'declare actual control restart; unique immutable collector reports; literal overnight held state never called PASS; '
            + 'existing369s seeded fallback ERROR matched by exact population/capped-slots shape, other errors still block. '
            + 'Runner105testsPASS; application/flags/MI-NXL policy unchanged; no fallback/recovery used. '
            + 'Fresh-source line/scanner and first daily rehearsal UNMEASURED.\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(dict(receipt=str(output), sha256=sha(output),
                         completed_at_utc=report['completed_at_utc'], flaggate=report['flaggate'],
                         runner_sha256=report['final_runner_log_sha256'], preopen_sha256=report['preopen_sha256']), sort_keys=True))


if __name__ == '__main__':
    main()
