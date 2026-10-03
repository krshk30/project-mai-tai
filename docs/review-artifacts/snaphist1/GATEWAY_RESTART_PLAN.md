# SNAPHIST1 / #1077 / #1084: one weekend gateway restart

Status: DRAFT FOR REVIEW, NOT EXECUTABLE APPROVAL. Prepared 2026-10-03.
Operator chooses the weekend date/time and exact application SHA. No timer or
restart is created by this draft. SNAPHIST1 stays unmerged until this plan exists
and still needs its independent pin and normal merge checks. RPG1 piece 3 keeps
priority; this is not permission to merge or deploy RPG1.

## Release bindings and scope

Current application baseline: 608339894a1cfb33284e695196df55c18f312889.
SNAPHIST1 candidate branch: `codex/snaphist1-120-batches`.
Before final approval, fill `INSTALL_DATE`, `APPROVED_SHA`, reviewed plan SHA,
the complete blob/file hash manifest, and a named attended operator. The final
application must contain reviewed SNAPHIST1 plus already-merged #1077 and #1084.
Any RPG1 changes in that SHA require their own pin and explicitly named scope.

One gateway restart activates all three together:

| Change | Runtime effect |
| --- | --- |
| #1077 | Gateway configure_logging, timestamped INFO; NOT_FOUND warning without traceback. |
| #1084 | Every subscription event persists full owner state and migration marker; missing marker triggers full-stream rebuild. |
| SNAPHIST1 | Snapshot MAXLEN default 180 -> 120; explicitly load env value 120 in the new gateway. |

Do not restart OMS, v2, strategy, ORB, orb-schwab, paper or the guard under this
gateway-only scope. Do not change trade rules, order quantities, flags other
than the named numeric retention setting, subscriptions, database rows or cron
cadences. Do not trim Redis manually. No separate strategy restart for this fix.
If paired with the eventual RPG1 service install, name each additional restart
and order in a reviewed combined plan, not an implicit expansion of this draft.

Numeric catalog entry: `redis_snapshot_batch_stream_maxlen`, expected=120,
owning_service=`market-data`, require_process_env=true. Environment addition:
`MAI_TAI_REDIS_SNAPSHOT_BATCH_STREAM_MAXLEN=120`. Refuse duplicate/case-variant
definitions. Back up and hash the env first; the only allowed redacted env diff
is this key. The publisher snapshots the setting at construction, so a strategy
restart or changing the file alone is not activation.

## Retention and reader impact

Live interval independently read on 10-03: 5 seconds. Scanner warm-up needs
60 / 120 valid batches for 5-minute / 10-minute squeeze alerts. Re-read the
gateway AND strategy loaded interval before install; any disagreement or need
over 120 is a stop for review, not a new interval or alert-rule change.

Momentum starts at `$` but then uses a rolling XREAD offset. At an exactly
5-second cadence, 120 entries provide **600 seconds nominal backlog capacity**,
down from 900 seconds; the oldest-to-newest timestamp span is 595 seconds.
Production cadence varies and MAXLEN is approximate, so no exact ten-minute
recovery guarantee is claimed. A consumer stalled long enough to lose its
offset cannot be called continuous. Preserve/check consumer cursor evidence;
missing coverage is UNKNOWN. Do not replay or fabricate missed Momentum data.

Strategy still reads up to 120 payloads during its existing warm-up: about
660 MB at the previously measured 5.5 MB/batch. Reducing retained depth does
NOT fix that read. No strategy restart here. At the next separately approved
strategy restart, preserve the existing Redis margin/eviction proof. Ownership
and next action for that risk remain the strategy warm-up follow-up, not this
setting-only PR. Live XREAD count=500 backlog readers are also unchanged.

## 1. Detector inventory BEFORE gateway restart

The gateway will begin INFO snapshot-count lines (code-derived ~12/min at 5s,
plus subscription/startup/error lines). A bare `1008` reader would misread
`11008 records` or `count=1008` as a policy disconnect. The only accepted
signature is `1008 (policy violation)` or an independently reviewed equivalent.
Install/hash-verify every deployed reader BEFORE the gateway restart. A source
merge, a hash of the wrong path, or an updated disk file beneath an old running
reader is not proof that the running reader is safe.

Read-only inventory at 10-03 ~13:03 ET (refresh it at execution):

| Reader / invocation | Installed SHA256 | Observed treatment |
| --- | --- | --- |
| `/home/trader/project-mai-tai/scripts/option_a_treatment_1008_sampler.py`; dated guard unit launches this checkout script | `22b2f4fdb5a0b5aabea962b8718652adf44e829c5222f51e001ea9b37b648aa5` | Narrowed signature already on disk. Inventory any running sampler separately. |
| `/home/trader/project-mai-tai/ops/health/fleet_health_check.py`; root cron -> `ops/health/fleet_health_cron.sh` | `cc0a87158cc3df0da77ef9f6c0e37b2eff23309e554e13ca95cd8014878d03ac` | Narrowed signature already on disk; this reader produces the massive-1008 verdict consumed by the pager wrapper. |
| `/home/trader/unexercised_watch/watch.py`; root cron with SHA guard | `45df60c60fe55af89406d20c29de277a44634d6bc6308586a4e0b62e718b8272` | No 1008 detector. Preserve it; unrelated INC1 source coverage is NOT repaired by this plan. |

Inventory root/trader cron, timer/service ExecStart, wrappers/environment path
overrides and live process command lines. Record exact reader paths and source
blobs; check every pager reader, not only the sampler. Hash the wrapper too.
If a separately installed copy exists, install the narrowed reviewed blob with
a backup and update BOTH installer/source and runtime SHA guards when present.
That exact artifact list and commands must be reviewed before execution. Unknown
copy, old in-flight reader, mismatched hash, or bare needle => NO restart.
If replacement requires a reader service restart, obtain explicit scope first;
do not disable watches to get through.

Run installed-reader offline fixtures without paging: `11008 records` and
`count=1008` must count zero; the real ConnectionClosedError line containing
`received 1008 (policy violation); then sent 1008 (policy violation)` must count
one line. Preserve stdout and exit codes. No synthetic entry in live logs.

## 2. Preflight and evidence capture

Use a new O_EXCL evidence directory under `/home/trader/after-hours/<date>/`;
never overwrite prior attempts. Acquire the reviewed deploy lock and verify
there is no concurrent install, guard trigger, or market-open overlap. Pin the
clean box HEAD, exact source target/ancestry/diff, imports, service PIDs/start
times/NRestarts and all detector/catalog hashes. A moved PID or changed scope
requires fresh review; old historical IDs are not execution pins.

Read both brokers directly and the bot books with the reviewed strict-flat
helper. No automatic reuse of a prior named manual-close exception. Flatness,
working entry orders and broker readability must be proven immediately before
the restart. Record arms; no v2 restart is authorized here. Refusal leaves the
gateway running untouched.

Take these content checkpoints immediately before and after the restart/proof:

- Redis `INFO stats`: evicted_keys; `INFO memory`: used_memory. Keep the
  1,600,000,000-byte margin unchanged. Above margin, any new eviction, missing
  previously present stream or unreadable safety evidence => STOP/UNKNOWN,
  page, no repeated proof/restart/rollback loop.
- Metadata-only inventory of existing `mai_tai:*` stream names and presence.
- The complete owners hash, including `_migration_complete=1`, `_last_applied_id`
  and the five consumer sets: `strategy-engine`, `schwab-1m-v2`, `orb`,
  `orb-schwab`, `momentum-paper`. Capture static symbols separately from config.
  Empty arrays are explicit owned-empty sets, not missing consumers. Paper <=16.
- Fresh retained raw replace events for ALL FIVE consumers, source IDs and
  exact bytes, paged COUNT 25, plus checkpoints. Unknown consumer, unreadable
  stream, unexpected action, incomplete reconstruction or disagreement between
  stream/hash/healthy heartbeat union => refuse. No Redis repair authorized.
- Healthy gateway heartbeat with source/produced_at, active_symbols equal to
  the union of the five consumer sets plus static. Preserve consumer offset
  coverage and current paper/guard identities; do not stop/restart them.

Reads in the reviewed runner must remain bounded:

| Read | Bound / recorded bytes |
| --- | --- |
| INFO, identity/config and known-key EXISTS/TYPE | Metadata only; record bytes, reject >256 KiB. |
| Owner hash | Precheck HLEN/HSTRLEN; known fields only, total <=256 KiB. |
| Subscription stream | XRANGE COUNT 25 per page; record reply bytes <=1 MiB/page. |
| Heartbeats/ticks | Paged small reads; record <=1 MiB/reply, no unbounded range. |
| Snapshot cadence | Bootstrap XREVRANGE COUNT 1, then exclusive XRANGE COUNT 1; discard payload immediately, intervals from IDs. At most three reads/second; backlog beyond that => UNKNOWN. Record per-entry bytes, stop further reads on >20 MiB. |

Never use snapshot XINFO/EVAL (they materialize payloads), bulk ranges or
COUNT>1. Measure one current entry before approving the literal runner; do not
claim COUNT 1 is a universal byte guarantee. First-run record must state actual
reply sizes. The existing strategy warm-up is outside these proof reads.

Re-read owner source IDs just before restart. If changed, recapture; do not
compare a fresh process to a stale consumer snapshot. After restart, changes
must reconcile to actual newly retained replace events for each consumer.

## 3. One restart and post-restart content proof

Final reviewed runner must provide literal, fail-closed commands for: backup,
approved checkout/runtime refresh if needed (pip as trader), detector install
and hashes, retention env edit, fresh flat/owner/Redis checks, then exactly one
`systemctl restart project-mai-tai-market-data.service`. No deploy_main or
deploy_service helper that silently restarts companions. Those commands and the
operator's exact-SHA GO are still owed; this document is the draft scope, not a
replacement for them.

Capture deadline anchor BEFORE restart; capture a separate microsecond UTC
content lower bound AFTER restart returns. No pre-restart tick/heartbeat/batch
can satisfy the new-process proof. Evaluate by 180 seconds from the anchor:

1. New PID/start, active, NRestarts=0; post-bound heartbeat healthy and exact
   active_symbols union count. Print expected/observed sets/counts and age.
2. For every expected symbol, post-bound trade_tick or quote_tick within 120s.
   Attribute every symbol back to ALL its owners; print per-consumer coverage
   and tick timestamps, including overlapping and explicit empty sets.
3. At least 20 post-bound snapshot ID intervals; p95 <=10s. Print n, coverage
   and p95. No max<=15 requirement and no INFO-log surrogate.
4. Owners hash `_migration_complete=1`; each of the FIVE consumer sets equals
   the preserved/reconciled source events, static correct and checkpoint valid.
   Print individual set comparisons. Recheck evictions and stream presence.

Owner/heartbeat equality proves restored content but NOT delivery while the
market is closed. Weekend tick silence is UNEXERCISED, never PASS. Before final
GO the operator must accept this distinction and name the next market-data
verification window; otherwise item 2 cannot pass and the install is not ready
to run. In a quiet window, print real control counts, do not synthesize ticks or
replay them into the live stream. The next-session read must establish real
delivery to all nonempty owners before declaring subscription service exercised.

Read `/proc/<NEW_GATEWAY_PID>/environ`: retention=120, interval=5, other values
unchanged. Install/hash-check reviewed expected_numeric.json with owner
market-data, plus compatible isolated checker/catalogs. Run the actual numeric
and complete FLAGGATE checks; baseline 7 numeric checks becomes 8, baseline
133 combined becomes 134 unless other reviewed changes explicitly alter them.
Require actual rc and lines, not assumed denominators. Preserve any unrelated
UNKNOWN honestly. Inspect first timestamped gateway log and measure INFO lines
per minute (startup separately, then five minutes); preserve every exception,
and distinguish warning-only NOT_FOUND from real failures.

No rollback, owner replay write, second gateway restart, paper stop/start, or
other service recovery is preauthorized by this draft. Any post-start failure:
page with actual states, preserve evidence, and get the operator's recovery
instruction. If the operator wants a one-attempt rollback, review its literal
commands and five-consumer replay/checkout-restoration proof BEFORE the GO.

## 4. Mandatory scanner validation (Codex-2 AND claude-1)

Do not restart strategy just to obtain this evidence. Bind to the strategy
restart in the separately approved next RPG1/combined install. If that restart
is not included, scanner post-restart validation stays outstanding/UNEXERCISED.
Do not reuse today's N=120 line as evidence of a future 120-cap restart.

Freeze the comparison windows before collecting outcomes. At the actual
strategy restart save its new PID/start/log byte offset and loaded interval.
Compute squeeze_10min_needs from that configuration. Require a NEW
`prefilled momentum alert history from N snapshot batches` with N>=needs (120
at 5s), both warm-up readiness values and no error/traceback in the new log
segment. Record Redis safety before/after warm-up; do not relax its margin.
If the stream lacks 120 valid batches, say incomplete, wait for actual fill-up;
do not claim fully warm or change the scan interval.

For the first trading session after installation, extract timestamped actual
`[ALERT] [SQUEEZE_5MIN]` and `[ALERT] [SQUEEZE_10MIN]` emissions from strategy
logs. Keep replay/prefill reconstruction emissions in a separate column; they
are not new live signals. Use the first 15 minutes after the market-active
restart, or the first 15 active minutes at session start if restarted on the
weekend. State exact ET and UTC endpoints. Compare the SAME clock window in
the prior five trading sessions, not five calendar days. Record:

| Session | Log coverage / expected minutes | Live 5m alerts / unique names | Live 10m alerts / unique names | Replayed alerts | Watchlist additions / unique names | Strategy errors |
| --- | --- | --- | --- | --- | --- | --- |
| First session + each of prior five | Required | Required | Required | Separate | Required | Required |

Also compare full-session watchlist additions with those prior five sessions.
Obtain actual per-consumer replace events from the existing subscription
archive/trade/scanner capture, deduplicate by source event ID, and count set
additions rather than repeats or the current list size. Report v2 and other
consumers separately. If historical retention cannot reconstruct a baseline,
label the missing session/denominator UNKNOWN; never substitute current Redis
state or zero for missing history. Preserve raw filenames and all boundaries.
Current/recent-alert UI lists are capped and cannot prove full-session counts.

Codex-2 produces the table and raw extraction; claude-1 independently checks
the warm-up line, counts and watchlist changes. Any missing coverage, absent
10-minute readiness, zero alerts despite qualifying input, lost additions, or
strategy errors is escalated with code/data evidence. Natural day-to-day count
variation is not automatically a code failure; investigate instead of declaring
PASS from comparable totals. Weekend zeroes are not a scanner validation.

## 5. Close-out and scheduling

Journal target/plan SHA, before/after PIDs, immutable raw paths, backups/hashes,
env diff, installed reader/catalog hashes, every content sub-call and value,
five owner comparisons, evictions before/after, INFO volume, numeric/flag rc,
scanner evidence and pending next-session checks. Bind any preopen change to
the new gateway identity and preserve all other service pins. One reviewed
preopen re-pin after the final identities; never repin to hide drift. The
existing Monday guard procedure hard-pins the gateway identity: do not leave
it stale after a weekend restart. Its reviewed replacement pins/source SHA and
the 06:20 gate must be prepared and approved with this plan before execution.

After exact-SHA approval, scheduling must be a named, listable systemd one-shot
and timer on the box, not a claim that a chat heartbeat is an installed job.
No such gateway timer is installed or approved now.

Completed-install timer cleanup, actually performed 2026-10-03:
`project-mai-tai-sizing-nfq1-install-20261003.timer` disabled and stopped;
UnitFileState=disabled, ActiveState=inactive, SubState=dead, no next elapse and
`systemctl list-timers --all <name>` returns 0 timers. Its historical service
failure evidence is preserved. Monday's separately approved
`project-mai-tai-option-a-guard-start-20261005.timer` remains enabled/active,
NEXT=2026-10-05 07:40:00 UTC (03:40 ET); it was not modified.
