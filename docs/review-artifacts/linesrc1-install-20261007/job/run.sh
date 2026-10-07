#!/usr/bin/env bash
# [codex] Immutable package entry point. Parent alone stages/activates.
set -euo pipefail
unset TZ
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/trader/project-mai-tai/src
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bootstrap_closeout() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" != 75 && ! -e "$here/write-started.json" ]]; then
    printf 'STOP bootstrap rc=%s; no application recovery authorized\n' "$rc" >&2
    (set -o noclobber; printf '{"verdict":"ABORTED_BOOTSTRAP","app_writes":false,"rc":%s}\n' "$rc" > "$here/write-started.json") || true
    systemctl disable --now project-mai-tai-linesrc1-20261007.timer || true
    systemctl show project-mai-tai-linesrc1-20261007.timer --property=ActiveState --property=UnitFileState || true
    timeout 35 /home/trader/project-mai-tai/ops/health/preopen_alert.sh ERROR \
      "Oct7 LINESRC1 STOP bootstrap rc=$rc; no app recovery" "$here/write-started.json" || true
    [[ "$rc" != 0 ]] || rc=1
  fi
  exit "$rc"
}
trap bootstrap_closeout EXIT
IFS= read -r sha < "$here/release.sha256"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || exit 1
set +e
/home/trader/project-mai-tai/.venv/bin/python -B "$here/runner.py" "$sha"
rc=$?
set -e
exit "$rc"
