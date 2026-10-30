# Live Market Restart Runbook

## Reviewed five-service package: automatic restart evidence

October 29, 2026 source proposal, for C1's separately authorized installation
**after the October 30 close**. Source and private guard qualification do not
prove an installation, native flatness, or a future scheduled preopen PASS.
The older service-specific procedures below remain historical context. This
focused package procedure supersedes their manual evidence sequence only when
C1 approves the exact final package and concrete execution plan. It does not
change `deploy_main`, `restart_all`, OMS-only, control-only, or C6 installers.

The entrypoint is `ops/install/five_service_install.sh`. Its default or
`--plan` invocation prints a plan and performs no queries or service actions.
Execution requires the explicit `--execute TARGET_SHA ROOT600_PLAN.json
SERVICE [SERVICE...]` form. Only `oms`, `schwab-1m-v2`, `strategy`, `control`,
and `orb-schwab` are accepted; v2 is mandatory. Include every actual consumer
of changed source. This is an attended C1 root/Linux operation, never a worker
or Main market-hours command. All production Git commands run as trader.

### Concrete inputs and staging

Before execution C1 reviews the literal final commit/tree/source/migrations,
full private qualification, current installed commit and environment SHA,
selected service list, all new switches, independent expected migration head,
and the current native/local preflight procedure. No old receipt is fresh
approval. Stage the reviewed entrypoint, library and proof hook outside the
mutable checkout in a root-owned0700 directory, files root-owned0600. Verify
all three file hashes against the approved source before launching the staged
entrypoint; a checkout cannot replace the executing script halfway through.
The proof hook's absolute path and SHA are mandatory plan operands.

The root0600 JSON plan has this **exact** schema (replace all explanatory
values with C1-reviewed literal operands; this example is not executable):

```json
{
  "version": 1,
  "target_sha": "<actual reviewed 40-hex final commit>",
  "expected_current_sha": "<actual clean installed 40-hex commit>",
  "expected_env_sha256": "<fresh 64-hex full environment preimage>",
  "install_date_et": "<actual authorized installation day>",
  "expected_alembic_head": "<independently reviewed migration identifier>",
  "evidence_directory": "/home/trader/after-hours/<actual-day>/<new-one-install-namespace>",
  "updates": {
    "MOMLIVE_SEC_APPROVED_HEAD": "<same actual final commit>",
    "MAI_TAI_<REVIEWED_BOOLEAN>_ENABLED": "true"
  },
  "remove": ["MAI_TAI_<REVIEWED_RETIRED_BOOLEAN>_ENABLED"],
  "expect_flags": ["schwab-1m-v2:<ACTUAL_FLAG_SUFFIX>=true"],
  "clock_override_reason": null,
  "verification_hook": "/root/<private-staging>/five_service_install_proof.py",
  "verification_hook_sha256": "<reviewed hook 64-hex SHA>"
}
```

Use only current reviewed boolean switch changes plus the exact MOMLIVE pin.
Preserve every unrelated environment byte. Explicit removals must exist;
duplicate/case aliases are a refusal. No arbitrary secret or numeric edit is
accepted. A clock-only reason, if needed, must be C1's contemporaneous literal
ruling for this execution. No historical Bug2, position, or cannot-see waiver
transfers. The expected migration head is independently approved; never copy
an old dated head or echo the observed value as its own expected value.

The checked-out target must already exist locally and the starting checkout
must be clean at the exact reviewed current commit. There is no fetch,
migration, daemon reload, timer action, forced trade, STOP clear, automatic
retry/reset/remediation/rollback, or implicit preopen publication. Storage
must retain15GiB plus the full checkout copy allowance. The exact safety STOP
must remain absent. Existing global installer and native preopen execution
locks are acquired exclusively/nonblocking; an occupied lock refuses.

C1's October29 21:37 pre-stop source contract binds the existing strict OMS
preflight, then v2 preflight, then the OMS's **already published** native census.
The publication log has generation/completeness/accounts/reason, but **no
working-order count**. The proof hook therefore performs one bounded GET of
that same OMS-owned `PREFIX:brokerdecides1-native-book` packet, using only the
actual OMS process's selected Redis namespace/account aliases. No provider is
called. Require schema3, current OMS epoch interval, same generation as the
latest complete publication, both account books complete, both working and
position sets empty, no unresolved local in-flight symbol, and original wire
and publication age under60s at the first-stop boundary. Retain the exact raw
packet plus its SHA and proof. A manual native exposure also blocks this
conservative all-account gate. A missing/stale/mismatched/unknown packet stops;
no retry and no inference from local zero. This is source-bound and still
requires C1 review and an actual future execution; it is not a fresh native
receipt from Main.

### Actual evidence and action order

1. Create one new root0700 evidence namespace and retain the reviewed plan,
   selected service list and staged source hashes. Verify the environment's
   full preimage before any stop. The proof hook saves all11 actual fleet
   identities/states and confirms readable startup logs before disruption.
2. Run the reviewed native/local gates and deployed strict OMS/v2 preflights
   while all old heartbeat writers are alive. Any failed/UNKNOWN gate stops.
   Run the **official collector snapshot once**, before the first stop, and
   require actual exit0. Preserve stdout/stderr and the whole immutable
   snapshot; seal its SHA and recheck it before every subsequent use.
3. Recheck clean starting HEAD, environment and snapshot. Stop only selected
   services in order **orb-schwab → v2 → strategy → OMS → control**, once each.
   Retain actual entered/completed timestamps and unit state. Require PID0,
   inactive/dead, successful result. A failed/unknown stop ends the sequence.
4. Checkout only the reviewed target as trader. Apply the explicit environment
   changes through backup-first byte CAS, fsynced temporary file, atomic
   replacement, parent-directory fsync and root0600 exact readback. A changed
   preimage stops; it never overwrites a concurrent edit.
5. Start only selected services **OMS → v2 → strategy → control → orb-schwab**,
   once each. Before each start, seal the stopped process's log offset, inode
   and device so the new process's entire startup is covered. Require active,
   running, NR0, new PID and InvocationID. Check each first healthy heartbeat
   within the original60s SLA (strategy240s), after the selected control/API
   has started. The SLA is measured from each actual process start, never reset
   to the check time. A slow full startup that cannot prove it fails; no waiver.
   After OMS start and before any selected consumer, require its actual fresh
   `[BROKERDECIDES1-PUBLISH] complete=True accounts=2` startup publication.
   Symbol UNKNOWN remains held; account completeness does not clear it.
6. The post-start proof uses the checked-out boolean and numeric catalogs,
   real selected process flags, saved first-health receipts for the exact new
   instances, full11 identity comparison, and **one numeric600-second watch**.
   Retain all selected startup/watch log bytes. ERROR/CRITICAL/Traceback,
   rotation/truncation/overflow, a new STOP, an instance change, or an unknown
   proof stops. Unselected service identities/states must equal the original
   baseline, including intentionally inactive services. No global restart.
7. Build schema1 installation actions only from the whole schema3 pre-stop
   snapshot and actual ordered entered/success journal. Classify all9
   collector services; selected ones restarted, all others deliberately
   untouched. Missing/duplicate/failed/foreign/out-of-order actions or a
   journal before its snapshot refuse. Never fabricate an after-fact baseline.
8. Run **one direct official collector report**, after the last selected start
   and proofs. Use that snapshot, actual installation record, exact repeated
   `--restarted` list, current selected `--expect-flag` operands, independent
   migration head and `--no-schema-change`. Preserve raw exit/findings and
   report. Do not also invoke the report wrapper in series. Removing an MD
   restart also removes its per-restart MD flag operand; full catalog audit
   remains. No quiet/held/upgrade-ACK waiver is added.
9. Emit matching future scheduled-gate argument JSON as a SOURCE/CAS input
   only. It does not edit the gate, binding or runtime. C1 separately reviews
   fresh metadata preimages/backups, reader lock, dependencies, owners/modes,
   actual epochs and all readbacks before any authorized publication.

Any failed or UNKNOWN operation preserves all raw evidence and the actual
fleet state, then stops for C1 disposition. A stop/start/report is never
retried automatically. Rollback requires a separate current C1 ruling,
fresh safety gates and actual owned process joins; old PIDs cannot be restored.
The October29 missing collector snapshot remains missing. This future script
cannot repair or retag tonight's historical report or predict October30 06:20.

## Historical service-specific procedures

Use this runbook during an active trading session.

Current state as of March 29, 2026:

- broker-side orders and positions survive service restarts
- Postgres-backed orders, fills, `virtual_positions`, and `account_positions` survive service restarts
- `strategy-engine`, `oms-risk`, and `market-data-gateway` do not fully replay downtime state on restart
- `ops/systemd/restart_all.sh` is for off-hours use only

This means live-market restarts must be coordinated.

Branch and deploy rule:

- restart and deploy from `main`
- do not leave the VPS on a feature branch after validation work
- if an emergency requires a temporary feature-branch deploy, document the
reason and rollback plan in the session handoff, then return the VPS to
`main` as soon as possible

## Service Risk Levels

Lower-risk restarts during market hours:

- `project-mai-tai-control.service`
- `project-mai-tai-reconciler.service`
- `project-mai-tai-tv-alerts.service`

Higher-risk restarts during market hours:

- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-market-data.service`

Golden rules:

- prefer restarting only the service you changed
- prefer restarting while flat
- do not use `ops/systemd/restart_all.sh` during an active session
- stop `project-mai-tai-strategy.service` before restarting `project-mai-tai-oms.service`
- stop `project-mai-tai-strategy.service` before restarting `project-mai-tai-market-data.service`

## Useful Checks

Dashboard/API checks:

- `https://project-mai-tai.live/api/overview`
- `https://project-mai-tai.live/api/scanner`
- `https://project-mai-tai.live/api/bots`
- `https://project-mai-tai.live/api/orders`
- `https://project-mai-tai.live/api/positions`
- `https://project-mai-tai.live/api/reconciliation`

Service checks:

```bash
sudo systemctl status \
  project-mai-tai-market-data.service \
  project-mai-tai-strategy.service \
  project-mai-tai-tv-alerts.service \
  project-mai-tai-oms.service \
  project-mai-tai-reconciler.service \
  project-mai-tai-control.service \
  --no-pager
```

Tail logs:

```bash
sudo tail -n 80 /var/log/project-mai-tai/strategy.log
sudo tail -n 80 /var/log/project-mai-tai/tv-alerts.log
sudo tail -n 80 /var/log/project-mai-tai/oms.log
sudo tail -n 80 /var/log/project-mai-tai/market-data.log
sudo tail -n 80 /var/log/project-mai-tai/reconciler.log
sudo tail -n 80 /var/log/project-mai-tai/control.log
```

Follow one service live:

```bash
sudo journalctl -u project-mai-tai-strategy.service -n 100 --no-pager -f
```

Convenience scripts:

- `bash ops/systemd/restart_control_live.sh`
- `bash ops/systemd/restart_reconciler_live.sh`
- `sudo systemctl restart project-mai-tai-tv-alerts.service`
- `bash ops/systemd/restart_strategy_live.sh`
- `bash ops/systemd/restart_oms_live.sh`
- `bash ops/systemd/restart_market_data_live.sh`

GitHub Actions equivalents:

- `Deploy Service` with `service=control`
- `Deploy Service` with `service=reconciler`
- `Deploy Service` with `service=tv-alerts`
- `Deploy Service` with `service=strategy`
- `Deploy Service` with `service=oms`
- `Deploy Service` with `service=market-data`

Automated live preflight:

- risky `Deploy Service` runs now block automatically if live state is not clean
- current preflight checks cover pending intents, open positions, recent fills, critical reconciliation findings, and stale/unhealthy heartbeats
- if preflight blocks the workflow, treat the situation as operator-reviewed rather than retrying casually

Optional hold behavior:

- `bash ops/systemd/restart_oms_live.sh --hold-strategy`
- `bash ops/systemd/restart_market_data_live.sh --hold-strategy`
- `Deploy Service` with `hold_strategy=true` for `oms` or `market-data`

## Preflight Before Any Trading-Critical Restart

Do these checks first:

1. Open `/api/orders`, `/api/positions`, and `/api/reconciliation`.
2. Confirm whether any strategy has open positions, pending opens, pending closes, or recent fills still settling.
3. If there are open positions, assume the restart is risky and prefer waiting until flat.
4. If there are `pending`, `submitted`, or `accepted` intents, do not restart yet.
5. If there are manual operator actions in progress, finish them first.

Safe-to-proceed signal:

- no pending or in-flight strategy intents
- no order you are actively waiting to fill or cancel
- operator understands the current broker/account positions

## Control Plane Restart

Use when:

- only dashboard code changed
- API/UI is stale or broken

Command:

```bash
sudo systemctl restart project-mai-tai-control.service
```

Post-checks:

- `/api/overview` loads
- `/api/orders` and `/api/positions` still render
- no trading services changed state unexpectedly

Expected impact:

- UI/API blip only
- trading pipeline keeps running

## Reconciler Restart

Use when:

- only reconciliation logic changed
- reconciliation worker is stuck

Command:

```bash
sudo systemctl restart project-mai-tai-reconciler.service
```

Post-checks:

- `/api/reconciliation` updates again
- no new critical findings appear unexpectedly

Expected impact:

- detection gap only while the service restarts
- trading pipeline keeps running

## Strategy Restart

Use when:

- only `strategy-engine` code changed
- market data and OMS are otherwise healthy

Important limitation:

- runtime bot memory does not fully rehydrate from OMS/DB on restart
- dashboard may still show broker and virtual positions even if runtime bot positions reset

Procedure:

1. Open `/api/bots`, `/api/orders`, and `/api/positions`.
2. Confirm there are no pending opens, pending closes, or active order acknowledgements still in flight.
3. If not flat, pause and decide whether the restart is worth the risk.
4. Restart the strategy service:

```bash
sudo systemctl restart project-mai-tai-strategy.service
```

Post-checks:

1. `/api/overview` shows `project-mai-tai-strategy.service` healthy.
2. `/api/scanner` returns live data again.
3. `/api/bots` shows watchlists rebuilding.
4. `/api/positions` still shows broker/account positions and virtual positions.
5. `/api/reconciliation` does not show new critical drift.

If runtime positions disappear but account positions remain:

- treat the bot as not fully recovered
- do not assume the strategy remembers the position lifecycle
- watch for reconciliation drift and do not make a second trading-critical change casually

## Schwab 1-Min v2 Boot Hold

### Durable restart evidence

Do not close a v2 restart from a hand-written checklist. Capture the fleet before the restart and
grade it afterward with the checked-in evidence collector:

```bash
sudo .venv/bin/python ops/health/v2_restart_evidence.py snapshot \
  --output /tmp/v2-restart-before.json

sudo .venv/bin/python ops/health/v2_restart_evidence.py report \
  --snapshot /tmp/v2-restart-before.json \
  --restarted schwab-1m-v2 \
  --expect-flag schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true \
  --expected-alembic-head 20260910_0020 \
  --no-schema-change \
  --output /tmp/v2-restart-evidence.md
```

The `snapshot` command must exit `0` before the restart. A non-flat live account exits `1` while
still preserving the measured evidence file; an unreadable measurement exits `2`.

Add every restarted service with another `--restarted`, and name each required live-process flag
with another `--expect-flag`. For a deploy containing a migration, replace `--no-schema-change`
with every object the migration added, for example `--schema-column table.column` and
`--schema-constraint table.constraint`.

The report is deliberately strict:

- it names ET and UTC timestamps rather than inheriting the box's UTC clock
- it counts open managed rows and nonzero account-position rows across both live accounts before
  and after the restart, so a later flat book cannot hide exposure present at restart time
- it records the migration head before and after, requires no movement for `--no-schema-change`,
  and requires movement plus named schema objects for a migration deploy
- it compares all nine fleet PIDs with the pre-restart snapshot, so an "untouched" service is
  measured rather than assumed
- it reads feature flags from `/proc/<newpid>/environ`, never from the env file
- it reports REST warmup as `rest_warmed/evaluated`, pending symbols, the 300-second fresh-bar
  age bound, and the distinct 369-second seeded-fallback wait with its released symbols
- it quotes the literal post-restart `restoration_complete=1` BOOT-HOLD release line
- it counts all same-session bar gaps and separately counts gaps that span the restart
- it scopes each traceback header to its nearest preceding timestamped line; an undated traceback
  makes the report `UNMEASURED` instead of silently becoming pre- or post-restart

Exit `0` is a complete report with no failed checks, `1` is a measured failure, and `2` means the
instrument could not establish one or more measurements. Never replace exit `2` with a zero.

- Do not restart `project-mai-tai-schwab-1m-v2.service` between 07:00 and 16:00 ET
  without a new explicit operator decision. Premarket entries are live from 07:00.
- After a restart, require `[V2-BOOT-RESTORE] restoration_complete=1` before treating
  the entry path as available. An empty restored-state list before that marker is not safety.
- The authoritative log is `/var/log/project-mai-tai/schwab-1m-v2.log`, not journald. It rotates
  at 00:00 UTC (20:00 ET), so one trading day can span the current and rotated files. Read both in
  chronological order before diagnosing a hold:

  ```bash
  sudo sh -c '
    for f in /var/log/project-mai-tai/schwab-1m-v2.log*; do
      case "$f" in *.gz) zcat -- "$f";; *) cat -- "$f";; esac
    done
  ' | grep -E '\[V2-BOOT-(RESTORE|HOLD)\]|\[V2-REST-WARMED\] schwab_v2 REST warmup complete for' \
    | LC_ALL=C sort | tail -n 100
  ```

- Triage the `reason=` field before acting:
  - `ineligible_exclusion_unreadable`: repair the broker-eligibility DB read, then wait for the
    next scanner snapshot. `last_known_exclusions_applied=N` states how many cached exclusions were
    actually used; a cold post-restart cache correctly reports zero.
  - `state_seed_incomplete`: at least one selected symbol's DB seed was unreadable. Repair the DB
    read and let the next restoration pass retry it.
  - `rest_warmup_incomplete`: inspect the explicit `warmup_pending_symbols=` list and restore REST
    bar delivery for those symbols.
  - `empty_evaluated_population_after_exclusions`: no selected state was evaluated; wait for a
    current non-empty scanner population. Zero evaluated states is not safety.
  - `empty_tradeable_scanner_population`: only held/protected symbols remain. They preserve exit
    ownership but are not evidence that the entry scanner was restored.
- `rest_warmed != evaluated` means **at least one** selected symbol has not supplied a REST bar no
  older than `REST_WARMUP_FRESH_THRESHOLD_SECS`; it does not mean every symbol is missing. One
  halted or thin symbol can hold the fleet indefinitely. There is deliberately no timeout that
  converts missing input into safety.
- To clear a persistent `rest_warmup_incomplete` hold safely:
  1. Read `warmup_pending_symbols=` from the marker (also published in the isolated v2 state
     payload) and verify each name against the current scanner/watchlist.
  2. If a name is still scanner-selected or held, repair REST delivery and require a later
     `[V2-REST-WARMED] schwab_v2 REST warmup complete for <SYMBOL>` followed by
     `[V2-BOOT-RESTORE] restoration_complete=1`. A held-position symbol must remain subscribed so
     exits continue; do not remove it to clear the entry gate.
  3. If a name has departed the scanner, wait for the next current scanner snapshot to remove it
     naturally. Before any operator-directed scanner removal, prove it has no broker position, no
     open managed row, and no working order using the same broker/managed/working-order preflight
     surfaces used for a v2 restart. Then wait for the next snapshot and the complete marker.
  4. Never clear the hold by toggling the safety flag, repeatedly restarting, or treating elapsed
     time as restored evidence. If safe clearance cannot be proven, keep entries held and escalate
     to the operator.

## TradingView Alerts Restart

Use when:

- only TradingView alert automation code changed
- the watchlist-to-alert mirror is stalled
- you need to refresh the browser automation profile or env values for alert creation

Expected impact:

- existing broker positions and OMS state are unaffected
- new TradingView alerts may lag until the sidecar catches up from `strategy-state`
- on restart, the service bootstraps from the latest `strategy-state` snapshot and then resumes stream consumption

Command:

```bash
sudo systemctl restart project-mai-tai-tv-alerts.service
```

Post-checks:

1. `sudo systemctl status project-mai-tai-tv-alerts.service --no-pager` shows active.
2. `sudo tail -n 80 /var/log/project-mai-tai/tv-alerts.log` shows no sign-in or selector errors.
3. `/api/overview` still shows `strategy-engine` and `market-data-gateway` healthy.
4. The alert service status endpoint or logs show the current watchlist syncing again.

## OMS Restart

Use when:

- only OMS or broker adapter code changed
- you need to rotate broker credentials or env values

Important limitation:

- `oms-risk` reads new strategy intents after restart and does not safely queue intents emitted while it is down
- this means `strategy-engine` must be stopped first

Procedure:

1. Stop new bot intents:

```bash
sudo systemctl stop project-mai-tai-strategy.service
```

1. Wait for OMS to drain:

- refresh `/api/orders`
- refresh `/api/positions`
- confirm no `pending`, `submitted`, or `accepted` intents remain
- confirm no cancel/replace/fill workflow is still in progress

1. Restart OMS:

```bash
sudo systemctl restart project-mai-tai-oms.service
```

1. Wait for OMS to become healthy and repopulate broker-account positions.
2. Start strategy again:

```bash
sudo systemctl start project-mai-tai-strategy.service
```

Post-checks:

1. `/api/overview` shows `project-mai-tai-oms.service` healthy.
2. `/api/positions` shows `account_positions` repopulated.
3. `/api/orders` shows no unexpected new rejected or duplicate orders.
4. `/api/reconciliation` stays clean or only shows pre-existing issues.
5. `/api/bots` shows strategy service healthy after it is started again.

Operational note:

- do not try to queue bot orders during OMS downtime
- let the bots recalculate after restart instead of trying to send stale pre-restart intent decisions

## Market Data Restart

Use when:

- only market-data code changed
- trade/quote stream is stale
- subscriptions look broken

Important limitation:

- the gateway restarts from new subscription events only
- dynamic subscriptions are safest when `strategy-engine` is restarted after the gateway

Procedure:

1. Stop strategy first so no new trade decisions are generated during the market-data interruption:

```bash
sudo systemctl stop project-mai-tai-strategy.service
```

1. Confirm OMS is quiet:

- no new intents arriving
- no in-flight order workflow you are waiting on

1. Restart market data:

```bash
sudo systemctl restart project-mai-tai-market-data.service
```

1. Wait until market data is healthy again.
2. Start strategy again so subscriptions and watchlists rebuild from a clean point:

```bash
sudo systemctl start project-mai-tai-strategy.service
```

Post-checks:

1. `/api/overview` shows `project-mai-tai-market-data.service` healthy.
2. `/api/scanner` shows live scanner data rather than only restored data.
3. `/api/scanner` or `/api/overview` shows active subscription symbols rebuilding.
4. `/api/bots` shows watchlists repopulating.
5. `/api/reconciliation` remains clean.

If scanner remains restored/idle after the restart:

- inspect `market-data.log` and `strategy.log`
- verify the strategy service was restarted after market data
- do not assume subscriptions are healthy until live rows return

## Full Stack Restart

Do not use this during market hours:

```bash
ops/systemd/restart_all.sh
```

Use full-stack restart only when:

- market is closed
- the account is flat
- no manual operator workflow is in progress

## Abort Conditions

Stop the restart procedure and reassess if any of these are true:

- open position exists and the bot/runtime state already looks inconsistent
- a broker cancel is pending
- a fill just arrived and positions are still updating
- reconciliation turns critical after a partial restart
- strategy runtime comes back empty while broker/account positions remain open

## Immediate Recovery After a Bad Restart

If a restart causes uncertainty:

1. Keep `project-mai-tai-strategy.service` stopped.
2. Leave `project-mai-tai-oms.service` running so broker/account sync continues.
3. Review `/api/orders`, `/api/positions`, and `/api/reconciliation`.
4. Inspect `strategy.log`, `oms.log`, and `market-data.log`.
5. Do not resume strategy until open positions and account positions are understood.

## Redis OOM Recovery

Use this when `/health` shows `redis_connected=false` and `redis-server`
crash-loops while loading old cache state.

Symptoms:

- `sudo systemctl status redis-server` shows `Status: "Redis is loading..."`
- `sudo journalctl -u redis-server -n 100 -l --no-pager` shows `OOM killer`
or repeated `status=9/KILL`

Safe recovery:

```bash
sudo systemctl stop project-mai-tai-strategy.service
sudo systemctl stop project-mai-tai-control.service
sudo systemctl stop redis-server
sudo mkdir -p /var/lib/redis/backup
sudo mv /var/lib/redis/dump.rdb /var/lib/redis/backup/dump.rdb.$(date +%Y%m%d-%H%M%S)
sudo systemctl start redis-server
redis-cli ping
sudo systemctl restart project-mai-tai-control.service
sudo systemctl restart project-mai-tai-strategy.service
curl -fsS http://127.0.0.1:8100/health
```

Notes:

- move the old `dump.rdb`; do not delete it blindly
- Postgres remains the source of truth for orders, fills, positions, and
dashboard snapshots
- Redis should be treated as a transient cache/event bus only

## Follow-Up Improvement Gap

This runbook exists because restart recovery is not fully automated yet.

The main missing capabilities are:

- strategy runtime rehydration from persisted open positions and pending order state
- safe intent buffering or replay across OMS restarts
- safer subscription re-seeding after isolated market-data restarts
- a tested restart-with-open-positions validation pass
