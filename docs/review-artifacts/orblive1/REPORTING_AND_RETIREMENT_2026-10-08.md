# ORBLIVE1: live reporting and paper-process retirement

## Step 0

AGREE (source, base d244f602): OrbSchwabService overrides OrbService.run and never
calls the old paper heartbeat. No service_heartbeats writer exists in production
source, so that table cannot be used as the new freshness check. The existing
Redis heartbeats stream is the reporting source; fleet-health reads it boundedly.
The 06:27 production observations supplied by the reviewer were not independently
re-read in this side conversation and remain attributed to that read.

AGREE (dependency): orb-schwab imports shared universe/tick aggregation in process;
it does not read the paper process's orders or events. Its live opening-order,
MACD/ATR admission and exit routines remain separate. Universe refresh, subscription
replacement, timestamp normalization and drain budget are retained. No OMS tick
handler or broker order path is changed.

AGREE (page additions): missing heartbeat can use fresh direct unit state plus
that consumer's exact gateway-owned set before 09:27 only. That is explicitly
labelled unit evidence, with active-since and NRestarts, NOT a synthetic heartbeat.
Unreadable evidence stays STOPPED; stale real heartbeat never falls back. Once
reporting exists, heartbeat freshness (60 s), phase and subscribed set own the tile.
Positions/orders still come from the exact live strategy/account DB view.

AGREE (suspended tabs): every ORB data-section header carries the render date.
An old paused page visibly says its old date; a new request excludes the prior
day's rows. No historical decision/fill is invented to populate an empty section.

## Implementation boundary

- Independent 15 s heartbeat task; Redis publish bounded to 2 s. No heartbeat SQL
  and no tick-path publication. Failed publication recovers on the next scheduled
  turn without blocking bar/exits; cancellation joins the task.
- Existing heartbeat details contract is dict[str, str]. Universe/subscribed are
  JSON arrays encoded as strings, decoded at the page boundary. Observe-only mode
  cannot claim LIVE subscriptions.
- WAITING FOR 09:27, EVALUATING, WORKING, IN TRADE, SESSION COMPLETE; absent/stale
  heartbeat is STOPPED unless the explicitly bounded transitional unit proof applies.
- Production OrbService is feed-only. Its standalone entrypoint refuses to run.
  The retired simulation is preserved ONLY under tests/support for historical
  regression replay, outside the packaged src tree; production never imports it.
- Seven simulation-only boolean Settings fields and catalog rows are removed;
  the catalog still exactly covers all production Settings booleans. The retained
  OMS quote-pricing compatibility flag is owned by OMS, its actual remaining reader.
  Process catalog: 142 boolean checks + 8 numeric = 150. This is an inventory count,
  NOT a live audit receipt. Fleet runtime/full gets one additional heartbeat verdict.
- Bootstrap, target and install inventory no longer install/enable the paper unit.
  The standalone deploy selector targets orb-schwab, not the retired paper process.
  No strategy or paper broker-account registration is added.

## After-close sequence (not executed)

Execute only after the reviewed exact patch is pinned, merged and selected for the
after-close install. No moving-branch checkout, no market-hours stop, no raw HDEL,
no gateway/OMS restart for this retirement. The live reporting install needs the
orb-schwab and control processes to load the reviewed source; schedule those with
the existing flat/working-order/open-row install discipline, not a hand restart.

Read-only checks immediately BEFORE stopping the paper process:

1. Record box clock (ET after 16:00), exact checkout SHA/clean tree and source hashes.
2. `systemctl show project-mai-tai-orb.service project-mai-tai-orb-schwab.service
   --property=ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp`
   and `systemctl cat` both units. Record all other live process identities.
3. `redis-cli --json HGETALL mai_tai:market-data-subscription-owners` bounded to
   five seconds: record every owner, migration marker and union. Require a readable
   orb-schwab owner set; use the actual set, not hard-coded AIXI/DKI.
4. Read `/health`, `/api/bot/orb-schwab` and `/api/overview` locally. Record current
   live positions/orders/strategy attribution. Before any LIVE-process restart,
   independently read both brokers and require the existing flat/order/row gates.
   Unexpected holding, order, open row or unreadable proof stops the live restart.
5. Confirm no live dependency/registration or managed row belongs to retired `orb`;
   preserve the existing ORB live broker-book rows and current subscription.

Writes, after these receipts and after close:

1. `systemctl disable --now project-mai-tai-orb.service`; require inactive/MainPID 0.
2. Publish exactly one `consumer_name=orb, mode=replace, symbols=[]` through
   OrbService._sync_gateway_subscription with startup announcement enabled. Never
   delete a hash field directly. Wait for gateway application/owner `orb=[]`.
3. Re-read all owner sets: orb-schwab and all other consumers must be byte-identical;
   migration marker retained; last-applied stream cursor may advance by design.
   The empty ORB tombstone remains until a separately reviewed cleanup.
4. Install the reviewed catalog/target inventory, remove only retired ORB identity
   arguments from the pinned preopen binding, and load the live heartbeat/control
   source via the reviewed after-close service sequence. No paper process restart.
5. Require fresh live heartbeat, exact subscriptions, new live identities and
   NRestarts 0; read phase/tile, orders and owned positions. Audit all 150 catalog
   checks, preserving truthful inactive-paper UNKNOWN where applicable; require
   zero mismatches. Verify the paper unit is disabled/inactive and target no longer
   Wants it. Re-pin only the changed identities for tomorrow's daily preopen gate.

Acceptance tomorrow: 07:00 WAITING FOR 09:27 + universe; 09:27 EVALUATING; accepted
order WORKING; owned position IN TRADE; flat after window SESSION COMPLETE. Until
actually observed these are scheduled acceptance criteria, not live PASS evidence.

## Local evidence

Focused unique tests: 828 passed. Seven offline mutations RED: absent/stale gate,
wrong waiting phase, producer phase boundary, wrong service name, stale-heartbeat
fallback mask, date labels removed, heartbeat made serial/blocking. These mutate
only in-memory functions in an isolated test process, never tracked source.

Full-unit exact-base/head pair is running; do not infer its result from historical
48-failure receipts. The baseline measured here is 47 failed / 7,491 passed.
Production receipt, final live audit, tomorrow's phase transitions and retirement
are UNMEASURED. No production action occurred in this side conversation.
