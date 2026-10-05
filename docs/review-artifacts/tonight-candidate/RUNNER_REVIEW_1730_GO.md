# 17:30 GO: literal runner review and read-only STOP

## Current Decision

STOP / UNKNOWN at17:39:49 ET on2026-10-05, BEFORE any box write. No install,
migration, service action, token refresh, remote staging, approval file or
timer was created. No retry or recovery was attempted. The operator's GO
names application`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf` and reviewed
plan`690775af2f31e0331430e37025598053b4d51df6`; the literal runner below
is NEW, LOCAL DRAFT material for review, not a substituted approved plan.

Read-only census raw source:
`/tmp/own-census-reviewed-20261005-r4.json`, completed
`2026-10-05T21:39:49.937146+00:00`. Reviewed immutable identities14/14,
phases5held_unknown/8refused/1filled, DB nonterminal orders0,
schema`20260916_0021`. Eight of11 exact linked broker-parent reads established
terminal states, then Webull raised `ServerException`; remaining three
are UNMEASURED. Last successful Webull exact read was cancelled SCKT parent
`schwab_1m_v2-SCKT-open-074e9d445a42`. This does NOT prove all broker working
orders absent: Schwab's OMS-sync 12-hour history read is not all-time orphan
coverage. The census does not clear the install fence.

STOP page accepted, ntfy id`nCuuMym1iFSP`, Unix timestamp1791236467.
It states no writes and missing proof, not a confirmed open buy.

Separate17:38:17 ET bounded Redis read: evicted_keys0,
used_memory808916112bytes, allnine expected streams present. Owner hash:
strategy-engine/schwab-1m-v2=[APUS,JAGX,MI,OLOX,VEEA],
orb/orb-schwab=[APUS,MI,VEEA], momentum-paper=[], migration_complete1.
This point-in-time read is not a post-install or broker proof.

## Literal Files And Abort Policy

`job/run.sh` contains every literal call. `job/actions.py` contains approval,
hash checks, backups, exactly three env edits, exact0022 migration, isolated
catalog installation, real-identity preopen candidate application and journal
and page calls. `job/census_readonly.py`, `job/redis_checkpoint.py`,
`job/proof.py` and the unchanged accepted `strict_flat_readonly.py` implement
the gates. `job/make_release.py --plan <40-hex-commit>` prints a manifest
from committed blobs only; it does not stage or approve anything.

Exact application sequence, if a reviewed continuation is later authorized:

```sh
systemctl stop project-mai-tai-schwab-1m-v2.service
systemctl stop project-mai-tai-strategy.service
systemctl stop project-mai-tai-oms.service
# actions.py requires base20260916_0021 and the pinned migration file hash:
runuser -u trader -- /home/trader/project-mai-tai/.venv/bin/alembic \
  -c /home/trader/project-mai-tai/alembic.ini upgrade 20261005_0022
systemctl start project-mai-tai-oms.service
systemctl start project-mai-tai-schwab-1m-v2.service
systemctl start project-mai-tai-strategy.service
```

Each stop/start has a fresh flat read and Redis comparison immediately before
it; v2's unmodified gate is immediately before its stop. No other service
call exists. The EXIT trap journals/pages actual states and does NOT restart
strategy or perform any recovery: the latest GO pre-authorizes none.
No saved startup BUY is permitted; source and all-date census are fenced.
Post-start logs use offsets captured AFTER the old processes stopped,
and the sync lower bound is microsecond UTC AFTER the OMS start returns.
Warm-up reads recheck bounded Redis metadata every5seconds; no snapshot
payload is read. Bar continuity is a bounded DB proof, UNKNOWN stops rather
than inventing missing-minute values. All executed lines would carry source
paths/time in the attempt receipt;20:00 log rotation has NOT occurred yet.

## Open Review Decisions

1. The Webull read UNKNOWN is the active hard stop. A fresh authorized
   continuation and successful broker census are needed; elapsed time/18:00
   does not clear it. No new run has been scheduled.
2. Paper is truly inactive/PID0. The current preopen identity function demands
   active/running. Pinning its real PID0 while preserving routing therefore
   remains a REAL FAILURE on that identity check, distinct from the accepted
   two FLAGGATE UNKNOWN rows. No routing bypass or paper restart is proposed.
   The draft requires an explicit reviewed disposition before approval; the
   new approval field is NOT authorized by the17:30 paste.
3. All new runner/helper interfaces and migration/close-out steps are LOCAL
   validation only, UNEXERCISED on the box. The final census helper includes
   additional identity/failure-receipt validation after the failed live read;
   those exact final bytes have NOT been rerun live. No whole-run PASS.
4. Post20:00 rotated-file rereads and actual bar-hole minutes remain pending
   execution evidence, not COMPLETE. Do not rerun the date-fixed tomorrow gate
   early. Scanner validation remains reviewer-owned.

## Local Validation

Parent action/runner unit tests:11PASS. Bash syntax and Ruff checked locally.
These pin literal service sequence, no recovery/override, env duplicates,
three edits only, exact migration and approval/hash refusal. They do not
prove migration or install safety on production. Census fixture controls:
14PASS, ten identity mutations STOP (worker result); not a live re-run.
Additional proof-helper local results are reported separately with their
exact bytes; do not infer a full production proof from these offline tests.

Proof worker froze final SHA256
`a28c134e95855f215ee7f050f101aa0bfc720880b38db63786b34da5959104ff`:
15 offline refusal controls passed, positive parser/checkpoint/loop controls
passed, simulated preopen candidate bash syntax passed. Parent independently
checked final hash, actual-key gate parsing and CLI lower-bound/Redis/bar-hole
hooks;11 action tests and whole job-directory Ruff passed again. No additional
box run occurred. Final census SHA256
`df233d02d527eda7a360f52f896fff4715f26783384c638f72682f582d7c4994`
is not the exact bytes used for the failed live receipt.
