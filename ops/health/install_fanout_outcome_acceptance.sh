#!/usr/bin/env bash
# Install the reviewed D6 acceptance report and its root-cron runner.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_check="$repo_root/ops/health/fanout_outcome_acceptance.py"
source_cron="$repo_root/ops/health/fanout_outcome_acceptance_cron.py"
source_lib="$repo_root/ops/health/fanout_outcome_acceptance_install_lib.sh"
target_dir=/home/trader/fanout_outcome_acceptance
target_check="$target_dir/check.py"
target_cron="$target_dir/cron.py"
python_bin=/home/trader/project-mai-tai/.venv/bin/python
begin_marker="# BEGIN project-mai-tai D6 outcome acceptance"
end_marker="# END project-mai-tai D6 outcome acceptance"

source "$source_lib"

require_root "${EUID:-$(id -u)}"

for source in "$source_check" "$source_cron"; do
  python3 - "$source" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
done
source_check_sha256=$(sha256sum "$source_check" | awk '{print $1}')
cron_line="17 4,5,6 * * 2-6 $python_bin $target_cron --acceptance $target_check --acceptance-sha256 $source_check_sha256 --out-dir $target_dir >> $target_dir/cron.log 2>&1"

install -d -o trader -g trader -m 0755 "$target_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -f "$target_check" ]] && ! cmp -s "$source_check" "$target_check"; then
  cp -a "$target_check" "$target_check.pre-versioned-$stamp"
fi
if [[ -f "$target_cron" ]] && ! cmp -s "$source_cron" "$target_cron"; then
  cp -a "$target_cron" "$target_cron.pre-versioned-$stamp"
fi
install -o root -g root -m 0755 "$source_check" "$target_check"
install -o root -g root -m 0755 "$source_cron" "$target_cron"
cmp "$source_check" "$target_check"
verify_d6_installed_copy "$source_cron" "$target_cron"
verify_d6_runtime "$python_bin" "$target_cron" "$target_check" "$source_check_sha256" "$target_dir"

current_cron=$(mktemp)
next_cron=$(mktemp)
cron_error=$(mktemp)
trap 'rm -f "$current_cron" "$next_cron" "$cron_error"' EXIT
if ! crontab -l >"$current_cron" 2>"$cron_error"; then
  if ! grep -qi "no crontab" "$cron_error"; then
    echo "REFUSED: could not read the existing root crontab" >&2
    cat "$cron_error" >&2
    exit 1
  fi
fi
begin_count=$(grep -Fxc "$begin_marker" "$current_cron" || true)
end_count=$(grep -Fxc "$end_marker" "$current_cron" || true)
if [[ "$begin_count" -gt 1 || "$end_count" -gt 1 || "$begin_count" -ne "$end_count" ]]; then
  echo "REFUSED: malformed existing D6 cron block begin=$begin_count end=$end_count" >&2
  exit 1
fi
if [[ "$begin_count" -eq 1 ]]; then
  begin_line=$(grep -nFx "$begin_marker" "$current_cron" | cut -d: -f1)
  end_line=$(grep -nFx "$end_marker" "$current_cron" | cut -d: -f1)
  if [[ "$begin_line" -ge "$end_line" ]]; then
    echo "REFUSED: existing D6 cron block markers are out of order" >&2
    exit 1
  fi
fi
awk -v begin="$begin_marker" -v end="$end_marker" '
  $0 == begin { inside=1; next }
  $0 == end { inside=0; next }
  !inside { print }
' "$current_cron" >"$next_cron"
{
  printf '\n%s\n' "$begin_marker"
  # The box cron is UTC. In EDT, 04:17 is the first attempt and 05:17/06:17 are retries. In EST,
  # 04:17 is 23:17 on the prior ET day and deduplicates its already-graded session; 05:17 is the
  # first attempt and 06:17 the one retry. cron.py deduplicates every completed result.
  printf '%s\n' "$cron_line"
  printf '%s\n' "$end_marker"
} >>"$next_cron"
crontab "$next_cron"

installed_cron=$(crontab -l)
verify_exactly_one_d6_schedule "$cron_line" "$installed_cron"

printf 'installed check_sha256=%s cron_sha256=%s schedule="%s" restart_required=0\n' \
  "$(sha256sum "$target_check" | awk '{print $1}')" \
  "$(sha256sum "$target_cron" | awk '{print $1}')" \
  "$cron_line"
