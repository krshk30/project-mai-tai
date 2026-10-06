# October 5 install complete; recurring guarded paper active

As of October 5, 21:22 ET, independently read from the box. The service
sequence ran once; the close-out continuation completed at 21:21:20.374 ET.
No rollback, recovery, extra trading-service restart, ledger write or ticket edit.
The separate paper start was authorized by the operator at 21:07 ET.

## Exact application and runners

- Application: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, clean checkout.
- Migration: `20260916_0021 -> 20261005_0022`, two nullable entry-binding columns.
- Original service runner plan: `490e098c4ae987dbfd2a702465e0b3583d2e4c75`.
- Original manifest: `53bc8873ef6f9f13ac40db57c162a047180f64e17e9ae4edd3d6dc52e4f066f2`.
- Original runner log: `/home/trader/after-hours/2026-10-05/owned-entry-rpg-all-on-2045-standing-job/attempt-1730-go/runner.log`.
- Original runner log SHA256: `0c6f6f746e77659d476ad28f8e948598747ed5b0c53ab1a0b38e8add1eb9239d`.
- Close-out plan: `78b213aaa47fdedde14f5271df835ddbc574e83d`.
- Close-out manifest: `828d6fb1ad313055e5d6a76c89c326d8a990d6c55eb5e43908656ce12211c830`.
- Close-out directory: `/home/trader/after-hours/2026-10-05/owned-entry-closeout-2121-job/closeout-2121`.
- Close-out `runner.log` SHA256: `901c22de064743f889191b0e088ac3b097130df3e0c547aae59971f3673e989b`.
- Deployment journal: `/home/trader/fleet_health/deployments-20261005.md`, literal COMPLETE at `2026-10-06T01:21:20.374341+00:00`.

## Identities and proofs

| Service | PID | Start UTC | Result |
| --- | ---: | --- | --- |
| OMS | 362892 | 10-06 00:53:08 | active, NRestarts=0 |
| v2 | 362945 | 10-06 00:53:31 | active, NRestarts=0 |
| strategy | 363061 | 10-06 00:53:53 | active, NRestarts=0 |
| gateway | 2907 | 10-03 22:03:17 | unchanged |
| ORB | 30225 | 10-04 03:13:49 | unchanged |
| ORB-Schwab | 27173 | 10-04 02:53:58 | unchanged |
| daily guard | 366236 | 10-06 01:10:58 | separately authorized start |
| paper | 366242 | 10-06 01:11:01 | separately authorized start |

Both new OMS and v2 show literal `true` for PM_PRINT_ASK_CONFIRM, PM_FLIP_WAIT,
PM_REST_REPRICE and ATR_REPRICE_HANDOFF. Receipt: `proc-four-keys.json`, SHA256
`784baf1a636dd77c16828fe74c2992a9babd82b7136f06475d03db8f037b7916`.

`ticket-dispositions.json`: all 14 recorded tickets proven terminal; four expire
by the window, eight are replacement refusals/accounted terminals, one is the
accounted SCKT fill and one is the explicitly rejected RETO old order. No new
buy orders, open intents or buy fills since the pre-install checkpoint. The
four rewritten Webull references are checked against the exact original open
intent, rejected/skipped-before-submit evidence and zero broker dispatch for
that account/generation; no arbitrary identity difference is admitted.

Actual isolated FLAGGATE: **PASS 147/147**, numeric **8/8**, rc=0, UNKNOWN=0.
Receipt `flaggate.json` SHA256:
`1cd990b2d2baa6154212be6d901be54d2ef9042da6e18b84a7bc71a08b3a0dd3`.
Redis evictions **0 -> 0**; used memory **806,948,736 -> 806,900,528 bytes**.
Five owners plus `_migration_complete=1` retained; union APUS/JAGX/MI/OLOX/VEEA;
paper empty. No bulk snapshot-batches read.

Process downtime was **141.263886 seconds**, after the 20:00 bar-session end.
The read-only query found zero live bars between 20:00 and the new v2 start.
Bar receipt is **NOT_APPLICABLE_OFFHOURS**, not PASS and not a fabricated zero
market-bar hole; next-session delivery remains UNMEASURED. The literal
`[V2-BOOT-HOLD] released` is at `00:59:44.146Z`. Strategy prefilled 120 batches
at `00:55:32.733Z`; scanner validation remains owned by claude-1.
Quoted process lines come from current `oms.log`, `schwab-1m-v2.log` and
`strategy.log` under `/var/log/project-mai-tai/`, after the 10-06 UTC rotation.

## Pre-open and recurring paper

Single actual preopen re-pin: date `2026-10-06`, SHA7823, final identities,
schema0022, active-paper direct PID/start check; routing preserved. Mode0700,
owner trader, `bash -n` passed. SHA256:
`dc334e446471b7919d904d5af60b18015d9214eb4a98f9c55f4d95bf09d774e0`.
Backup: close-out directory `/preopen.sh.before`; diff: `/preopen.diff`.
The official collector excludes paper, so its original ten-service snapshot
and three restarted declarations remain intact; paper is checked separately,
not invented into the historical snapshot.

Named enabled timer: `project-mai-tai-option-a-daily-guard.timer`.
NEXT `2026-10-06 07:40:00 UTC` = **03:40 ET**. Service same basename; failure
service `project-mai-tai-option-a-daily-guard-failure.service` stops paper only.
The old dated 10-05 timer is disabled. User clarified existing bot hours:
detect through09:30, feed through09:40:01; guard ends09:40, then paper closes;
the next weekday03:40 invocation restarts guarded paper. No trading code changed.
Runtime artifacts: `/home/trader/option-a-daily-guard/manifest.json` (seven
hashes checked), unit syntax verified, 60 guard tests passed.

Today's alleged missing sample is proven to be a final-drain race: the original
file has **9600/9600 OK rows**, 11:00:00.480489Z through13:39:59.484776Z, interval
range0.969730..1.031112s, zero kicks. The old guard last counted at13:39:59.000181Z
and did not drain the final fractional-second row after waiting for the sampler.
The isolated guard now drains it; internal missing rows still fail. No count or
load threshold is relaxed. Fifty-four close-out tests passed.

Initial sampler baseline01:11:01.348097Z, first following row01:11:02.348313Z,
baseline/OK respectively, kicks0. At21:26:38ET the measured first15minute
receipt PASSED:936.000158seconds,937rows including baseline, intervals
0.994400..1.005630s, zero kicks, no guard stop, stable guard/paper/gateway PIDs,
Redis806893768B/evictions0, fullfiveowners/healthyunion5/heartbeatage2.810746s.
Load1/5/15=0.855/0.945/1.015; existing warning-only3.5 threshold unchanged.
Exclusive receipt: `/home/trader/after-hours/2026-10-06/option-a-daily/run-20261006T011059600358Z/first-15-minutes.json`.

Mechanics-only stops retained in the journal: separate Redis baseline omitted
from the first close-out copy; official snapshot does not track paper. Both
were fixed/tested without service actions; catalog copies were backed up on
each attempt. Only the final successful attempt wrote preopen.
