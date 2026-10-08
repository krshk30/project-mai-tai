from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

import runner


@pytest.mark.parametrize('stamp, expected', [
    ('2026-10-08T19:59:59+00:00', 'BEFORE_CLOSE'),
    ('2026-10-08T20:00:00+00:00', 'READY'),
    ('2026-10-09T03:59:59+00:00', 'READY'),
    ('2026-10-09T04:00:00+00:00', 'EXPIRED'),
    ('2026-10-07T22:00:00+00:00', 'EXPIRED'),
])
def test_first_write_date_and_after_close_only(stamp, expected):
    assert runner.window(datetime.fromisoformat(stamp)) == expected


@pytest.mark.parametrize('results, expected, attempts, sleeps', [
    ([2, 0], 0, 2, [60]), ([2, 2, 2], 2, 3, [60, 60]),
    ([1, 0], 1, 1, []), ([0], 0, 1, []), ([124], 124, 1, []),
])
def test_rc_two_only_bounded_read_retries_preserve_every_attempt(results, expected, attempts, sleeps):
    calls, waits, receipts = [], [], []
    def call(number):
        calls.append(number)
        return results[len(calls) - 1]
    assert runner.retry_read(call, sleep=waits.append, record=lambda n, rc: receipts.append((n, rc))) == expected
    assert calls == list(range(1, attempts + 1))
    assert waits == sleeps
    assert receipts == list(enumerate(results[:attempts], 1))


def environment():
    return (runner.LINE + '=false\n' + runner.HANDOFF + '=false\n'
            '# Retained settings\nOTHER=true\n').encode()


def test_env_add_changes_only_gap_line_and_retains_rpg_false():
    before = environment()
    assert runner.env_candidate(before) == before.replace((runner.LINE + '=false').encode(), (runner.LINE + '=true').encode()) + (runner.GAP + '=true\n').encode()
    assert runner.env_candidate(runner.env_candidate(before)) == runner.env_candidate(before)
    assert runner.env_candidate(before, False) == before + (runner.GAP + '=true\n').encode()


@pytest.mark.parametrize('extra', [
    lambda: runner.GAP + '=false\n' + runner.GAP + '=true\n',
    lambda: runner.GAP.lower() + '=true\n',
    lambda: runner.LINE + '=false\n',
    lambda: runner.HANDOFF + '=false\n',
    lambda: runner.GAP + '=garbage\n',
])
def test_env_duplicates_aliases_unknown_refuse(extra):
    with pytest.raises(RuntimeError):
        runner.env_candidate(environment() + extra().encode())


def test_env_cannot_turn_handoff_on():
    with pytest.raises(RuntimeError):
        runner.env_candidate(environment().replace((runner.HANDOFF + '=false').encode(), (runner.HANDOFF + '=true').encode()))


def catalogs():
    original = {'schema_version': 1, 'flags': [
        {'name': 'strategy_schwab_1m_v2_line_chart_restoration_enabled', 'expected': True},
        {'name': 'strategy_schwab_1m_v2_atr_reprice_handoff_enabled', 'expected': False},
        {'name': 'orb_paper_enabled', 'expected': False, 'owning_service': 'orb-schwab'},
    ]}
    approved = {'schema_version': 1, 'flags': [
        *original['flags'][:2],
        {'name': 'strategy_schwab_1m_v2_gap_line_carry_enabled', 'expected': True,
         'owning_service': 'schwab-1m-v2', 'ruling': 'Approved carry card'},
    ]}
    return original, approved


def test_catalog_reviewed_inventory_replaces_retired_fields_and_line_on():
    original, approved = catalogs()
    result = json.loads(runner.catalog_candidate(runner.canonical(original), approved))
    assert result['flags'][0]['expected'] is True
    assert result['flags'][1]['expected'] is False
    assert result['flags'][2] == approved['flags'][2]
    assert not any(row['name'] == 'orb_paper_enabled' for row in result['flags'])
    assert runner.catalog_candidate(runner.canonical(result), approved) == runner.canonical(result)


def test_catalog_duplicates_and_rpg_on_refuse():
    original, approved = catalogs()
    original['flags'].append(original['flags'][0])
    with pytest.raises(RuntimeError):
        runner.catalog_candidate(runner.canonical(original), approved)
    original, approved = catalogs()
    approved['flags'].append(approved['flags'][0])
    with pytest.raises(RuntimeError):
        runner.catalog_candidate(runner.canonical(original), approved)


def test_approval_and_all_artifact_bytes_bound(tmp_path):
    from test_retire_orb import receipt
    names = ('runner.py', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py', 'run.sh',
             'retire_orb.py', 'official_v2_restart_evidence.py', 'candidate-review.json', 'completed-retirement.json')
    artifacts = {}
    candidate = dict(approved_sha='a' * 40, line_enabled=True, landed_prs={'1126': {}, '1127': {}},
                     candidate_prs=['1126', '1127'], completed_retirement=receipt())
    for name in names:
        raw = runner.canonical(candidate) if name == 'candidate-review.json' else (
            runner.canonical(receipt()) if name == 'completed-retirement.json' else name.encode())
        (tmp_path / name).write_bytes(raw)
        artifacts[name] = hashlib.sha256(raw).hexdigest()
    release = dict(approved_sha='a' * 40, plan_commit='b' * 40, box_sha='c' * 40,
                   date_et='2026-10-08', scope=runner.SCOPE, line_enabled=True, landed_reviews=candidate['landed_prs'],
                   release_branch='codex/install-2026-10-08-' + 'a' * 12, artifacts=artifacts)
    raw = runner.canonical(release)
    (tmp_path / 'release.json').write_bytes(raw)
    approval = dict(decision='APPROVED', authority='operator-standing-go', manifest_sha256=runner.digest(raw),
                    approved_sha=release['approved_sha'], plan_commit=release['plan_commit'])
    (tmp_path / 'approval.json').write_bytes(runner.canonical(approval))
    assert runner.verify_package(tmp_path) == release
    (tmp_path / 'run.sh').write_bytes(b'drift')
    with pytest.raises(RuntimeError, match='hash differs'):
        runner.verify_package(tmp_path)


def test_command_captures_both_streams_and_rc(tmp_path):
    run = runner.Run(tmp_path, {}, tmp_path)
    rc, stdout, stderr = run.command(['/bin/sh', '-c', 'echo receipt; echo unreadable >&2; exit 2'])
    assert rc == 2
    assert stdout.read_bytes() == b'receipt\n'
    assert stderr.read_bytes() == b'unreadable\n'
    assert len(run.events) == 2
    assert run.events[-1]['stdout_sha256'] == runner.digest(stdout.read_bytes())
    assert run.events[-1]['stderr_sha256'] == runner.digest(stderr.read_bytes())


def test_own_timer_date_and_no_installer_start_in_stager():
    job = Path(__file__).parent
    timer = (job / 'project-mai-tai-gapline-wbquiet-20261008.timer').read_text()
    assert 'OnCalendar=2026-10-08 16..23:*:00 America/New_York' in timer
    assert 'Persistent=false' in timer
    stager = (job / 'stage.py').read_text()
    assert "'enable','--now',unit + '.timer'" in stager
    assert "'start',unit + '.service'" not in stager
    assert 'MAI_TAI_ALLOW_LIVE_RESTART=1' not in (job / 'runner.py').read_text()


def test_runner_no_clock_check_after_first_write_exact_restarts_retirement_no_migration():
    source = Path(runner.__file__).read_text()
    body = source.split("self.backup_write(ENV, env)", 1)[1].split('def main()', 1)[0]
    assert 'window(' not in body
    assert 'MAI_TAI_RUN_MIGRATIONS=0' in body
    assert "for target in ('oms', 'schwab-1m-v2', 'orb-schwab', 'control')" in body
    assert "['systemctl', 'disable', '--now', 'project-mai-tai-orb.service']" not in body
    assert "'publish'" not in body
    assert 'CancelledError' not in source
    assert 'chmod(0o775)' not in source


def test_date_function_accepts_aware_utc_only_for_actual_timer_decision():
    assert runner.window(datetime(2026, 10, 8, 20, tzinfo=timezone.utc)) == 'READY'
