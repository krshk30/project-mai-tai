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

# ⛔⭐⭐ THE TEST USED TO CARRY ITS OWN HAND-COPY OF THE STEP, and that is why it passed 6/6
# while the production defect was live: the copy had drifted (no `rc`, no refuse-on-failed-backup),
# so the suite was grading the TEST AUTHOR'S ASSUMPTIONS, not the deployed code. A fixture that
# differs from production proves nothing. ⇒ Extract the REAL function from the REAL file on disk
# and exercise THAT. If extraction fails, the run is VOID — never a pass.
SUBJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/ops/bootstrap/08_install_runtime.sh"
[[ -r "$SUBJECT" ]] || { echo "⛔ VOID — cannot read $SUBJECT; the step was NOT tested."; exit 3; }
FN=$(awk '/^link_preflight_fences\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "$SUBJECT")
[[ -n "$FN" ]] && grep -q 'PREFLIGHT-LINK-FAILED' <<< "$FN" || {
  echo "⛔ VOID — could not extract link_preflight_fences() from $SUBJECT (renamed?); NOT tested."; exit 3; }
eval "$FN"
echo "SUBJECT: $SUBJECT"
echo "         md5=$(md5sum "$SUBJECT" | awk '{print $1}')  function lines=$(wc -l <<< "$FN")"
# the production function is guarded by `if [[ -d "$PREFLIGHT_SRC" ]]` at its call site; mirror
# only that, so everything inside the function under test is the deployed code itself.
link_step() { if [[ -d "$PREFLIGHT_SRC" ]]; then link_preflight_fences; fi; }

# ⛔⭐⭐ PRECONDITION, NOT A CASE. The whole step is about symlinks, so on a filesystem that
# cannot make one (Git Bash without winsymlinks, some CIFS mounts) EVERY assertion below fails
# for a reason that has nothing to do with the code. That is a VOID, and it must not be allowed
# to print as FAIL=3 — a failing control voids the probe, it does not become a negative result.
if ! ( ln -sfn "$PREFLIGHT_SRC/preflight_v2_restart.sh" "$ROOT/.symprobe" 2>/dev/null && [[ -L "$ROOT/.symprobe" ]] ); then
  echo "⛔ VOID — this filesystem does not create symlinks; the link step was NOT graded here."
  echo "         Run it on the target Linux box. Reporting VOID(3), never a pass and never a fail."
  exit 3
fi
rm -f "$ROOT/.symprobe"

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

echo "CASE 5 ★★ — if the BACKUP fails, the box copy must SURVIVE (no symlink replacement)"
# ⛔⭐⭐ THE §189 DEFECT ITSELF. `cp -a` was unchecked and errexit was off, so a failed backup fell
# through to `ln -sfn` and the only surviving copy of a live-money gate was replaced by a symlink
# — while the function returned 0 and printed "preflight fences linked". This control forces the
# backup to fail with a PATH shim and asserts the original is still there, byte-for-byte.
# CASE 4 deleted the repo sources, and without restoring them the `for src in .../*.sh` loop
# never runs -- so the file would sit untouched and this control would PASS FOR THE WRONG
# REASON. A fault control that cannot fire is the exact defect this suite exists to catch, so
# the source is restored and the firing is PROVEN below, not assumed.
printf 'repo version
' > "$PREFLIGHT_SRC/preflight_v2_restart.sh"
rm -f "$PREFLIGHT_DST"/preflight_v2_restart.sh*
printf 'ONLY COPY ON THE BOX
' > "$PREFLIGHT_DST/preflight_v2_restart.sh"
before=$(md5sum "$PREFLIGHT_DST/preflight_v2_restart.sh" | awk '{print $1}')
SHIM=$(mktemp -d); printf '#!/bin/sh
exit 1
' > "$SHIM/cp"; chmod +x "$SHIM/cp"
# ⛔ `hash -r`: bash caches command paths, and CASE 3 already ran the real /usr/bin/cp -- without
# flushing, the shim is bypassed and the forced failure never happens.
out5=$(PATH="$SHIM:$PATH"; hash -r; link_step 2>&1)
hash -r
rm -rf "$SHIM"

# POSITIVE CONTROL FOR THE CONTROL: prove the shim actually made a `cp` fail. If this reads
# clear, everything asserted below is vacuous -- the step never reached the backup at all.
probe=$(mktemp -d); printf '#!/bin/sh
exit 1
' > "$probe/cp"; chmod +x "$probe/cp"
if ( PATH="$probe:$PATH"; hash -r; cp /dev/null /dev/null 2>/dev/null ); then
  bad "⛔ the cp shim did NOT take effect -- CASE 5 is vacuous, not passing"
else
  ok "the forced-failure shim is in effect (the control can fire)"
fi
rm -rf "$probe"
if [[ -L "$PREFLIGHT_DST/preflight_v2_restart.sh" ]]; then
  bad "⛔ the box copy was REPLACED BY A SYMLINK after the backup failed — the §189 defect is live"
elif [[ "$(md5sum "$PREFLIGHT_DST/preflight_v2_restart.sh" | awk '{print $1}')" == "$before" ]]; then
  ok "the only copy survived untouched after a failed backup"
else
  bad "the file changed after a failed backup"
fi
grep -q 'PREFLIGHT-LINK-FAILED' <<< "$out5" && ok "and it says PREFLIGHT-LINK-FAILED out loud"   || bad "the refusal was SILENT: $out5"
grep -q 'REFUSING to replace' <<< "$out5" && ok "naming the refusal explicitly" || bad "no explicit refusal"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
