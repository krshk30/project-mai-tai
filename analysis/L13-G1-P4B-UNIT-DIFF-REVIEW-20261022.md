# L13 G1 P4(b): Exact Unit Review / Two Dispositions Needed

SOURCE ONLY. Isolated codex/l13-p4-unit-review-20261022 from frozen
f76a3f23d22490a0f19eb34504e73c46d0ac1409; old worker/PRICE/control worktrees
untouched. No systemd executor, install/reload/restart/kill/test/import/provider.
Maxwell owns his control files; NONE are edited here. Main owns composition,
combined install runbook, execution and reviewer disposition.

## Already Present in Exact f76 Unit / This Diff

ops/systemd/project-mai-tai-snapshot-worker.service already has:

```ini
After=network-online.target redis-server.service project-mai-tai-market-data.service
Wants=network-online.target
PartOf=project-mai-tai.target
StartLimitIntervalSec=0
Restart=always
RestartSec=5
TimeoutStopSec=30
```

This successor changes FOUR COMMENT LINES ONLY in that unit to document the
reviewed independent-lifetime and momentum-only guard boundaries. No executable
directive, application source, worker API, unit ordering, guard, table, flag or
control file changes. No fresh heartbeat identity or runtime qualification is
claimed from comments. Full f76->HEAD diff is the reviewable unit diff plus this
handoff; main can send the exact unit, not an invented missing Restart fix.

## Material Reviewer Decision 1: PartOf Gateway Conflicts With Prior Independence

16:31:39 asks After/PartOf ordering against market-data. After gateway is present.
Both units are PartOf project-mai-tai.target, NOT PartOf one another. Original
G1 handoff explicitly removed gateway Requires/BindsTo/PartOf coupling so a
gateway restart leaves the independent worker alive; main's controlled P4(a)
51b196bf receipt specifically checks one worker across two gateway lifetimes.

Adding this line literally is NOT a harmless ordering clarification:

```diff
-PartOf=project-mai-tai.target
+PartOf=project-mai-tai.target project-mai-tai-market-data.service
```

It propagates gateway stop/restart jobs to the worker, changing the exact prior
contract. It does not prove automatic crash/restart timing, nor make gateway
readiness await worker health. I DID NOT silently apply it or claim its absence
meets the literal new requirement. Reviewer must choose explicitly:

1. Keep After gateway + shared-target PartOf, preserving independent lifetimes.
2. Require gateway PartOf propagation and explicitly supersede the prior
   gateway-restart independence requirement, with corresponding control review.

Worker crash/restart remains independent of gateway in either direction only
under option1's full original lifetime contract. Restart=always/Sec5 already
provide the reviewed configured worker retry policy, not an observed systemd
restart-latency receipt. No production kill or new systemd executor proposed.

## Material Reviewer Decision 2: STOP Is Momentum-Scoped, Not a Global Data Stop

Exact existing source anchors:

- guard_rules.py:76-89 (ops/health/momwarn1_repair/optiona/G1): durable
  /var/lib/project-mai-tai/option-a/safety-stop.json. lstat presence OR unknown
  lookup -> Blind; only FileNotFoundError permits. No clear/removal added.
- daily_guard.py:335-339 checks that latch and returns safety exit78. Its
  --paper-prestart path also checks session/manifest/gateway/Redis/current guard
  identity and may retain the durable STOP (lines345-375).
- project-mai-tai-momentum-paper.service: ExecStartPre=+...daily_guard.py
  --paper-prestart; RestartPreventExitStatus=78. Guard unit retains
  ExecCondition, ExecStartPost start-paper, ExecStopPost stop-paper and exit78.
- project-mai-tai-market-data.service has NO such prestart/STOP path. Snapshot
  worker likewise has none and is a shared source for scanner/ATR/ORB, not a
  momentum execution consumer. Snapshot data does not clear a consumer STOP.

Blindly copying --paper-prestart onto the worker imports momentum-session,
manifest, Redis and paper-guard identity dependencies into shared market data.
Blindly adding a STOP-file condition can block worker recovery because of a
momentum STOP and starve other consumers; an exists-only condition also does
not reproduce the guard's unreadable/dangling-symlink fail-closed semantics.
No such behavior change is inferred from 'same STOP-file/guard behavior'.

Reviewer must confirm preserving EXISTING consumer/guard STOP handling while
the shared source stays available satisfies P4(b), OR explicitly authorize a
new shared-source STOP scope with exact affected consumers/recovery contract.
This source assessment is not permission to waive or bypass an existing latch.

## Impact / Remaining Evidence

No changed functions/callers/shared state/tables/flags; only unit comments and
this report. Worker entry remains services.market_data_snapshot_worker;
actual runtime identity service=market-data-snapshot-worker, snapshot stream
source_service=market-data-gateway unchanged. One REST client/process and one
gateway WebSocket unchanged. Existing P2 bytes/control selection unaffected.
No controls added/edited in Maxwell's lane; no PASS borrowed from another head.

P4(b) literal PartOf/STOP-scope disposition remains OPEN, not source-CLEAR/GO.
Main's existing P4(a) controlled receipt and P4(c) reviewer post-install read
are separate: active/NRestarts0, old batch cadence within2min, gateway lagp99
<300ms, actual unit/source hashes/PIDs. ALL-ON P5 remains owed.

PRICE census declaration note for main only: prior.66 BOTH days in the original
receipt is a historical caller declaration, not an actual exported prior map.
16:31:39 now says10-22 prior>=1. Preserve the immutable original receipt; do not
relabel it as an actual prior map or overwrite it. Main owns the accepted
UNKNOWN-counterfactual disposition and P3 ALL-ON gate. PRICE source untouched.
