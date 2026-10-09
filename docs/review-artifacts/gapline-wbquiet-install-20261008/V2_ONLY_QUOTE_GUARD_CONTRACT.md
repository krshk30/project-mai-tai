# V2-only quote-guard followup: local, application unbound

Only the exact reviewer-pinned, CI-green, merged application M supplied by the
parent can populate release.json. No candidate, manifest hash, staging, install
approval or current trading-gate PASS is asserted by this local build.

Existing dependencies live in hotfix-job. v2_only.py is the sole new entry point;
repin_preopen.py gains an explicit --v2-followup receipt. The old sealed r3 job
is immutable and never rerun. A new exclusive package/attempt holds all new bytes,
raw command outputs, hashes and the deployment lock. The manifest binds its exact
dependency blobs and prior failed receipts. No new approval latch is introduced.

Read-only baseline observed before this build: OMS2094823, strategy2094834,
v22096131, control2075090, ORB-Schwab2121782, all active/NRestarts0. Current
preopen885cbe8b714ee72cab7c8726e77fe74a641235f61ea9a6ca3e69167b07e3b83c.
This is identity evidence only, not a fresh trading admission for execution.

## Literal sequence after exact M is supplied

1. Verify new package hashes and immutable old ABORT/proof/seal receipt hashes;
   take the same nonblocking deployment lock. Capture current identities, flags,
   environment bytes and read-only telemetry/log cursors with microsecond starts.
2. Run the existing gate_readonly.py: flat BOT book, zero working orders, zero
   open rows/in-flight work; retain stdout/stderr, rc2 retries three times at60s.
   Run the native preflight_v2_restart.sh. Only the existing authorized clock
   proxy applies before18:00; no armed override or gate-policy edit.
3. Verify the immutable exact-M remote ref exists before the first application
   write. Repeat fresh trading/native reads and unchanged identity/flag evidence.
4. Sole deployment command, inside the existing tested health_view context:

```sh
sudo -u trader env MAI_TAI_EXPECTED_SHA="$M" \
  MAI_TAI_RUN_MIGRATIONS=0 MAI_TAI_ALLOW_LIVE_RESTART=0 \
  APP_HEALTH_URL="$TESTED_HEALTH_VIEW_URL" \
  bash /home/trader/project-mai-tai/ops/systemd/deploy_service.sh \
  /home/trader/project-mai-tai "$EXACT_RELEASE_REF" schwab-1m-v2
```

No OMS, strategy, control, ORB, paper or gateway restart; no migration,
environment, catalog, archival, DB or Redis mutation. The v2 deploy retains its
existing60s health/identity SLA and clean systemd stop semantics, without a
required CancelledError log signature. A failed start or real gate condition
stops and reports actual states; no automatic recovery or rollback.

5. Verify only v2 changed identity, all process settings/environment unchanged,
   and other service identities unchanged. Read the trading gate again. Observe
   ten actual minutes using only new v2 PID/start and original log cursor across
   rotation. Keep errors, unknown continuity, flags, scanner and DB-rate results
   unchanged in meaning; no old error waiver/filter or fake clean proof.
6. Copy prior snapshot/record/journal inputs to new exclusive paths, retaining
   the actual0022->0023 migration receipt and original FAIL/ABORT. The official
   collector retains its five-service cumulative restart group. Latest actual
   service_actions is only schwab-1m-v2 restarted. Other pins remain unchanged.
   Reuse hash-bound parent ORB restart/retirement evidence; old Redis ACK stays
   historical/inactive and retired paper ORB is never reintroduced.
7. Invoke new repin_preopen.py --approved-sha M --snapshot <cumulative-original>
   --install-record <cumulative-record> --v2-followup <actual-v2-receipt>
   --orb-restart <preserved-parent-receipt> --retirement <preserved-retirement>
   --line-enabled true --receipt <exclusive-repin-receipt>.
   Atomic backups preserve ownership/root modes and publish runtime hashes last.
   Run real daily.verify_runtime, official preopen checks-only and flag audit;
   retain raw nonzero/UNKNOWN, including inactive momentum-paper.

Driver completion is FINISHED.json with actual proof assessment, never a global
COMPLETE/PASS waiver. Historical ABORT remains unchanged even if the new v2
guard's own observation is clean. Official cumulative evidence can still fail
on prior v2 errors; evening clock/paper admission and backfill UNKNOWN remain
reported honestly. No new trading gate is derived from these telemetry limits.

## Local validation

370 mechanics tests PASS, seven platform skips; focused new v2-only tests27PASS;
Ruff clean. Isolated tests execute the real daily runtime verifier after the
five-service cumulative re-pin, reject untouched-identity/current-pin/retired-ACK
drift, preserve backup/hash semantics, and run the literal driver with controlled
commands to prove one v2 deployment and retained failed proof. No global source
full-suite parity or live execution claim.

Six assertion-level mutations RED: untouched identity check removed (4 failures),
current-pin binding removed (1), retired ACK guard removed (1), deploy failure
accepted (1), observation restart scope expanded (1), OMS substituted as the
deployment target (2). These were isolated local mutations, not deployed changes.
