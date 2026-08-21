#!/usr/bin/env bash
# KNOWN-BAD TAPE for the B22/§189 preflight-linking step in 08_install_runtime.sh.
# Runs entirely in a temp dir. Read-only w.r.t. production — touches nothing under
# /home/trader or /etc. Usage:  bash ops/preflight/test_preflight_link_step.sh
#
# ⛔ A deploy step that has only ever been reasoned about is not a deploy step you can trust
# with the only copy of a live-money gate. The case that matters is the THIRD one below: the
# box copy differs from the repo copy, and the step must preserve it rather than clobber it.
set -u
PASS=0
FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

# ⛔ 127 does not trip `set -u`; without this a typo'd helper reads as a silent pass.
command_not_found_handle() { bad "command not found: $1"; return 127; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
REPO_DIR="$ROOT/repo"
PREFLIGHT_SRC="$REPO_DIR/ops/preflight"
PREFLIGHT_DST="$ROOT/ops_preflight"
mkdir -p "$PREFLIGHT_SRC"
printf 'repo version\n' > "$PREFLIGHT_SRC/preflight_v2_restart.sh"
printf 'repo version oms\n' > "$PREFLIGHT_SRC/preflight_oms_restart.sh"

# the step, verbatim in behaviour with the one in 08_install_runtime.sh
link_step() {
  if [[ -d "$PREFLIGHT_SRC" ]]; then
    mkdir -p "$PREFLIGHT_DST"
    for src in "$PREFLIGHT_SRC"/*.sh; do
      [[ -e "$src" ]] || continue
      dst="$PREFLIGHT_DST/$(basename "$src")"
      if [[ -f "$dst" && ! -L "$dst" ]]; then
        if ! cmp -s "$src" "$dst"; then
          echo "     (step: $dst differs — preserving)"
        fi
        cp -a "$dst" "$dst.pre-symlink-$(date -u +%Y%m%d%H%M%S)"
      fi
      ln -sfn "$src" "$dst"
    done
  fi
}

echo "CASE 1 — destination does not exist yet"
link_step
[[ -L "$PREFLIGHT_DST/preflight_v2_restart.sh" ]] && ok "created a symlink" || bad "did not create a symlink"
[[ "$(cat "$PREFLIGHT_DST/preflight_v2_restart.sh")" == "repo version" ]] && ok "resolves to the repo copy" || bad "resolves elsewhere"

echo "CASE 2 — idempotent: re-running over an existing symlink makes no backups"
link_step
n=$(find "$PREFLIGHT_DST" -name '*.pre-symlink-*' | wc -l)
[[ "$n" -eq 0 ]] && ok "no backup churn on re-run ($n)" || bad "created $n backup(s) on a no-op re-run"

echo "CASE 3 ★ — a DIFFERING hand-edited box copy must be PRESERVED, never clobbered"
rm -f "$PREFLIGHT_DST/preflight_v2_restart.sh"
printf 'HAND EDITED ON THE BOX\n' > "$PREFLIGHT_DST/preflight_v2_restart.sh"
link_step
kept=$(find "$PREFLIGHT_DST" -name 'preflight_v2_restart.sh.pre-symlink-*' | head -1)
if [[ -n "$kept" ]] && grep -q 'HAND EDITED ON THE BOX' "$kept"; then
  ok "the hand-edited copy survived at $(basename "$kept")"
else
  bad "the hand-edited copy was DESTROYED — the step ate the only version"
fi
[[ "$(cat "$PREFLIGHT_DST/preflight_v2_restart.sh")" == "repo version" ]] && ok "and the link now points at the repo" || bad "link does not point at the repo"

echo "CASE 4 — a repo dir with no .sh files must not create a literal '*.sh' link"
rm -rf "$PREFLIGHT_SRC"/*.sh
link_step
[[ ! -e "$PREFLIGHT_DST/*.sh" ]] && ok "no glob-literal link created" || bad "created a literal '*.sh' entry"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
