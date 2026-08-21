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
if [[ -d "$PREFLIGHT_SRC" ]]; then
  mkdir -p "$PREFLIGHT_DST"
  for src in "$PREFLIGHT_SRC"/*.sh; do
    [[ -e "$src" ]] || continue
    dst="$PREFLIGHT_DST/$(basename "$src")"
    if [[ -f "$dst" && ! -L "$dst" ]]; then
      if ! cmp -s "$src" "$dst"; then
        echo "preflight: $dst differs from the repo copy — preserving it before linking"
      fi
      cp -a "$dst" "$dst.pre-symlink-$(date -u +%Y%m%d%H%M%S)"
    fi
    ln -sfn "$src" "$dst"
  done
  echo "preflight fences linked from $PREFLIGHT_SRC"
fi
