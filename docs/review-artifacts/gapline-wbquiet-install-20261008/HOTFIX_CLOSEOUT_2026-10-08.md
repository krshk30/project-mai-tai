# PG hotfix applied; post-proof FAILED; bookkeeping not yet applied

Application 06b5e388affb5f33e4edf80c6e635cf72dbc3f78 is installed. Actual
runner plan 10cbee223b685e6b908bd6e0ed5378d79b66cffc, manifest
fa606690a63575da03d3ddf7c388258d5a852856e728d916fca35f117b12c802.
Job: /home/trader/after-hours/2026-10-08/falseflip-pg-hotfix-r3/job.
Attempt: attempt-20261008T211500954696Z.

First write 21:15:39.832906 UTC (authorized catalog namespace only).
OMS 2094823 and strategy 2094834 started 21:16:04 UTC; v2 2096131
started 21:18:16 UTC. All active/running, NRestarts 0. Control 2075090 and
all other app services untouched. Schema 20261008_0023 checked read-only;
no migration or environment write. Prior timers disabled, one installer path.

ABORT sealed 21:28:36.790824 UTC, stage ten-minute-observation, rc 1.
Actual failure: post_start_traceback_or_error:schwab-1m-v2 (four historical
closed-fill line_unproven ERRORs). OMS and strategy ERROR/traceback counts 0.
No COMPLETE and no recovery/restart/rollback after the seal.

Runner log SHA256:
50e015024767114d591e209ed55a2c88d328b9eec4588382ef9205a7d254cddc.
Failed proof SHA256:
ffe6b97ee86e927556e28deba5c77e99680a526409825adfd0350fd4e860b6ad.
ABORT SHA256:
6628baf0e018b0b2fc9a7a71cb440df32327b9a80867f8eed88d3837e1cb2b3d.
Original application-install ABORT and first hotfix prewrite ABORT also remain
unchanged. This report does not replace any sealed receipt.

## Actual observations

At 21:26:49 UTC: classification_unreadable 0; 20 new closed-bar fact rows
since OMS start (AIXI 10, FLYE 10), 16 after v2 start. Latest bar epoch
1791494700000. Actual deployed-store read-only restore counts before and
after both 0. Direct process-memory boot count UNMEASURED: no count marker.
Archived rows untouched. Live flag audit rc 2, checked 153/155, mismatches 0,
two named inactive momentum-paper UNKNOWNs. No false all-green receipt.

Final whole-database rate 18.0859534877 tx/s over 795.645085 s, including
collector traffic, not OMS-only attribution. Fifty OMS sync passes complete,
all outcomes ok; p95 772.594 ms, max 1075.423 ms. Redis evicted_keys 0;
active owner union AIXI/FLYE, other owners preserved. Morning scanner acceptance
remains unmeasured; no production scanner certification from log keywords.

## Read-only precision correction

Sealed proof had bar continuity UNKNOWN: rounded systemd start 21:18:16
preceded actual Stopping timestamp 21:18:16.443700. The local read-only
collector now requests systemctl --timestamp=us. Actual process start is
21:18:16.959983 UTC. Same-second and truly inverted/out-of-attempt controls
tested; full mechanics 306 passed, seven platform skips.

Read-only rerun 21:32:03 UTC: bar continuity MEASURED, no missing persisted
minutes for AIXI/FLYE; no UNKNOWN. Verdict still FAIL, solely the four v2
ERRORs. No application action or sealed-artifact change.
Local receipt /tmp/oct8-pg-hotfix-corrected-readonly-proof.json SHA256
e9920312f1dc91f70a513b8a1b4dfd555f7197ebc52b87fea7e55c308bad5f15.

## Separate Bookkeeping Closure

The first in-memory preview did not verify the stale schema expectation; it is
superseded, not erased. The schema-aware preview at 21:59:46 UTC verifies actual
0023 and the original successful migration event from the exact immutable
runner-log hash. Fresh direct trading reads rc 0: both brokers flat, no working
orders, managed/virtual rows or in-flight intents. Exact installed PIDs and all
untouched identities match. Receipt:
/tmp/oct8-schema-aware-bookkeeping-preview.stdout (raw gate and preview).
Preview is CHECKS_NOT_RUN, not the daily gate or global installation PASS.
The old preopen still names 0022 and --no-schema-change; the new helper updates
0023/schema-column and removes that inaccurate declaration without migrating.

Parent authorized this narrow closure under standing mechanics authority. Publish
and separately stage only the closure helper and its dependencies, verify every
hash, then run bookkeeping_only.py --apply under the existing nonblocking deploy
and daily locks. It re-reads trading conditions, checks exact installed identities,
combines the original snapshot/control restart and actual r3 OMS/v2/strategy
events with bookkeeping.combined_record, and exclusively creates
/home/trader/restart_evidence/hotfix-bookkeeping-06b5e388/original-snapshot.json,
install-record.json and sealed-actions.json. Journal retains both actual ABORT
hashes, original/current failed-proof hashes and FAIL verdict; no COMPLETE.
The sealed r3 helper is NOT modified or rerun. The separately staged schema-aware
closure calls the tested repin functions. Literal commands after hash verification:

```sh
sudo -n env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/trader/project-mai-tai/src \
  /home/trader/project-mai-tai/.venv/bin/python \
  /home/trader/after-hours/2026-10-08/preopen-bookkeeping-06b5e388/bookkeeping_only.py --apply
sudo -n bash -n /home/trader/preopen.sh
sudo -n env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/trader/project-mai-tai/src \
  /home/trader/project-mai-tai/.venv/bin/python -c \
  'import sys; sys.path.insert(0,"/home/trader/preopen-daily"); import daily; print(daily.verify_runtime())'
sudo -n env PYTHONDONTWRITEBYTECODE=1 bash /home/trader/preopen.sh
```

Declared repin writes only: /home/trader/preopen.sh and preopen-daily binding.json,
restart_report.py, runtime.json, upgrade-ack.json, upgrade_ack.py, with atomic
backups/hashes. Exact unchanged orb-schwab upgrade acknowledgement, legacy ORB
identity, dynamic date/paper shape and adapter/root mode preserved. Official
snapshot/action inputs describe actual four-service changes across both installs;
this is not a second control restart. Receipt remains REPINNED_CHECKS_NOT_RUN.
The official checks-only run can write its new dated report; it never overwrites
the sealed install evidence. Its after-hours/paper/legacy errors remain raw FAIL
or UNKNOWN, not waived. No env, catalog, source, DB/Redis, service, timer, schema
or archived-row action.
Any identity/artifact/flag discrepancy refuses rather than adopting a new state.

At this report, preopen.sh SHA256 remains
dc64d80a54c9127b74025873455d7ac3d41fc9f985db932afddfe45e5711d73f;
no r3 preopen-repin receipt exists. Bookkeeping is honestly incomplete.
