#!/usr/bin/env bash
# Install the shared INC1/unexercised watch and both DST-safe root cron blocks.
set -euo pipefail

repo_root=${PAGER_INSTALL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
source_watch="$repo_root/ops/health/unexercised_watch.py"
test_mode=${PAGER_INSTALL_TEST_MODE:-0}
if [[ $test_mode == 1 ]]; then
  target_watch=${PAGER_INSTALL_TARGET:?test target is required}
  crontab_bin=${PAGER_INSTALL_CRONTAB_BIN:?test crontab is required}
else
  target_watch=/home/trader/unexercised_watch/watch.py
  crontab_bin=crontab
fi
watch_begin='# BEGIN project-mai-tai unexercised-condition watch (claude-1 2026-09-08, operator GO)'
watch_end='# END project-mai-tai unexercised-condition watch'
inc1_begin='# BEGIN project-mai-tai INC1 uncovered-position pager (#928)'
inc1_end='# END project-mai-tai INC1 uncovered-position pager'

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

[[ -f $source_watch ]] || { echo 'REFUSED: watch source missing' >&2; exit 1; }
python3 - "$source_watch" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
source_sha=$(hash_file "$source_watch")
[[ $source_sha =~ ^[a-f0-9]{64}$ ]] || { echo 'REFUSED: invalid watch hash' >&2; exit 1; }

render_blocks() {
  printf '%s\n' "$watch_begin"
  printf '%s\n' '# Every 15 minutes, ET weekdays 07:00-20:00 inclusive; --cron guards DST and UTC day rollover.'
  printf '*/15 * * * * [ "$(sha256sum %s | cut -d" " -f1)" = "%s" ] && %s --cron --state %s --status %s >> %s 2>&1\n' \
    "$target_watch" "$source_sha" \
    '/home/trader/project-mai-tai/.venv/bin/python /home/trader/unexercised_watch/watch.py' \
    '/home/trader/unexercised_watch/state.json' \
    '/home/trader/unexercised_watch/STATUS.txt' \
    '/home/trader/unexercised_watch/cron.log'
  printf '%s\n' "$watch_end" "$inc1_begin"
  printf '%s\n' '# Every minute, ET weekdays 07:00-20:00 inclusive; same installed file and SHA guard.'
  printf '* * * * * [ "$(sha256sum %s | cut -d" " -f1)" = "%s" ] && %s --cron --inc1 --state %s --status %s >> %s 2>&1\n' \
    "$target_watch" "$source_sha" \
    '/home/trader/project-mai-tai/.venv/bin/python /home/trader/unexercised_watch/watch.py' \
    '/home/trader/unexercised_watch/inc1-state.json' \
    '/home/trader/unexercised_watch/INC1_STATUS.txt' \
    '/home/trader/unexercised_watch/inc1-cron.log'
  printf '%s\n' "$inc1_end"
}

if [[ ${1:-} == --print-cron ]]; then
  render_blocks
  exit 0
fi
[[ ${1:-} == --install ]] || { echo "usage: $0 --print-cron|--install" >&2; exit 2; }
if [[ $test_mode != 1 ]]; then
  [[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'REFUSED: run as root' >&2; exit 1; }
  [[ -n ${EXPECTED_SHA:-} ]] || { echo 'REFUSED: EXPECTED_SHA is required' >&2; exit 1; }
  [[ $(git -C "$repo_root" rev-parse HEAD) == "$EXPECTED_SHA" ]] ||
    { echo 'REFUSED: checkout SHA differs from EXPECTED_SHA' >&2; exit 1; }
  [[ -z $(git -C "$repo_root" status --porcelain) ]] ||
    { echo 'REFUSED: checkout is not clean' >&2; exit 1; }
fi

install_watch() {
  if [[ $test_mode == 1 ]]; then
    install -m 0644 "$1" "$2"
  else
    install -o root -g root -m 0644 "$1" "$2"
  fi
}

tmp=$(mktemp -d)
committed=0
cron_changed=0
rollback() {
  if [[ $committed -eq 0 ]]; then
    if [[ -f $tmp/watch.before ]]; then
      install_watch "$tmp/watch.before" "$target_watch" || true
    fi
    if [[ $cron_changed -eq 1 ]]; then
      "$crontab_bin" "$tmp/cron.before" || true
    fi
  fi
  rm -rf "$tmp"
}
trap rollback EXIT
trap 'exit 130' INT TERM

[[ -f $target_watch ]] || { echo 'REFUSED: installed watch missing' >&2; exit 1; }
cp -p "$target_watch" "$tmp/watch.before"
"$crontab_bin" -l > "$tmp/cron.before" || { echo 'REFUSED: root crontab unreadable' >&2; exit 1; }
for marker in "$watch_begin" "$watch_end" "$inc1_begin" "$inc1_end"; do
  [[ $(grep -Fxc "$marker" "$tmp/cron.before" || true) -eq 1 ]] ||
    { echo "REFUSED: missing or duplicate cron marker: $marker" >&2; exit 1; }
done
[[ $(grep -Fc "$target_watch" "$tmp/cron.before" || true) -eq 2 ]] ||
  { echo 'REFUSED: expected exactly two existing watch cron lines' >&2; exit 1; }
awk -v wb="$watch_begin" -v we="$watch_end" -v ib="$inc1_begin" -v ie="$inc1_end" '
  $0 == wb || $0 == ib {if (skip) bad=1; skip=1; next}
  $0 == we || $0 == ie {if (!skip) bad=1; skip=0; next}
  !skip {print}
  END {if (skip || bad) exit 1}
' "$tmp/cron.before" > "$tmp/cron.after"
[[ $(grep -Fc "$target_watch" "$tmp/cron.after" || true) -eq 0 ]] ||
  { echo 'REFUSED: unmarked watch cron line would remain' >&2; exit 1; }
render_blocks >> "$tmp/cron.after"

install_watch "$source_watch" "$tmp/watch.new"
mv "$tmp/watch.new" "$target_watch"
[[ $(hash_file "$target_watch") == "$source_sha" ]] ||
  { echo 'REFUSED: installed watch hash mismatch' >&2; exit 1; }
cron_changed=1
"$crontab_bin" "$tmp/cron.after" || { echo 'REFUSED: new root crontab rejected' >&2; exit 1; }
"$crontab_bin" -l > "$tmp/cron.readback"
cmp -s "$tmp/cron.after" "$tmp/cron.readback" ||
  { echo 'REFUSED: root crontab readback differs' >&2; exit 1; }
[[ $(grep -Fc "$source_sha" "$tmp/cron.readback" || true) -eq 2 ]] ||
  { echo 'REFUSED: both SHA guards were not installed' >&2; exit 1; }
committed=1
printf 'installed_watch_sha256=%s cron_guards=2 root_cron=verified restart_required=0\n' "$source_sha"
