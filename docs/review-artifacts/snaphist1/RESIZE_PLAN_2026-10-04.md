# Sunday resize: one cold boot, SNAPHIST1, logging and full owner persistence

Status: STAGING AUTHORIZED 10-03 15:40 ET; execution still requires claude-1's
exact staged-hash approval Sunday morning. No approval file is written by Codex.
The review dispositions below replace the two former draft blockers. Fresh
flat/readability/idle checks still run at preparation; a new ambiguity stops it.
This replaces the standalone gateway plan at 6b3aae95. The operator performs
the provider resize/reboot at 10:00 ET Sunday 2026-10-04, only after the runner
prints PREPARED. If preparation refuses, DO NOT resize/reboot. No permission
carries to another date. RPG1 is excluded and remains a separate build/install.

The operator authorized staging the bounded owner replay and offline RDB archival.
Neither production write is authorized before Sunday's exact plan approval.
Cold-boot execution, owner migration, replay writes, recovery and new boot links
are UNEXERCISED, not PASS. Staging/timer installation is not execution.

## 1. Exact scope and release

- Clean starting box/main: `608339894a1cfb33284e695196df55c18f312889`.
- PR #1087 pinned head: `bc59b220656b2ff24c4a557f4aa7b864334d4b14`.
- Reviewed target tree: `fde43f820650e1237d860b84a6cde9c5166faa96`.
- The sole merge is rebase-merge #1087 inside approved Sunday preparation.
  Recheck independent pin plus two Validate passes; changed head/base requires
  re-review. Resulting SHA is recorded in `merge.json`; exactly one commit from
  the base, exact tree above. No moving-main substitution or Update branch.
- Env addition only: `MAI_TAI_REDIS_SNAPSHOT_BATCH_STREAM_MAXLEN=120`, duplicate
  definitions refused. Dollar sizing, NFQ1, ORB and exit flags unchanged.
- Checkout/runtime refreshed once; editable install as trader, env readers root.
- Reboot loads #1077 timestamped gateway/orb-schwab logging and #1084 full owner
  persistence. Narrowed detectors must be installed/hash-checked beforehand.
- Resize target: 8 CPUs and at least 15,000,000 KiB MemTotal (16 GB offering).
  Redis maxmemory remains 2 GiB, allkeys-lru; safety margin remains 1.6 GB.
- No database edits, migrations, synthetic market events, order placements,
  manual cancellations, guard start, provider API call, automatic reboot or
  application rollback. Flatness and arm gates cannot be overridden here.

Reviewed artifacts live in `resize_job/`: `run_resize.sh`, `resize.py`,
`review_gate.py`, `merge_in_window.sh`, two services and one timer.
`stage_review.sh` builds `release.json` from committed Git blobs. It records
the plan commit, exact application bindings and SHA256 of eight deployed files
(including this plan). Approval must exactly equal the release plus the listed
cold-boot authorizations in `review_gate.py`, reviewer=claude-1,
decision=APPROVED. Root-only JSON, non-writable/root-owned artifacts, hashes of
installed units, date and app bindings are checked before every invocation.
No script generates its own approval. Approval also records the 15:40 operator
disposition: no manual stops or hand orders to preserve; archive retained intent
history without replay; list unknown broker orders as pre-existing orphans,
non-blocking and never cancelled by this plan.

Paths:

| Purpose | Absolute path |
| --- | --- |
| Immutable staged release | `/home/trader/after-hours/2026-10-04/resize-job/` |
| Exclusive attempt and raw evidence | `/home/trader/after-hours/2026-10-04/resize-run/` |
| Fleet journal | `/home/trader/fleet_health/deployments-20261004.md` |
| Preparation unit | `project-mai-tai-resize-prepare-20261004.service` |
| Approval-gated timer | `project-mai-tai-resize-prepare-20261004.timer` |
| One cold-boot bootstrap | `project-mai-tai-resize-postboot-20261004.service` |

Preparation timer runs each minute 09:30-09:40 ET, but no approval means no
execution. The first attempt creates the exclusive directory and disables the
timer. No automatic attempt two. systemd calendar/units were syntax-verified
read-only on the box: NEXT Sunday 13:30 UTC, 09:30 ET. The staging receipt must
show the installed timer's NEXT value; this text alone is not a schedule proof.

## 2. Independent findings and Redis state audit

Read-only box/code audit 10-03: current Redis INFO reports persistence OFF
(`save=""`, `appendonly=no`), evicted_keys=23, used_memory about 1.176 GB.
But `/var/lib/redis/dump.rdb` exists: 189,948,710 bytes, Apr 1 timestamp,
checksum valid, five keys. Runtime last-save metadata is NOT proof of the disk
file's age/content. Persistence OFF prevents new snapshots, not loading this
old file. A reboot without treatment could restore stale state, not empty Redis.

The app target has no normal-boot WantedBy link. Orb-schwab is not a member of
that target either. Therefore "enabled units all return" is not a valid plan.
The proposed bootstrap is the first-boot owner. It temporarily sets Restart=no
for ten app units plus Redis/Postgres so automatic retries cannot hide failure.
On successful close-out only, it removes its exact drop-ins, registers the app
target under multi-user.target and orb-schwab under the app target, and disables
the one-shot postboot unit. These boot-policy edits are explicit review scope.

| Service / state | Durable authority and cold rebuild | Gate / treatment |
| --- | --- | --- |
| Gateway: subscriptions/owner hash, quotes/trades, heartbeat, snapshot-batches | Owner hash and streams are Redis-only; #1084 reconstructs from retained replace events, but reboot removes that stream too. Quotes/snapshots are reacquired from provider. | Preserve/replay five raw replaces; gateway itself rebuilds hash and marker. No direct HSET. Fresh heartbeat and cadence required; ticks Monday. |
| Strategy: alert history, strategy-state, subscription debounce | DB scanner/watchlist snapshots and bar/history data survive. In-memory alert window and recent Redis snapshots do not. | Empty boot prefill is expected; live history accumulates. Five-/ten-minute readiness and scanner validation Monday, not invented from weekend zeroes. |
| v2: watch/flip/retry/armed state, drafted intents | DB watchlist/strategy state and durable budgets survive. In-memory drafts and Redis intent transport do not. | Zero armed segments, no active/draft work; restart gate blocks. BOOT-HOLD literal population/release recorded, Monday verification assigned. |
| OMS: held positions, orders, NFQ holds, pending closes | Managed rows, orders, fills and NFQ dashboard rows survive in Postgres. Serial intent cursor/queues, retry/liveness caches and pacing latches do not. Ordinary broker submit can precede DB commit. | Direct brokers flat AND zero known working orders, zero DB pending orders/holds/outbox. Supplemental broker discoveries outside our records are listed as pre-existing orphans, non-blocking per review. Unreadable broker data remains STOP. |
| ORB / orb-schwab | Paper decisions/positions and managed broker orders are DB-backed; minute aggregators, pending callbacks and entry drafts are process memory. | Flat, no live order, no unresolved intent. New process IDs recorded; Sunday is not execution evidence. |
| Momentum paper | Paper store/candidate/trade records in Postgres; stream offset/window in memory. Sunday `_tick` immediately takes weekend stop path. | Starts Sunday but does not prepare/claim symbols/stream; owner set must remain empty. Monday 03:55 prepare/04:00 streaming needs guard at 03:40. |
| Control | DB/control settings and dashboard snapshots; runtime-controls transport is volatile. | Inventory unknown Redis state; do not rely on old transient commands as a maintenance lock. |
| Global/per-bot manual stops | DB snapshots exist but strategy startup deletes prior-session stops. OMS only has global stop handling and a volatile last-good cache. | Operator 10-03 15:40 ET: "No" manual stops or hand orders to preserve. Record "operator: none". A later contradictory instruction/state must be escalated, not silently covered by this disposition. |
| Market capture | Captured trades/quotes in Postgres; live subscriptions/cursor reconnect. | No backfill of lost Redis ticks claimed; Monday delivery proof. |
| Reconciler | Postgres/broker truth and incident rows persist; transient last-good caches lost. | Direct flat/readability; unresolved active order/position work blocks. Existing incident records preserved, never auto-resolved by this plan. |
| tv-alerts | Intentionally inactive; retained journals/DB untouched. | Must stay PID 0, explicitly classified deliberately untouched. |
| Redis `symbol-block:*`, unknown keys | Some intraday rejection blocks exist only in Redis. | Unknown key inventory, including any current symbol-block, STOP. No assumption that a block will rebuild. |
| Redis strategy-intents/order-events | Transport is not an authoritative durable queue with restart ACKs. OMS begins at `$`; confirmation outbox does not replay everything historically published. | Archive every retained intent's stream ID/raw fields, including special exit envelopes. Do not call these historical entries lost or pretend to acknowledge them. Review accepts weekend idle gates + producer stop +20s stable tail +fresh DB/broker flat proofs. New pending work or unreadable checks STOP. No historical buy replay. |

Four publishers suppress their initial empty replace; only v2 reliably sends
an initial empty set. Sunday paper also does not send one. Thus all-five-owner
recovery from a genuinely empty stream cannot happen unaided. Approved-for-review
solution: preserve latest raw replace per consumer, exact field bytes and source
IDs, compare with hash, replay exactly five in source-ID order before apps start.
The five names are strategy-engine, schwab-1m-v2, orb, orb-schwab, momentum-paper.
Duplicate/fifth-missing/sixth/unknown owner or changed set is refused. The new IDs
are journaled. No tick, buy or other historical event is replayed.

Retention 120 at 5s provides **600 seconds nominal backlog capacity** (595s
oldest-to-newest span). MAXLEN approximate/cadence variable; no exact coverage
guarantee. Scanner warm-up needs 60/120 cycles at the loaded 5s interval.
The existing strategy prefill can still read 120 large payloads (~660-736 MB);
this plan does not fix that reader. Cold boot starts with zero batches, reducing
the expected read, but the Redis margin/eviction checks remain mandatory.

## 3. Literal execution order

The executable commands are `resize_job/run_resize.sh`; the helpers are in
`resize_job/resize.py`. No prose command is a substitute for that reviewed text.

### Before 10:00: preparation, attended by Codex-2

1. Validate release/approval/date, obtain exclusive deploy lock, check clean
   box/main/base, all twelve app/infrastructure identities, file pins, loaded
   scan interval, no concurrent deploy/guard/sampler, Redis margin/config/keys.
2. Fresh direct broker positions and reviewed open-order reads; managed/virtual
   rows/net fills zero on both accounts, no named manual exception, no DB pending
   orders, no active NFQ hold, no unpublished confirmation exit. Use the real
   OmsStore.list_open_orders_for_sync selector on both accounts; zero known
   orders means zero fetch_order_update detail requests, not an account scan.
   Supplemental Schwab discovery is the native-OCO sync reader's exact12h GET
   request (maxResults500), including working/queued/accepted and other
   nonterminal nested legs. Webull uses GET v2 /trade/orders/list-open,
   page_size100; malformed/truncated/unreadable remains STOP. No year-long read.
   Known bot orders block. Unknown broker-side orders are listed for the operator
   as pre-existing orphans, not reboot losses, and do not block. Record exact
   requests in root-only evidence and decoded-response byte sizes; do not claim
   global historical broker enumeration. Archive all retained intent IDs/raw
   fields, prove the three idle gates using current code/config, and record
   "operator: none" for manual-stop preservation.
3. Copy/hash env, preopen, Monday script, Redis config, stale RDB, detector/gate
   files and unit definitions. Capture restart snapshot and original identities.
   Root-only backups and JSON; no passwords are printed. These are config/state
   evidence backups, NOT a full Postgres or provider disk image. Postgres data
   remains on disk. Operator should retain a provider recovery image separately;
   no new provider snapshot or full DB dump is silently authorized here.
4. Write merge-ready. Wait at most 600s for the local attended
   `bash docs/review-artifacts/snaphist1/resize_job/merge_in_window.sh`.
   It rechecks #1087's exact pin/checks/base, merges, verifies tree and emits the
   root-only receipt. No receipt/new head mismatch => stop, do not reboot.
5. Fresh flat check; checkout exact receipt SHA; `pip install --no-deps -e` as
   trader, clean tree/import-path proof. Install/hash narrowed detector files
   from reviewed blobs. Append ONLY retention env120; install no-auto-retry
   drop-ins; enable postboot bootstrap (does not start it on this boot).
6. Fresh flat check then v2 gate, pipefail. Sunday clock-only reason is explicit
   in approval; no armed override. Gate must say zero armed/rows and pass.
   Stop v2/strategy/orb-schwab/orb/paper; OMS remains alive for the drain proof.
   If any check fails after a stop, preserve states/page, do not improvise a
   restoration or continue the resize. Operator receives the exact stopped set.
7. After drain proof, stop OMS/reconciler/control/capture. Fresh final five-owner
   capture/hash comparison. Stop gateway, verify ten app PIDs0/inactive and
   save log byte offsets. Fresh flat check once more. Stop Redis.
8. Offline only: recheck stale RDB against its backup hash; rename on the same
   filesystem to `/var/lib/redis/dump.rdb.pre-resize-20261004.offline`, verify
   original absent and archived hash equal. No SAVE/BGSAVE/CONFIG/FLUSH.
   Write PREPARED with boot ID and exact app SHA; journal and notify operator.
9. Operator performs provider resize/reboot at 10:00 ET. Runner contains no
   shutdown/reboot/provider call. No PREPARED means no resize.

### After boot: one bootstrap, no unattended retry

1. Check new boot ID, Sunday/date/window, exact clean SHA, hardware, RDB absent,
   Redis empty (DBSIZE0), evictions0/config unchanged, app PIDs still0. Any
   unexpected key means STOP; do not flush. Infra must be healthy.
2. Fresh broker/DB flat proof. WATCH subscription stream+hash absent, then one
   transaction with exactly five XADDs of preserved raw fields. Journal source
   and destination IDs. No HSET. A concurrent write aborts the transaction.
3. Start gateway once; record microsecond lower bound AFTER start returns.
   Start the other nine app units once. All twelve app/infra units must have new
   invocation IDs, active/running, NRestarts0. tv-alerts remains inactive.
4. Within180s of the post-start bound require: a healthy fresh gateway heartbeat
   whose active_symbols equals five-owner+static union; five hash fields equal
   preserved sets with `_migration_complete=1`; >=20 post-bound snapshot-ID
   intervals p95<=10s; Redis evictions0 and used_memory<=1.6GB throughout.
   Missing migration fields wait within that same deadline, never PASS.
   Actual mismatched/unknown fields STOP. Checkpoints/values go to content.json.
5. No weekend tick proof is fabricated. Tick delivery remains UNEXERCISED by
   design until the Monday windows below. New-process log Traceback is a stop
   for inspection, not silently labelled historical. Record warm-up/BOOT-HOLD
   literally. A zero-entry cold prefill has NO log line in current code; report
   observed empty stream + absent prefill line, not a made-up "N=0" line.
6. Install isolated checker/catalogs from exact app blob. Require and paste actual
   FLAGGATE134/134 and numeric8/8; retention120 must come from new gateway /proc.
   Unknown never gets forced green. Preserve full output and hashes.
7. One preopen re-pin for10-05: six PID/start pairs (12 identities), appSHA, new
   pre-reboot snapshot and fully classified install record. Nine checker app
   services restarted; tv-alerts deliberately untouched; paper recorded separately.
   Re-pin Monday script appSHA, gatewayPID/start/proc path and paperPID/start.
   Syntax-check candidates before replacing; backups/hash/diff evidence retained.
8. After final Redis/owner/fleet proofs restore only this plan's temporary
   restart-policy drop-ins, register normal boot links, disable bootstrap unit.
   Journal COMPLETE with new identities, memory/eviction0, hardware and hashes.
   COMPLETE does not mean Monday delivery/scanner validation has passed.

## 4. Recovery and stop policy

No gateway rollback to608 with a second automatic restart. That obsolete
standalone section is withdrawn. A failed resize or multiple failed apps,
Redis/Postgres failure, lost owners/eviction, unknown work or interrupted
preparation => STOP and urgent page to operator/claude-1; report actual states.

Only if exact approval includes the one-app recovery clause: after postboot
claim, exactly one app is failed/inactive PID0 and every other app is healthy,
fresh broker/books flat and Redis safety proven, Codex-2 may run once:

```bash
sudo bash /home/trader/after-hours/2026-10-04/resize-job/run_resize.sh recover project-mai-tai-<EXACT_FAILED_APP>.service
sudo bash /home/trader/after-hours/2026-10-04/resize-job/run_resize.sh postcheck
```

The exclusive recovery claim prevents a second attempt. This is ONE start,
not restart of a running service. No reset-failed/retry loop/infra restart.
Reproof uses a new post-start bound; failures page and stop. A failure after
gates/repin or with existing proof artifacts requires reviewed continuation,
not deletion of evidence to rerun. Page delivery failure is itself UNKNOWN.

## 5. Redis read budgets and preservation

| Read | Bound |
| --- | --- |
| INFO stats/memory; CONFIG GET; DBSIZE/XLEN/TYPE | Metadata; combined INFO<=256KiB, config<=16KiB. |
| SCAN known keys | Hint COUNT100, <=2000 keys /256KiB total; unknown state STOP. |
| Owners | HKEYS<=16KiB, exact seven fields; HSTRLEN sum<=256KiB before HGETALL. |
| Subscription capture | XRANGE COUNT25, <=1MiB/page, at most1000 events; tail COUNT1. |
| Retained-intent audit | XRANGE COUNT25, <=1MiB/page, at most2000 events; tail COUNT1. |
| Heartbeat proof | XREVRANGE COUNT25, <=1MiB; gateway source/timestamp required. |
| Snapshot cadence | Exclusive XRANGE COUNT1, <=20MiB response; at most3 reads per second. Discard payload immediately, IDs only for cadence; backlog at cap STOP. No XINFO/EVAL or bulk snapshot reads. |

COUNT is not a server-side byte limit. Payload bounds are checked immediately
after the bounded reply; oversize prevents further calls. One snapshot measured
10-03 was6,135,585 bytes, not an assurance for every future batch. Existing
strategy warm-up multi-entry read is explicitly outside proof-read budgets;
Redis safety covers its resulting margin/eviction risk. Reboot intentionally
removes known ephemeral keys; postboot absence is not called eviction. Compare
evictions23 before reboot with0 after reset, not23==0. No increase within either
boot epoch is allowed. Empty order/runtime streams can remain absent until used.

## 6. Persistence decision and Monday ownership

Default remains OFF as operator directed. RDB archival is not persistence ON.
Enabling RDB later means periodic fork/copy-on-write CPU/RAM and full snapshot
disk writes; AOF means continuing write/fsync and rewrite costs. Neither fixes
broker-action atomicity by itself. No setting change under this plan; raise
durability policy as a separate operator decision. Config backups here are not
a substitute for a tested full-machine/Postgres backup.

Sunday paper can run its idle loop safely but must own zero symbols and must
not prepare/stream. It will prepare Monday03:55 and stream04:00 itself. Leave
failed guard@10-02 evidence untouched; do not start a Sunday guard. Existing
`project-mai-tai-option-a-guard-start-20261005.timer` is re-pinned, not duplicated.

| Time ET / owner | Required evidence |
| --- | --- |
| Sunday close-out, Codex-2 | New identities, /proc retention, actual gates, full owner sets, new timestamped logs, scanner warm-up/BOOT-HOLD literal state. Measure INFO lines/min for5min after startup separately; code-derived12/min is not measured. |
| Monday03:40, named existing guard timer; Codex-2 | New gateway identity/healthy heartbeat/complete owner hash, app source hashes, RedisSafety, guard@10-05 active/NRestarts0, startup and sampler10x1Hz. Failure stops paper only and pages. |
| Monday04:00/first nonempty watchlist, Codex-2 | Actual ticks to every nonempty consumer, fresh snapshots, paper<=16, no second websocket/broker route; v2 warm-up and BOOT-HOLD release. Empty union is N/A, not a delivery PASS. |
| Monday06:20, read-only preopen as trader/root | All new identity pins, real restart evidence and FLAGGATE/numeric output. Any UNKNOWN retained literally. |
| Monday07:10, claude-1 | Schwab v2 bar continuity and each nonempty owner's subscriptions/delivery. |
| Monday scanner first15 active minutes, BOTH | Recipe below; natural low counts are not automatically a defect. |

Scanner comparison frozen to04:00-04:15 ET on10-05 vs same window09-28..10-02,
plus full-session watchlist additions. Extract timestamped live
`[ALERT] [SQUEEZE_5MIN]` and `[ALERT] [SQUEEZE_10MIN]` lines, per-type counts and
unique names, separate replay/prefill emissions, coverage received/expected15min,
strategy errors (must0), consumer watchlist additions deduplicated by event ID.
Use retained raw log/archive files, not capped recent-alert UI or present Redis.
Missing historical coverage=UNKNOWN, never zero. Compute loaded warm-up60/120;
Sunday prefillN<120 is expected because Redis booted empty. Monday require actual
history/readiness, not a fake second startup prefill line. Table has one row per
session, all denominators/raw paths. Codex-2 produces, claude-1 independently
checks; missing readiness, lost additions or qualifying inputs with no alerts
is escalated under Rule1 before declaring scanner validation complete.

## 7. Review dispositions and fresh evidence

- Former blockers are closed to the reviewed scope, not by claiming a complete
  broker-history census or a universal in-memory ACK. No application code is
  changed. Operator 10-03 15:40 ET: "No" manual stops or hand orders to preserve.
  The approval schema records that disposition explicitly.
- Offline verification: 32 tests PASS; Python compile and all shell syntax
  checks PASS. Tests cover altered/duplicate/missing replay owners, source order,
  Redis safety, reply limits, date expiry, offline archival restriction, single
  recovery scope, exclusive evidence, COUNT1 reads and exact approval gating.
  Syntax and final test results are reported with the staged commit.
- Standalone Saturday service/timer LoadState=not-found, inactive, PID0 at15:22ET.
  No standalone restart job ran. Finished sizing timer remains disabled.
- Read-only new flat proof: direct holding rows0/0, open managed0/0, virtual[],
  netfills[], working DB orders0, NFQ holds0, unpublished confirmation exits0.
- Fresh read 10-03 15:56 ET: OMS sync selector0/0, detail requests0. Supplemental
  Schwab GET `/trader/v1/accounts/<hash>/orders?fromEnteredTime=2026-10-03T07:56:23.000Z&toEnteredTime=2026-10-03T20:56:23.000Z&maxResults=500`
  and Webull GET v2 `/trade/orders/list-open?account_id=<id>&page_size=100` both
  HTTP200, rows0; compact decoded JSON sizes2 and42 bytes respectively. Exact
  request/account identifiers are in the root-only evidence. Byte counts are
  labelled decoded JSON, not guessed network size. No orphan discovered.
  No year-long request remains. The12h listing is discovery, not proof of every
  historical broker order; orphan risk is explicitly disposed by the reviewer.
- Idle probes exercised loaded code with real scanner DB input at actual
  2026-10-03T19:56:25.445214Z and simulated Sunday2026-10-04T14:00:00Z:
  v2 market_session=closed, within_entry_window=false; ORB-Schwab universe=[],
  cold-start bars0/opening orders0; paper engine=None, gateway_task=None,
  subscriptions=[], external I/O calls0. Source scanner session was
  2026-10-03T08:00:00Z. These are code probes, not inspection of another
  process's memory. Weekend idle disposition plus fresh books/broker checks is
  the accepted pre-reboot proof; no universal new weekend trading rule is added.
- All353 retained strategy-intent stream IDs and raw fields, including special
  exit envelopes without event_id, are archived for audit. They are historical
  transport that OMS would not reread from `$`, not declared lost or replayed.
  The count is freshly remeasured Sunday, not hardcoded353. Before/after producer
  quiescence archives are exclusive files; changed capture or new work stops.
- Redis evicted_keys23->23; used_memory1,175,614,040->1,181,950,544 bytes during
  the read-only evidence pass. No Redis write or bulk snapshot read.
- Evidence file: `/home/trader/after-hours/2026-10-04/resize-review/review-evidence-1540-ruling.json`,
  SHA256 `343209147691f21d9b91846692250cd83b6451496415db2826fbba8d2930b740`.
  This is outside the eight-file release manifest; it contains private raw
  request/history details and is root-only, not committed to Git.
- Staging installs only the three new unit/timer files and root-only release.
  It starts the approval-gated prep TIMER, not preparation or an app. Postboot
  unit is installed but disabled until approved preparation. It compares all
  ten app identities before/after staging and requires no approval/run directory.
- Sunday exact staged-hash approval remains owed. No merge/restart/archival/
  owner replay/reboot occurs during staging. Cold-boot content, migration,
  replay, startup ordering and recovery remain UNEXERCISED.
