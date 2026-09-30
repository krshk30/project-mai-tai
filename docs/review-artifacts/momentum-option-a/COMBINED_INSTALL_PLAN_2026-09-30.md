# Combined 2026-09-30 install plan: FLAGGATE, restart evidence, Option A

Status: **PLAN ONLY; no production write or restart authorized.** Request one
operator GO for the exact merged `origin/main` SHA
`1d70172609fe0f397667101792d4ed6a2e94f876` (#1070, #1073, #1072).
If main moves, the source differs, or the reviewed baseline/stop procedure is
not approved, stop and obtain a new exact-SHA review and GO. Do not include
the unconfirmed ORB Schwab MACD wait-card, WBREAD1, or a watch reinstall.

## 1. Read-only preflight and hard stops

1. Confirm the exact SHA and reviewed PR heads/pins; verify the box checkout is
   clean, no other install is in progress, all current service identities and
   `NRestarts` are recorded, and Redis, gateway, v2, scanner/strategy, OMS and
   broker connections are healthy. A Git merge is not an install.
2. Obtain fresh broker reads proving **both live accounts flat**, zero open
   managed rows on `live:schwab_1m_v2` and `live:orb`, and zero armed segments.
   Repeat immediately before each service restart. A failed, stale, ambiguous,
   or unavailable read blocks the restart; a database zero alone is not broker
   flatness. Do not override a gate or restart while a position is held.
3. Before touching the shared gateway, preserve its current subscription
   stream/owner-hash evidence and prove the retained stream can reconstruct
   **both scanner and v2 consumer owners** (including explicit empty replace
   events where appropriate). Record their event IDs and current union. If
   either owner is absent, the Redis read is unavailable, or a replay would
   remove a scanner/v2-owned symbol, **do not restart the gateway**. Obtain
   fresh owner replaces or a separately reviewed migration plan first.
4. Require independent approval of the accepted-with-gaps inactive control,
   its numerical thresholds and the first-session stop coverage. If a required
   treatment signal cannot be observed, stop the whole install before the
   first write; a passing code review alone is not a measured treatment plan.
5. Capture hashes/backups of `/home/trader/preopen.sh`, the isolated files in
   `/home/trader/restart_evidence/`, the current checkout and service units;
   capture the existing Sunday pre-restart snapshot and the 09-29 fleet
   journal. No flag/env change, database migration, unrelated service
   restart, Momentum replay, or live Momentum order route is in scope.

## 2. One scoped install under that GO

1. Advance only the clean box checkout and required runtime to the exact SHA,
   after reviewing the runtime refresh for side effects on unrelated units.
   Do **not** use `deploy_main.sh` or `deploy_service.sh market-data`: the latter
   stops/restarts strategy as a companion, contrary to this plan's two-service
   restart scope. Use a reviewed scoped procedure that restarts **only**
   `project-mai-tai-market-data.service` and
   `project-mai-tai-momentum-paper.service`. Stop the old paper process before
   the gateway restart, then start the new paper process only after gateway
   health and owner restoration pass. Recheck broker flatness before each
   restart. Leave OMS, v2, strategy, ORB, and every other unit untouched.
2. Stage isolated `/home/trader/restart_evidence/` copies from this exact SHA:
   `ops/health/expected_flags.json`, `expected_flags_check.py`,
   `preopen_restart_evidence.sh` (#1070), and
   `v2_restart_evidence.py` (#1073). Compare each installed SHA-256 against
   its Git blob. Use these isolated copies in the wrapper rather than
   assuming the updated checkout's ops files are installed automatically.
3. Write a structured install record bound to the existing snapshot's exact
   `captured_at_utc`, with a source fleet journal and **every** unit monitored
   by the restart gate classified as `restarted`, `newly_installed`, or
   `deliberately_untouched`. Verify evidence for `control`, `market-capture`,
   `reconciler`, and `tv-alerts`; never infer untouched from silence. Account
   for the 09-29 OMS/strategy/v2/ORB restarts and new `orb-schwab`, plus this
   gateway restart, against the snapshot. The paper service is journaled
   separately if it is not in the gate's monitored service list. A missing or
   conflicting classification is UNKNOWN and blocks readiness. The reviewed
   restart reporter requires the prior v2 restart declaration; do not create
   a fictitious v2 restart tonight merely to make the report pass.
4. Back up `/home/trader/preopen.sh`; update it **once** for 2026-10-01, the
   final checkout SHA and freshly verified OMS/strategy/v2 PIDs/start times.
   Point its restart-evidence call at the isolated checker with the structured
   `--install-record`, the verified `--restarted`/`--new-service` declarations
   (including `orb-schwab`), and the existing required flags. Source the
   isolated router and add the reviewed
   `preopen_check_expected_flags "$REPO/.venv/bin/python"` call with the
   isolated checker/catalog before the final verdict. Preserve other checks.
   Log the exact before/after diff, backup path, `bash -n`, and new SHA-256.
5. Run the revised gate read-only as `trader` (or root if permissions require)
   against actual running processes. Require exactly one consistent final call
   from each checker: FLAGGATE `PASS` with the full catalog checked, and
   restart evidence `PASS` or a genuine `EXPECTED BY DESIGN` N/A. A mismatch
   is REAL FAILURE; unreadable evidence or a return-code/final-call mismatch
   is UNKNOWN. Neither is an install success. Arrange a one-shot read-only
   06:20 ET 2026-10-01 gate with the verified script hash; no service restart
   may be used to force green.

## 3. Post-restart proof and first-session stop coverage

1. Record old/new PIDs, exact start times, `NRestarts`, box checkout SHA,
   service logs, `/proc` flags and the unchanged PIDs of all other units.
   Attribute the final SHA only to the two newly started processes; an
   untouched process does not acquire new code merely because checkout moved.
   Require the gateway healthy, normal snapshot/heartbeat cadence, restored
   scanner and v2 owner sets, and a union containing every symbol still owned
   by either. Require Momentum owns at most 16 candidates, opens **no** second
   Massive socket, has no broker route, and receives condition-provenance trade
   ticks. No observed paper intent may be described as a live fill.
2. The gateway log rotates by `copytruncate` at about **20:00:06 ET**. A
   truncation makes the 1008 byte-offset sampler UNKNOWN. Start the reviewed
   one-second 1008 sampler and treatment samplers **after both 20:00:06 ET and
   the gateway restart**, and verify this day's rotation has actually
   completed before fixing the initial offset. Record the sampler PID, start
   time, treatment date, log device/inode, initial byte offset, raw JSONL path
   and expected sample count. Confirm them still running at **06:30 ET** before
   the first full 07:00-09:40 ET treatment window. Late start, unreadable log, rotation,
   truncation, or missing samples is UNKNOWN and blocks a healthy treatment
   verdict; it is never reported as zero 1008s.
3. Apply the independently reviewed
   `FIRST_SESSION_PROTOCOL.md` and
   `INACTIVE_CONTROL_2026-09-30.md` without tuning thresholds after treatment
   begins. Any new **gateway** 1008 stops only the paper service; measured
   trading slowdown uses the pre-registered v2, OMS, heartbeat and snapshot
   rules. One-minute load over 3.5 is a warning, not a stop. Record Momentum
   union additions and historical 30/60-second REST attempts per session.
   If a stop fires, stop only paper, page low-priority, and verify its owner
   hash is `[]`, the post-stop union removes Momentum-only symbols, and a
   fresh gateway heartbeat matches the remaining owners. If release fails,
   publish only the approved empty `momentum-paper` replace and reverify;
   blind evidence remains UNKNOWN and pages. A service-stop page drill (board
   row 45) is **excluded** unless the exact-SHA GO explicitly approves it.

## 4. Journal and decision

Journal the operator GO, preflight denominators and raw paths, retained-owner
proof, every file blob/installed hash and backup, the structured install record,
service identities, preopen diff/hash, gate return codes **with their actual
Final calls**, sampler offsets/coverage, and any hard stop. Report the result
as REAL FAILURE, EXPECTED BY DESIGN, or UNKNOWN after checking the relevant
code/design, not by repeating a tool's red line. No automatic rollback or
additional restart is authorized by this plan. The first-session result is
`STOPPED`, `OBSERVED`, or `UNKNOWN`, not a Momentum P&L or live-trading verdict.
