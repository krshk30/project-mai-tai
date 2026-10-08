# GAPLINE1 + WBQUIET1: October 8 after-close install

## Authority and candidate

Operator standing GO: deploy reviewed work after the close when flat. Reviewer
2026-10-08 STAGE TONIGHT names GAPLINE1 #1126 and WBQUIET1 #1124. Mechanics
may be fixed, tested and repeated without a new approval round trip; trading
code, trading conditions, and unreviewed recovery are not waived.

- GAPLINE1 pinned 00bd0c64325ec25c11642d913c4a0ae2b35aad0f; merge commit
  1ed10831508e9a251aa3440a973f49c8bb2c18c9.
- WBQUIET1 pinned b40212e420ea3eb6312ab47df83aa6bb2042daf1; its second
  Validate is being rerun unchanged. GitHub refuses the unchanged pinned head
  as BEHIND after GAPLINE1 merged. No administrative bypass or pinned-head
  change is authorized by this document. Final M is not yet available.
- RPGRETIRE1 is optional only if tested, reviewed and pinned by 15:00 ET;
  isolated code lane. No stale handoff snapshot is edited or unarchived.

This plan is NOT staged, scheduled or COMPLETE until a receipt says so.
make_release.py refuses an incomplete runtime package. The final committed
plan, exact M, manifest and approval are bound together. An immutable remote
release branch points to M, so deploy_service.sh cannot follow a moving main.
The operator's standing GO is recorded as such, not represented as a new
independent exact-byte review.

## Literal Sequence

The date fence is October 8, first application write after 16:00 ET; a held
position waits for its close. No 18:00/20:00 fence or clock override is added.
After the first write, advancing clock time alone does not abort the sequence.
No migration, ledger write, archival edit, gateway/Redis/Postgres restart,
paper restart, ORB restart, control restart or automatic recovery is included.

1. Verify release.json, approval.json, every staged artifact hash; take the
   nonblocking deployment lock; the re-pin helper takes the daily-preopen
   lock itself (not a conflicting second acquisition by its parent). Read both direct broker
   positions and working orders and the repeatable-read database book. Record
   raw stdout and stderr on every attempt. rc 2 means unreadable: at most three
   reads, 60 seconds apart. rc 1 means measured work: wait without an app write.
   AIXI Webull 135 is bot-owned and MUST block while open.
2. Confirm clean box baseline, exact release ref M and approved source blobs.
   Capture actual pre-restart identities, official snapshot, log offsets,
   bounded Redis owner/eviction state and PostgreSQL transaction counters.
3. Fresh trading gate immediately before the first write. Exclusively claim
   the attempt. Back up env and box catalog, recording old/new hashes.
   Only add/set GAP_LINE_CARRY=true. LINE_CHART_RESTORATION=false and
   ATR_REPRICE_HANDOFF=false stay explicit and unchanged. All other env bytes
   remain unchanged, including RETRY_ONE=true/MAX_RETRIES=0.
4. Add the approved GAP catalog row to the box copy. Correct only its LINE
   expectation to false to match the already-authorized 07:28 rollback.
   Do not change unrelated ORB ownership or other catalog policy.
5. Fresh trading gate; execute exactly:

   ```sh
   sudo -u trader env MAI_TAI_EXPECTED_SHA="$M" MAI_TAI_RUN_MIGRATIONS=0 \
     MAI_TAI_ALLOW_LIVE_RESTART=0 bash /home/trader/project-mai-tai/ops/systemd/deploy_service.sh \
     /home/trader/project-mai-tai "$IMMUTABLE_RELEASE_BRANCH" oms
   ```

   The repository script refreshes the runtime, stops strategy, runs its
   native OMS preflight, restarts OMS, then starts strategy. It also installs
   bootstrap units and derives orb-paper.env as designed; no extra ORB start
   or restart is added. Runtime migration is explicitly disabled.
6. Fresh trading gate; same command with target schwab-1m-v2. The script's
   own identity and post-health checks remain intact. A clean systemd stop
   is proof: no required CancelledError line or permission-mode self-stop.
7. Record actual new OMS/v2/strategy PIDs and timestamps. Verify active,
   NRestarts=0, exact code and process flags; fresh trading read. Observe ten
   minutes, preserving live and rotated log paths/inodes/ranges. Record zero
   tracebacks/errors, WBQUIET shadow/pass coverage, sync duration and DB tx/s,
   scanner status, actual restart bar-hole minutes and Redis before/after.
   Coverage/drop/after-hours UNKNOWN is not converted into PASS.
8. Run official expected_flags_check with box boolean and numeric catalogs.
   Preserve by-design inactive-paper UNKNOWN and any two explicitly named
   legacy ORB catalog mismatches until the separate catalog PR lands; all
   other mismatches/unreadables are reported. Do not restart paper for green.
9. Write an actual whole-fleet install record bound to the actual official
   snapshot and sealed actions. Re-pin preopen SHA/PIDs/starts, SNAPSHOT and
   INSTALL_RECORD; only OMS/v2/strategy are newly restarted. Update daily
   application bindings and their exact runtime hashes, preserving the dynamic
   date, dynamic daily-paper shape, and the narrow Redis-upgrade acknowledgement.
   No non-restarted flag arguments; LINE false, RPG false, GAP true. Backups,
   one diff and old/new hashes are in preopen-repin.json. bash -n only; do not
   invoke tomorrow's daily gate early. The existing weekday 06:20 timer remains.
10. Seal COMPLETE only after all requested measured proofs and bookkeeping.
    Journal and post the handoff C-row with plan/manifest/attempt hashes.
    Stop seals ABORT with actual states and pages; it starts nothing and does
    not re-run a partly applied service deployment.

## Real-Time Gates

| Read | Requirement | Effect |
| --- | --- | --- |
| Schwab direct positions | Configured account identity and complete fresh response; no bot holding | Holding waits; unreadable bounded retries |
| Webull direct positions | Configured account identity, complete pagination; no bot holding including AIXI 135 | Same; never read failure as flat |
| Broker working orders | Fresh direct/list-today complete states on both accounts | Any working or unknown order blocks |
| Managed/virtual book | Zero open managed rows / nonzero virtual rows | Blocks, no quantity or net-zero waiver |
| Intent book | Zero unconfirmed/in-flight dispatches, positive terminal proof for local aborts | Blocks; no age expiry |
| Native OMS preflight | Repository script executes it unchanged | Report exact refusing line; no omitted gate |
| Process, logs and flags | Measured new identities and requested post-install evidence | Failed start or process drift pages; no recovery |

MI +180 / NXL +2 ledger discrepancies are decided matters, not new trading
holdings. No ledger mutation or new exception is sought. Any operator-only
holding admission must prove ZERO session bot orders AND fills; a net-zero
round trip is not that proof. Working orders and open rows are never waived.

## Schedule and Evidence

Named one-shot/timer: project-mai-tai-gapline-wbquiet-20261008.service/.timer.
Minute checks after 16:00 ET on October 8 only, Persistent=false; no future-day
permission. Runtime seals prevent any duplicate deploy. Package destination:
/home/trader/after-hours/2026-10-08/gapline-wbquiet/job. Timer installation alone
does not claim preparation, first stop or install COMPLETE.

Tomorrow scanner acceptance, live first-gap outcomes and decision equivalence
of hypothetical Webull cadence remain UNMEASURED until observed. WBQUIET1 is
log-only; no live cadence, order, cancel or sizing rule changes.
