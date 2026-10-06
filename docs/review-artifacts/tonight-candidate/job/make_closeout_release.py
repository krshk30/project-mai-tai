"""Committed close-out blobs only; no service control included."""
import argparse
import hashlib
import json
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--plan', required=True)
a = p.parse_args()
assert len(a.plan) == 40 and all(c in '0123456789abcdef' for c in a.plan)
app = '7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf'
base = 'docs/review-artifacts/tonight-candidate/job/'
def digest(ref, path):
    return hashlib.sha256(subprocess.check_output(['git', 'show', ref+':'+path])).hexdigest()
artifacts = {name: digest(a.plan, base+name) for name in ('closeout.sh', 'actions.py', 'proof.py')}
sources = {'ops/health/'+n: digest(app, 'ops/health/'+n) for n in ('expected_flags_check.py', 'expected_flags.json', 'expected_numeric.json')}
print(json.dumps({'plan_commit': a.plan, 'approved_sha': app, 'scope': 'closeout-only-no-service-env-schema-action',
                  'standing_authority': 'operator-2026-10-05-20:45-and-21:07', 'artifacts': artifacts,
                  'application_blobs': sources}, indent=2, sort_keys=True))
