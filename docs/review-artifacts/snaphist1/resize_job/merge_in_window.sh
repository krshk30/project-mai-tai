#!/usr/bin/env bash
# Local attended half; production has no GitHub CLI. No credentials sent to box.
set -euo pipefail
JOB=/home/trader/after-hours/2026-10-03/resize-job
RUN=/home/trader/after-hours/2026-10-03/resize-run
BASE=608339894a1cfb33284e695196df55c18f312889
PIN=bc59b220656b2ff24c4a557f4aa7b864334d4b14
TREE=fde43f820650e1237d860b84a6cde9c5166faa96
LOCAL=$(git rev-parse --show-toplevel)
PY="$(git rev-parse --path-format=absolute --git-common-dir)/../.venv/bin/python"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ssh mai-tai-vps "sudo /home/trader/project-mai-tai/.venv/bin/python '$JOB/review_gate.py' && test \"\$(systemctl show project-mai-tai-resize-prepare-20261003.service -p ActiveState --value)\" = activating"
ssh mai-tai-vps "sudo cat '$JOB/release.json'" > "$TMP/release.json"
ssh mai-tai-vps "sudo cat '$RUN/merge-ready.json'" > "$TMP/ready.json"
"$PY" - "$TMP" "$0" <<'PY'
import hashlib,json,sys
from datetime import UTC,datetime
from pathlib import Path
p=Path(sys.argv[1]); release=json.loads((p/'release.json').read_text()); ready=json.loads((p/'ready.json').read_text())
assert hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()==release['artifacts']['merge_in_window.sh']
assert all(ready[k]==release[k] for k in ('plan_commit','base_sha','pinned_head','pinned_tree'))
assert 0<=(datetime.now(UTC)-datetime.fromisoformat(ready['at_utc'])).total_seconds()<480
PY
git fetch origin main review-pins
test "$(git rev-parse origin/main)" = "$BASE"
LEDGER=$(git rev-parse origin/review-pins)
git clone --shared --no-checkout "$LOCAL" "$TMP/ledger"
git -C "$TMP/ledger" switch --detach "$LEDGER"
"$PY" scripts/review_pin_gate.py verify --repo "$LOCAL" --ledger "$TMP/ledger" --pr 1087 --base "$BASE" --head "$PIN"
gh pr view 1087 --repo krshk30/project-mai-tai --json state,headRefOid,baseRefOid,statusCheckRollup > "$TMP/pr.json"
"$PY" - "$TMP/pr.json" "$PIN" "$BASE" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))
assert p['state']=='OPEN' and p['headRefOid']==sys.argv[2] and p['baseRefOid']==sys.argv[3]
v=[c for c in p['statusCheckRollup'] if c.get('name')=='validate']
assert len(v)==2 and all(c.get('conclusion')=='SUCCESS' for c in v)
pins=[c for c in p['statusCheckRollup'] if c.get('name')=='independent-review-pin']
assert pins and max(pins,key=lambda c:c['startedAt'])['conclusion']=='SUCCESS'
PY
gh pr merge 1087 --repo krshk30/project-mai-tai --rebase --match-head-commit "$PIN"
SHA=$(gh pr view 1087 --repo krshk30/project-mai-tai --json mergeCommit --jq '.mergeCommit.oid')
git fetch origin main
test "$(git rev-parse origin/main)" = "$SHA"
test "$(git rev-parse "$SHA^{tree}")" = "$TREE"
git merge-base --is-ancestor "$BASE" "$SHA"
test "$(git rev-list --count "$BASE..$SHA")" = 1
"$PY" - "$TMP/release.json" "$SHA" <<'PY' | ssh mai-tai-vps "sudo /home/trader/project-mai-tai/.venv/bin/python '$JOB/resize.py' receipt"
import json,sys
print(json.dumps({'plan_commit':json.load(open(sys.argv[1]))['plan_commit'],'sha':sys.argv[2]}))
PY
printf 'MERGED #1087 @ %s inside reviewed Saturday preparation\n' "$SHA"
