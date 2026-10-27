#!/usr/bin/env bash
# O-1019-2: operator's fresh preimage/identity capture, not a metadata publisher.
set -euo pipefail
umask 077
EXPECTED=${1:?supply the full operator-confirmed installed commit}
OUT=${2:?supply a new private absolute output directory}
REPO=/home/trader/project-mai-tai
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]]
[[ "$OUT" = /* ]]
test ! -e "$OUT"
test "$(git -C "$REPO" rev-parse HEAD)" = "$EXPECTED"
test -z "$(git -C "$REPO" status --porcelain)"
mkdir -m 700 "$OUT"

capture_epoch() {
  local target=$1 unit name properties
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$target.utc"
  cat /proc/sys/kernel/random/boot_id > "$target.boot"
  git -C "$REPO" rev-parse HEAD 'HEAD^{tree}' > "$target.git"
  test "$(head -1 "$target.git")" = "$EXPECTED"
  test -z "$(git -C "$REPO" status --porcelain)"
  : > "$target.units"
  for name in schwab-1m-v2 oms strategy orb-schwab market-data control reconciler; do
    unit="project-mai-tai-$name.service"
    properties=$(timeout 5 systemctl show "$unit" --no-pager \
      -p Id -p MainPID -p ExecMainStartTimestamp -p ExecMainStartTimestampMonotonic \
      -p InvocationID -p ActiveState -p SubState -p NRestarts)
    printf '%s\n\n' "$properties" >> "$target.units"
    grep -Eq '^MainPID=[1-9][0-9]*$' <<< "$properties"
    grep -Eq '^ExecMainStartTimestamp=.+ UTC$' <<< "$properties"
    grep -Eq '^ExecMainStartTimestampMonotonic=[1-9][0-9]*$' <<< "$properties"
    grep -Eq '^InvocationID=[0-9a-f]{32}$' <<< "$properties"
    grep -Fxq 'ActiveState=active' <<< "$properties"
    grep -Fxq 'SubState=running' <<< "$properties"
    grep -Fxq 'NRestarts=0' <<< "$properties"
  done
}

capture_epoch "$OUT/before"
for path in /home/trader/preopen.sh /home/trader/preopen-daily/runtime.json \
  /home/trader/preopen-daily/binding.json /home/trader/preopen-daily/run.sh \
  /home/trader/preopen-daily/daily.py; do
  test -f "$path"
  test ! -L "$path"
  test "$(stat -c %s "$path")" -le 1048576
  name=${path#/home/trader/}
  name=${name//\//_}
  cp -p -- "$path" "$OUT/$name"
  sha256sum "$path" >> "$OUT/preimages.sha256"
  stat -c '%n device=%d inode=%i mode=%a uid=%u gid=%g size=%s mtime=%y ctime=%z' \
    "$path" >> "$OUT/preimages.stat"
done
timeout 5 systemctl show project-mai-tai-{momentum-paper,option-a-daily-guard}.service \
  --no-pager -p Id -p MainPID -p ExecMainStartTimestamp -p InvocationID \
  -p ActiveState -p SubState -p NRestarts > "$OUT/dynamic-paper-guard.units"
capture_epoch "$OUT/after"
for suffix in boot git units; do
  cmp "$OUT/before.$suffix" "$OUT/after.$suffix"
done
sha256sum -c "$OUT/preimages.sha256" > "$OUT/preimages.verify"
printf 'CAPTURE ONLY: stable epoch/preimages; publication and native acceptance UNRUN\n' > "$OUT/RESULT.txt"
printf 'O-1019-2 capture retained at %s; no metadata changed\n' "$OUT"
