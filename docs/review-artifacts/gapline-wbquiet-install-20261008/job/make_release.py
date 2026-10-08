"""Generate release/approval from committed blobs, not the working tree."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PREFIX = 'docs/review-artifacts/gapline-wbquiet-install-20261008/job/'
REQUIRED_PRS = {'1126', '1129', '1127'}
SCOPE = 'gapline1-wbquiet1-linesrc2-orblive1-retirement-five-services'
SOURCES = ('ops/systemd/deploy_service.sh', 'ops/bootstrap/08_install_runtime.sh',
           'ops/preflight/preflight_oms_restart.sh', 'ops/health/expected_flags.json',
           'ops/health/expected_flags_check.py', 'ops/health/v2_restart_evidence.py',
           'src/project_mai_tai/services/orb_app.py', 'src/project_mai_tai/runtime_registry.py',
           'ops/systemd/project-mai-tai.target')


def git(*arguments):
    return subprocess.run(['git', *arguments], capture_output=True, check=True).stdout


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def verify_pin(pr, row, ledger):
    if ledger is None:
        raise ValueError('committed review-pins ledger required; no approval inferred')
    repo = git('rev-parse', '--show-toplevel').decode().strip()
    result = subprocess.run([sys.executable, str(Path(repo) / 'scripts/review_pin_gate.py'), 'verify',
        '--repo', repo, '--ledger', str(ledger), '--pr', pr, '--base', row['base'], '--head', row['reviewed_head']],
        check=True, capture_output=True, timeout=120)
    if not result.stdout.startswith(('PASS: PR #' + pr + ' ').encode()):
        raise ValueError('independent committed pin not proven ' + pr)
    return dict(stdout_sha256=digest(result.stdout), stderr_sha256=digest(result.stderr),
                ledger_commit=subprocess.run(['git', '-C', str(ledger), 'rev-parse', 'HEAD'],
                    check=True, capture_output=True, timeout=10).stdout.decode().strip())


def validate_reviews(review, approved, ledger):
    if review.get('approved_sha') != approved or type(review.get('line_enabled')) is not bool:
        raise ValueError('candidate review must bind exact application and LINE disposition')
    rows = review.get('landed_prs', {})
    if not REQUIRED_PRS <= rows.keys():
        raise ValueError('both reviewed GAP/WB union and LINESRC2 landings required')
    if approved not in {row.get('landing') for row in rows.values()}:
        raise ValueError('final application must be an exact reviewed landing, never moving main')
    pins = {}
    for pr, row in rows.items():
        if not pr.isdecimal() or row.get('pin_verdict') != 'PASS' or row.get('validate_verdict') != 'PASS':
            raise ValueError('unreviewed or unvalidated landing ' + pr)
        for key in ('base', 'reviewed_head', 'landing'):
            value = row.get(key)
            if not isinstance(value, str) or re.fullmatch('[0-9a-f]{40}', value) is None:
                raise ValueError('full review commit required ' + pr + ':' + key)
            if git('rev-parse', value + '^{commit}').decode().strip() != value:
                raise ValueError('review commit identity differs')
        # The merged UNION is the review unit. Original cherry-picked heads need
        # not be ancestors; the exact reviewed whole tree must be reproduced.
        git('merge-base', '--is-ancestor', row['landing'], approved)
        if git('rev-parse', row['landing'] + '^{tree}') != git('rev-parse', row['reviewed_head'] + '^{tree}'):
            raise ValueError('landing tree differs from reviewed patch union ' + pr)
        if git('diff', '--binary', row['base'], row['landing']) != git('diff', '--binary', row['base'], row['reviewed_head']):
            raise ValueError('landed patch content differs ' + pr)
        # Actual committed pin output/CI URLs travel with the package, not a
        # statement inferred from main or a successful git command.
        if not row.get('pin_receipt') or not row.get('validate_receipts') or not row.get('source_prs'):
            raise ValueError('review/pin/CI provenance missing ' + pr)
        pins[pr] = verify_pin(pr, row, ledger)
    if not {'1124', '1125'} <= set(rows['1129']['source_prs']):
        raise ValueError('WB and ORBLIVE union provenance required')
    return pins


def release(plan, approved, box, review, ledger=None):
    for value in (plan, approved, box):
        if re.fullmatch('[0-9a-f]{40}', value) is None:
            raise ValueError('full commit SHA required')
        if git('rev-parse', value + '^{commit}').decode().strip() != value:
            raise ValueError('commit identity differs')
    pins = validate_reviews(review, approved, ledger)
    git('merge-base', '--is-ancestor', box, approved)
    blobs = {}
    names = git('ls-tree', '-r', '--name-only', plan, '--', PREFIX).decode().splitlines()
    for path in names:
        name = path.removeprefix(PREFIX)
        if '/' in name or name.startswith('test_') or not name.endswith(('.py', '.sh', '.service', '.timer')):
            continue
        blobs[name] = git('show', plan + ':' + path)
    required = {'runner.py', 'run.sh', 'gate_readonly.py', 'proof_readonly.py', 'repin_preopen.py', 'retire_orb.py'}
    if not required <= blobs.keys():
        raise ValueError('incomplete committed runner artifacts: ' + ','.join(sorted(required - blobs.keys())))
    blobs['candidate-review.json'] = canonical(review)
    blobs['official_v2_restart_evidence.py'] = git('show', approved + ':ops/health/v2_restart_evidence.py')
    result = dict(schema_version=1, date_et='2026-10-08', scope=SCOPE,
                  plan_commit=plan, approved_sha=approved, box_sha=box,
                  release_branch='codex/install-2026-10-08-' + approved[:12],
                  line_enabled=review['line_enabled'], landed_reviews=review['landed_prs'], verified_pins=pins,
                  baseline_deploy_sha256=digest(git('show', box + ':ops/systemd/deploy_service.sh')),
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
    parser.add_argument('--review-receipt', type=Path, required=True)
    parser.add_argument('--review-ledger', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    raw, approval, _ = release(args.plan, args.approved_sha, args.box_sha,
                               json.loads(args.review_receipt.read_bytes()), args.review_ledger)
    args.output_directory.mkdir(parents=True, exist_ok=False)
    (args.output_directory / 'release.json').write_bytes(raw)
    (args.output_directory / 'approval.json').write_bytes(approval)
    print(json.dumps(dict(plan_commit=args.plan, approved_sha=args.approved_sha,
                         manifest_sha256=digest(raw), approval_sha256=digest(approval)), sort_keys=True))


if __name__ == '__main__':
    main()
