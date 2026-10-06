# Attempt 4: services installed, close-out STOP

As of 2026-10-05 20:57 ET, Codex-2's own reads. This is **NOT COMPLETE**.
The approved service sequence and migration ran once. All three services are
active; close-out stopped before isolated catalog installation and preopen re-pin.
No recovery, rollback, extra restart, ledger write or ticket edit was performed.

## Exact bytes and authority

- Application: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, clean.
- Tree: `ee6f058c248eeebf475fd392845eadfef7af59eb`.
- Actual runtime plan: `490e098c4ae987dbfd2a702465e0b3583d2e4c75`.
- Branch: `codex/1005-owned-entry-reprice-install-plan`.
- Release SHA256: `53bc8873ef6f9f13ac40db57c162a047180f64e17e9ae4edd3d6dc52e4f066f2`.
- Helper SHA256: `658ca1c0ed85c18b9f898643dc84fc49ac4826ffa06f38c8a471f9e0f8565670`.
- Seven staged runtime artifacts verified on box before first call.
- Authority: operator 17:30 GO, reviewer 20:40 exact-shape admission and
  20:45 standing non-trading mechanics authority. No new independent exact-byte
  review is claimed for this mechanics revision.
- Tests: 121 PASS (53 existing allowance, 29 off-hours, 39 runner/census/TZ);
  new 12 and existing 18 allowance mutations RED; Ruff and bash syntax PASS.
- Full real-time read-only rehearsal: 20:44:07-20:45:23 ET, rc0.
  Raw receipt SHA256 `84ea738e750963e0d15e9544c39aaf5827d069c18640c21ec5eee61535556c7e`.

Mechanics changes: admit only the specified fresh, complete, healthy v2
off-hours degraded shape in an in-memory preflight copy; retain every MI/NXL
guard; first-stop deadline 23:00 ET; regenerate manifest and approval under
standing authority. Prior bounded retries, serial broker reads, native tool
timezone and explicit ET runner clock remain. Trading code, app SHA, migration,
switch set and recovery policy were not changed.

## Actual stop and service evidence

Runner started 20:48:07.367 ET and stopped **20:54:02.956 ET**, rc2,
stage `closeout-no-extra-restarts`. Trap page returned HTTP200 and started nothing.

Remote attempt directory:
`/home/trader/after-hours/2026-10-05/owned-entry-rpg-all-on-2045-standing-job/attempt-1730-go`.

`runner.log` SHA256:
`0c6f6f746e77659d476ad28f8e948598747ed5b0c53ab1a0b38e8add1eb9239d`.

Journal: `/home/trader/fleet_health/deployments-20261005.md`.
Its terminal line is `STOP rc=2 stage=closeout-no-extra-restarts`, not COMPLETE.

| Service | New PID | ExecMainStartTimestamp (UTC) | State / NRestarts |
| --- | --- | --- | --- |
| OMS | 362892 | Tue 2026-10-06 00:53:08 UTC | active / 0 |
| schwab-1m-v2 | 362945 | Tue 2026-10-06 00:53:31 UTC | active / 0 |
| strategy | 363061 | Tue 2026-10-06 00:53:53 UTC | active / 0 |

Untouched identities verified in final checkpoint: gateway2907, control2916,
capture2917, reconciler2918, ORB30225, orb-schwab27173; paper0/inactive.
Migration revision is `20261005_0022` (two nullable columns, no backfill).

Read-only post-stop `/proc` proof: print-confirm, flip-wait, pm-rest-reprice and
ATR hand-off all literal true on BOTH OMS362892 and v2362945.
Receipt `/tmp/oct5-standing-stop-evidence/proc-four-keys.json`, SHA256
`784baf1a636dd77c16828fe74c2992a9babd82b7136f06475d03db8f037b7916`.

## Why close-out is not complete

1. `bar-continuity.json`: `live_at_stop=0`, rows/pending/spanning-over90s all
   empty, verdict UNKNOWN. The helper requires a recent live bar even though
   this run is after the 20:00 bar cutoff. No actual bar-hole duration is proven.
   Observed stopped checkpoint00:51:09.736114Z to new start00:53:31Z is
   141.263886 seconds, **not evidence of missed market bars**. No UNKNOWN waiver.
2. Diagnostic `census-after` subsequently refuses
   `old ticket identity changed: 4be7cb2d-406b-56db-b597-3783f6e956ff`.
   Four local-no-wire recoveries (MI4be7cb2d, RETOba108172, APUSee3d0d07,
   VEEAfaa55c1f) replaced cancel-envelope client ids/metadata with original open
   intent ids/metadata. The approved app explicitly performs this in
   `atr_reprice_runtime.py:237-240`, after its persisted-local-open proof.
   This is a checker-contract mismatch to investigate, NOT a silently waived
   census or authorization to change trading code. Full old snapshots are in
   the before/final receipts. No new token appeared.

Read-only SQL activity receipt20:55:45 ET: **zero new buy orders, open intents
and buy fills** since20:49:31.586738 ET. This independent activity read is not a
PASS for the refusing ticket checker.

## Observed ticket dispositions (not a passing census)

Same 14 tokens; full UUIDs and accounts in
`/tmp/oct5-standing-stop-evidence/post-stop-diagnostics.json`.

| Token prefix | Account | Symbol | Before -> after | Reason |
| --- | --- | --- | --- | --- |
| 4be7cb2d | live:orb | MI | held_unknown -> expired | window_closed; local_no_wire |
| 6fb89c93 | live:orb | SCKT | refused -> refused | replacement_terminal_accounted |
| 7c7c5afb | live:schwab_1m_v2 | APUS | refused -> refused | replacement_refused |
| 889889cd | live:orb | SCKT | refused -> refused | replacement_terminal_accounted |
| a007716c | live:schwab_1m_v2 | VEEA | refused -> refused | replacement_refused |
| a9eac442 | live:schwab_1m_v2 | SCKT | refused -> refused | replacement_refused |
| ba108172 | live:orb | RETO | held_unknown -> expired | window_closed; local_no_wire |
| bd6ac0b9 | live:schwab_1m_v2 | APUS | refused -> refused | replacement_refused |
| c539a57f | live:schwab_1m_v2 | MI | refused -> refused | replacement_refused |
| d86d5d38 | live:orb | SCKT | filled -> filled | replacement_fill_accounted |
| ee3d0d07 | live:orb | APUS | held_unknown -> expired | window_closed; local_no_wire |
| faa55c1f | live:orb | VEEA | held_unknown -> expired | window_closed; local_no_wire |
| fbfd692d | live:schwab_1m_v2 | APUS | refused -> refused | replacement_refused |
| ff6464ff | live:schwab_1m_v2 | RETO | held_unknown -> refused | old_rejected_explicit_zero |

## Gate, Redis and log reads

Diagnostic checker used hash-verified **application checkout** checker/catalogs,
not an installed isolated catalog. Actual rc2:
`Final call: UNKNOWN; checked=145/147 mismatches=0 unknown=2`.
Both unknown rows belong to inactive momentum-paper. Numeric8/8 PASS.
`/tmp/oct5-standing-stop-evidence/flaggate.json` preserves raw output and label.
The runner never reached its catalog installation or own `flaggate.json` step.

Redis baseline00:49:05.823857Z: evictions0, used_memory806948736B.
Post-start00:53:55.095802Z: evictions0, used_memory806974160B.
After warm-up00:56:15.623158Z: evictions0, used_memory806871648B.
All five owner fields and `_migration_complete=1` remain; nine stream types
remain streams; union `[APUS,JAGX,MI,OLOX,VEEA]`.
Latest sets: strategy/v2 that five-symbol set; ORB/ORB-Schwab `[VEEA]`; paper`[]`.
These are observed evolving consumer sets, not a claim every set was unchanged.

New-process proof quotes current10-06 UTC cycle files, not rotated copies:
`/var/log/project-mai-tai/oms.log`, `schwab-1m-v2.log`, `strategy.log`.
No traceback in the runner's captured new-process slices; Webull syncok1/failed0
and both account stamps newer than OMS start. Strategy later logged at
00:55:32.733Z `prefilled momentum alert history from 120 snapshot batches`.
V2 BOOT-HOLD remains HELD: restoration_complete0, warmup_pending5 at
00:55:34.904Z; not a release PASS. Morning release/scanner/bar reads remain owed.

## Pending writes and artifacts

No preopen diff or re-pin: file remains mode0700, SHA256
`2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3`, old pins.
Do not call tomorrow's gate ready. Isolated catalog install, install record,
preopen backup/re-pin and COMPLETE journal line remain unperformed.
Continuation needs disposition of the two proof blockers; **no restart is needed
or authorized by this receipt**. No recovery was improvised.

Raw receipt copies are `/tmp/oct5-standing-stop-evidence/`; only selected
non-secret evidence was copied (no env backup/token bytes). Runtime runner.log
hash above matches the remote file. Diagnostic artifacts are local receipts,
not fabricated outputs from the interrupted remote runner.
