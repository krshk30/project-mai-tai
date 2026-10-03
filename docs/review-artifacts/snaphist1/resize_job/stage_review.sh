#!/usr/bin/env bash
# Explicit staging after reviewer requests it; never writes approval or starts apps.
set -euo pipefail
PREFIX=docs/review-artifacts/snaphist1
JOB=/home/trader/after-hours/2026-10-04/resize-job
PLAN=$(git rev-parse HEAD)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
test -z "$(git status --porcelain)"
git show "$PLAN:$PREFIX/RESIZE_PLAN_2026-10-04.md" > "$TMP/RESIZE_PLAN_2026-10-04.md"
for name in review_gate.py resize.py run_resize.sh merge_in_window.sh \
  project-mai-tai-resize-prepare-20261004.service project-mai-tai-resize-prepare-20261004.timer \
  project-mai-tai-resize-postboot-20261004.service; do
  git show "$PLAN:$PREFIX/resize_job/$name" > "$TMP/$name"
done
python3 - "$TMP" "$PLAN" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1])
data={'plan_commit':sys.argv[2],'base_sha':'608339894a1cfb33284e695196df55c18f312889',
      'pinned_head':'bc59b220656b2ff24c4a557f4aa7b864334d4b14','pinned_tree':'fde43f820650e1237d860b84a6cde9c5166faa96',
      'artifacts':{x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted(p.iterdir())}}
with (p/'release.json').open('x') as out: json.dump(data,out,sort_keys=True,indent=2)
print(json.dumps(data,sort_keys=True,indent=2))
PY
REMOTE=$(ssh mai-tai-vps 'mktemp -d /tmp/resize-review.XXXXXX')
scp "$TMP/"* "mai-tai-vps:$REMOTE/"
ssh mai-tai-vps sudo bash -s -- "$REMOTE" "$JOB" <<'SH'
set -euo pipefail
BEFORE=$(mktemp)
systemctl show project-mai-tai-{control,market-capture,market-data,oms,orb,orb-schwab,reconciler,schwab-1m-v2,strategy,momentum-paper}.service \
  -p Id -p MainPID -p ExecMainStartTimestamp -p NRestarts > "$BEFORE"
install -d -o root -g root -m 0700 /home/trader/after-hours/2026-10-04
mkdir -m 0700 "$2"
for path in "$1/"*; do install -o root -g root -m 0600 "$path" "$2/$(basename "$path")"; done
systemd-analyze verify "$2/"*.service "$2/"*.timer
for path in "$2/"*.service "$2/"*.timer; do
  test ! -e "/etc/systemd/system/$(basename "$path")"
  install -o root -g root -m 0644 "$path" "/etc/systemd/system/$(basename "$path")"
done
/home/trader/project-mai-tai/.venv/bin/python - "$2" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); release=json.loads((p/'release.json').read_text())
for name,want in release['artifacts'].items(): assert hashlib.sha256((p/name).read_bytes()).hexdigest()==want,name
print('STAGED_HASHES_MATCH',release['plan_commit'])
PY
test ! -e "$2/approval.json"
systemctl daemon-reload
systemctl enable --now project-mai-tai-resize-prepare-20261004.timer
# Postboot is enabled only during approved preparation, never while merely staged.
systemctl list-timers --all project-mai-tai-resize-prepare-20261004.timer --no-pager
systemctl show project-mai-tai-{control,market-capture,market-data,oms,orb,orb-schwab,reconciler,schwab-1m-v2,strategy,momentum-paper}.service \
  -p Id -p MainPID -p ExecMainStartTimestamp -p NRestarts > "$2/staged-service-identities.txt"
diff -u "$BEFORE" "$2/staged-service-identities.txt"
test ! -e /home/trader/after-hours/2026-10-04/resize-run
test ! -e "$2/approval.json"
printf 'STAGED_ONLY approval absent; no application action\n'
SH
