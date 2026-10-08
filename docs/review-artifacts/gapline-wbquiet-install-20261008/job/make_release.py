"""Generate release/approval from committed blobs, not the working tree."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

PREFIX = 'docs/review-artifacts/gapline-wbquiet-install-20261008/job/'
SOURCES = ('ops/systemd/deploy_service.sh', 'ops/bootstrap/08_install_runtime.sh',
           'ops/preflight/preflight_oms_restart.sh', 'ops/health/expected_flags.json',
           'ops/health/expected_flags_check.py', 'ops/health/v2_restart_evidence.py')


def git(*arguments):
    return subprocess.run(['git', *arguments], capture_output=True, check=True).stdout


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def release(plan, approved, box):
    for value in (plan, approved, box):
        if re.fullmatch('[0-9a-f]{40}', value) is None:
            raise ValueError('full commit SHA required')
        if git('rev-parse', value + '^{commit}').decode().strip() != value:
            raise ValueError('commit identity differs')
    blobs = {}
    names = git('ls-tree', '-r', '--name-only', plan, '--', PREFIX).decode().splitlines()
    for path in names:
        name = path.removeprefix(PREFIX)
        if '/' in name or name.startswith('test_') or not name.endswith(('.py', '.sh', '.service', '.timer')):
            continue
        blobs[name] = git('show', plan + ':' + path)
    required = {'runner.py', 'run.sh', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py'}
    if not required <= blobs.keys():
        raise ValueError('incomplete committed runner artifacts: ' + ','.join(sorted(required - blobs.keys())))
    result = dict(schema_version=1, date_et='2026-10-08', scope='gapline1-wbquiet1-oms-strategy-v2',
                  plan_commit=plan, approved_sha=approved, box_sha=box,
                  release_branch='codex/install-2026-10-08-' + approved[:12],
                  artifacts={name: digest(raw) for name, raw in sorted(blobs.items())},
                  source_hashes={name: digest(git('show', approved + ':' + name)) for name in SOURCES})
    raw = canonical(result)
    approval = canonical(dict(decision='APPROVED', authority='operator-standing-go',
        authority_source='Operator deploy-everything/after-close standing GO; reviewer 2026-10-08 STAGE TONIGHT scope',
        manifest_sha256=digest(raw), approved_sha=approved, plan_commit=plan,
        scope=result['scope'], no_trading_gate_override=True))
    return raw, approval, blobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--approved-sha', required=True)
    parser.add_argument('--box-sha', required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    raw, approval, _ = release(args.plan, args.approved_sha, args.box_sha)
    args.output_directory.mkdir(parents=True, exist_ok=False)
    (args.output_directory / 'release.json').write_bytes(raw)
    (args.output_directory / 'approval.json').write_bytes(approval)
    print(json.dumps(dict(plan_commit=args.plan, approved_sha=args.approved_sha,
                         manifest_sha256=digest(raw), approval_sha256=digest(approval)), sort_keys=True))


if __name__ == '__main__':
    main()
