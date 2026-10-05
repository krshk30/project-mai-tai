# HEALTHLATCH1 - ops-only install plan

REVIEW REQUIRED. Execute only after the independent exact-head pin, merge,
operator card confirmation and explicit install GO. No trading service restart,
env/flag change, database write, manual cursor/latch edit or new alarm.
The installed checker and cron are the production checkout, not separate copies.

Fill APPROVED_SHA with the verified merge SHA (40 hex) and confirm the box's
HEAD is bbb4360453eb38570a3eb988122d73f97a886a61, clean. The merge alone is not
install approval. Review the inherited N2 shared-cursor risk in REPORT.md;
neither cron caller changes in this install.

## Preflight and advance

As root via the operator's production SSH connection, retain command output
in a new, non-overwriting evidence directory. Record UTC/ET time, current
HEAD, checker/cron/preopen hashes, crontab targets, current latest.txt,
paged.active and socket state, plus all service MainPID/start/NRestarts.
Archive copies for evidence; do not modify the live state file.

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
BOX_SHA=bbb4360453eb38570a3eb988122d73f97a886a61
: "${APPROVED_SHA:?operator-approved merge SHA required}"
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C "$REPO" rev-parse HEAD)" = "$BOX_SHA"
test -z "$(git -C "$REPO" status --porcelain)"
sudo -u trader git -C "$REPO" fetch origin main
git -C "$REPO" merge-base --is-ancestor "$BOX_SHA" "$APPROVED_SHA"
git -C "$REPO" merge-base --is-ancestor "$APPROVED_SHA" origin/main
git -C "$REPO" diff --name-only "$BOX_SHA" "$APPROVED_SHA"
while IFS= read -r path; do
  case "$path" in
    docs/*|tests/*|ops/health/fleet_health_check.py) ;;
    *) printf 'REFUSED disallowed path=%s\n' "$path"; exit 1 ;;
  esac
done < <(git -C "$REPO" diff --name-only "$BOX_SHA" "$APPROVED_SHA")
sudo -u trader git -C "$REPO" merge --ff-only "$APPROVED_SHA"
test "$(git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(git -C "$REPO" status --porcelain)"
```

This is an exact-SHA fast-forward of the detached production checkout, not
an unbounded pull of moving main. If any ancestry, identity or path check
fails, STOP and report; do not force, reset or widen the allowlist.
Only the health checker changes executable behavior. No pip refresh needed.
Compare the installed checker's SHA-256 to the reviewed Git blob and verify
fleet_health_cron.sh, bar_gap_watch_cron.sh and src remain unchanged.

## Proof at the next scheduled five-minute run

Do not invoke the state-writing checker additionally to manufacture green.
Observe the next root cron invocation and retain its new cron.log line and
latest.txt. Require exactly 19 runtime rows or 24 full-mode rows and no
momentum-paper:feed-policy-violation row. SUMMARY GREEN/exit=0 only if all
remaining verdicts are GREEN; otherwise name the genuine remaining verdicts.
Require paged.active to lack the old fingerprint and socket state to lack
momentum_policy_active and the paper log cursor. No page for retiring that
fingerprint; alert.log has no new delivery attributable to the retirement.
Any independent alarm or page is retained, not removed to pass this proof.

The bar-gap reader must still select exactly one v2-bar-continuity row in
full mode. Before 09:35 ET, the fleet cron's live-money checks are NOT RUN,
not proven GREEN. S3 synthetic paging proof is local only, never injected
into a production log or sent to the operator as a real kick.

## Single preopen re-pin and completion

Back up /home/trader/preopen.sh, record the before hash and diff. Re-pin the
date to 2026-10-06 and checkout SHA to APPROVED_SHA only, preserving the
freshly verified unchanged process PIDs/starts/NRestarts, checker/catalog
pins, routing and mode 0700. Use the established reviewed preopen template;
bash -n and review the diff, then record the after hash. Do not run a
date-fixed October 6 gate early or change it to force green.

Journal /home/trader/fleet_health/deployments-20261005.md with exact SHA,
backup paths/hashes, checker blob/file hash, all untouched identities,
next-cron proof, state cleanup, actual summary/exit, alert-log disposition,
preopen backup/diff/hash and the known N2 limitation. Report completion or
the exact blocker; update only Codex C rows in shared handoff #1089.
