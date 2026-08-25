#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV_DIR="$REPO_DIR/.venv"
APP_ENV_FILE="/etc/project-mai-tai/project-mai-tai.env"
RUN_MIGRATIONS="${MAI_TAI_RUN_MIGRATIONS:-1}"

if [[ ! -f "$APP_ENV_FILE" ]]; then
  echo "missing env file: $APP_ENV_FILE"
  exit 1
fi

cd "$REPO_DIR"

sudo -u trader "$PYTHON_BIN" -m venv "$VENV_DIR"
sudo -u trader "$VENV_DIR/bin/python" -m pip install --upgrade pip
sudo -u trader "$VENV_DIR/bin/pip" install -e "$REPO_DIR"

if [[ "$RUN_MIGRATIONS" == "1" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a

  sudo --preserve-env=MAI_TAI_DATABASE_URL -u trader "$VENV_DIR/bin/alembic" upgrade head
else
  echo "Skipping alembic upgrade because MAI_TAI_RUN_MIGRATIONS=$RUN_MIGRATIONS"
fi

# ⛔⭐⭐ B22/§189 — THE RESTART FENCES ARE REPO ARTEFACTS, AND THE BOX POINTS AT THEM.
#
# Found 2026-08-21: `preflight_v2_restart.sh` — a BLOCKING live-money gate, 263 lines, whose
# printed verdict once read `===> NO-GO` on a Friday deploy — existed ONLY at
# /home/trader/ops_preflight/, root-owned, with FOUR `.bak-*` files beside it from repeated
# hand-editing on the box. Untracked, unreviewed, and one reimage away from gone.
# (`preflight_oms_restart.sh` was already tracked at ops/preflight/ — the reported "repo copy
#  is MISSING" was a wrong path, not a missing file. The v2 fence was the real gap.)
#
# ⛔ A SYMLINK, RE-ESTABLISHED EVERY DEPLOY — not a one-time manual act and not a memory test.
# A hand-edit on the box now lands in the repo checkout, where `git status` shows it and the
# next `git pull` refuses to bury it. That surfacing IS the feature.
# ⛔ The first replacement of a real file keeps a dated copy: this must never be the step that
# destroys the only surviving version of a gate.
PREFLIGHT_SRC="$REPO_DIR/ops/preflight"
PREFLIGHT_DST="/home/trader/ops_preflight"
# ⛔⭐⭐ LINKING MUST NOT ABORT THE DEPLOY. This block sits AFTER `pip install -e` and BEFORE
# the restart, under `set -euo pipefail` — so a failed mkdir/cp/ln left NEW SOURCE ON DISK UNDER
# AN OLD RUNNING PROCESS and no restart. That is the exact state #775 removed for logrotate, and
# it would have been reintroduced here by a different file. Same rule, applied consistently:
# an auxiliary install FAILS LOUDLY, it does not block the application deploy.
# ⚠ The cost of continuing is real — an unlinked fence means the box runs a STALE preflight — so
# the failure is announced with a marker that greps, not a quiet skip.
link_preflight_fences() {
  set +e
  local rc=0
  mkdir -p "$PREFLIGHT_DST" || rc=1
  for src in "$PREFLIGHT_SRC"/*.sh; do
    [[ -e "$src" ]] || continue
    dst="$PREFLIGHT_DST/$(basename "$src")"
    if [[ -f "$dst" && ! -L "$dst" ]]; then
      if ! cmp -s "$src" "$dst"; then
        echo "preflight: $dst differs from the repo copy — preserving it before linking"
      fi
      cp -a "$dst" "$dst.pre-symlink-$(date -u +%Y%m%d%H%M%S)"
    fi
    ln -sfn "$src" "$dst" || rc=1
  done
  if [[ $rc -ne 0 ]]; then
    echo "[PREFLIGHT-LINK-FAILED] could not link fences from $PREFLIGHT_SRC to $PREFLIGHT_DST" >&2
    echo "[PREFLIGHT-LINK-FAILED] the box may be running a STALE preflight gate. The deploy is NOT" >&2
    echo "[PREFLIGHT-LINK-FAILED] blocked by this, on purpose — fix the link before the next restart." >&2
    return 0
  fi
  echo "preflight fences linked from $PREFLIGHT_SRC"
  return 0
}
if [[ -d "$PREFLIGHT_SRC" ]]; then
  link_preflight_fences
  set -e
fi
