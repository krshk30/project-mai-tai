# 2026-10-05 evening: one v2 restart, two switches

**DRAFT BLOCKED: required proof (b) FAILS. Do not execute or schedule this plan.**
This single document supersedes the folded three-PM-flag plan and the conditional
OWNMIX1/RPGSTUCK1 evening draft (including 13a05090). It records sequencing,
not an operator exact-SHA GO or a waiver. No executable runner/timer is staged.

## Scope

After 20:00 ET October5 and observed rotation: only one
project-mai-tai-schwab-1m-v2.service restart, once pinned #1092 is merged and
the operator names exact application/plan SHAs. Merge order: #1091 (already
a80b51816abf0aefc269f0fdfc473468fd3fe62c, dark), then #1092. BOX_SHA=
e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89. APPROVED_SHA remains unset pending
pin/merge/GO. No OWNMIX1, RPGSTUCK1 or GAPKEEP1 in this install.

Exactly TWO primary EnvironmentFile values may change:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=false
```

PM_FLIP_WAIT=false, PM_REST_REPRICE=false remain dark, NFQ1=true and
GAP_HOLD=true unchanged. Verify, do not edit those values. No orb-paper.env edit.
Sizing600/300/1000, target5.0/stop8.0/floorfalse, bands, offset, exits unchanged.

OMS does NOT need a restart merely to stop NEW admission: v2 will stamp no RPG
handoff on new legacy cancel intents. OMS's already loaded value may stay true;
the catalog owns that flag on v2 only. But OFF is NOT a durable-ticket rollback:
OMS retry/admission and v2 restoration honor existing tickets regardless of flag.
Restarting OMS with OFF would not prove clearance either. No extra restart,
ticket deletion, relabelling or database edit is authorized.

## Mandatory proof ledger

| Proof | Result before execution | Evidence and limits |
|---|---|---|
| (a) OFF uses legacy cancel then next pass | PASS with a clean journal | test_rpg_flag_off_new_reprice_retains_legacy_cancel_then_next_pass; test_tonight_rpg_off_clean_journal_legacy_cancel_then_next_pass, first/reclaim, exact six flags; OMS still ON |
| (b) refused/held_unknown cannot own after restart | **FAIL, blocks plan** | test_current_rpg_off_startup_still_restores_recorded_blocking_ticket: four actual APUS/VEEA tickets; real _rpg_handoff_pass restores them, refused same-segment and held_unknown still own. Characterization PASS is NOT safety PASS |
| (c) NFQ1 ON/RPG OFF | PASS in recorded-price clean-journal composition | test_shared_v2_reprice_retires_queued_nfq_generation_exactly_once[tonight_handoff_off], four cases; old/new retries submit one buy; OMS loaded ON. Cache/bar scaffolding and simulated execution disclosed; live behavior UNEXERCISED |

Startup unconditionally polls HandoffJournal.jobs(), registers terminal jobs via
rpg_handoff_authorization, and _rpg_entry_owned ignores OFF. OMS ordinary-open
also blocks held_unknown regardless of flag. Flat books/zero working orders do
not prove the journal clear. Reviewer/operator disposition required BEFORE an
executable reviewed plan. Do not fold RPGSTUCK1 into tonight to force proof PASS.

## Intended sequence after blocker resolution and exact-SHA GO

1. Verify no concurrent deploy, exact clean BOX_SHA, approved ancestry/path
   allowlist and editable import path; journal every PID/start/NRestarts.
   Executable allowlist: settings, v2 strategy, v2 service, entry_gate, isolated
   flag catalog; docs/tests allowed. No unrelated source or migration.
2. Fresh direct positions AND working-order reads on BOTH live accounts;
   zero managed rows and zero nonzero virtual positions before writes/restart.
   Exposure/UNKNOWN/orders = STOP. No historical manual-close exception.
3. Require proof(b) resolved under separate review with exact bounded journal
   and strategy-state evidence, not an empty in-memory cache or assumed segment.
4. Exclusive backups/hashes/modes of env, isolated catalog and preopen.sh.
   Reject duplicate env definitions; change only the two named values. Confirm
   retained/dark values. Advance only to operator-named APPROVED_SHA, refresh
   editable runtime as trader, repeat clean-tree/import checks.
5. Fresh flat/working-order proof; blocking v2 preflight with pipefail and real
   rc0. Armed set blocks pending operator-named live override/Bug2 acceptance;
   no retry loop or automatic override. Then exactly ONE restart, attended:

```bash
sudo /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh
sudo systemctl restart project-mai-tai-schwab-1m-v2.service
sudo systemctl is-active --quiet project-mai-tai-schwab-1m-v2.service
sudo systemctl show project-mai-tai-schwab-1m-v2.service \
  -p MainPID -p ActiveEnterTimestamp -p NRestarts
```

No OMS/strategy/gateway/ORB/orb-schwab/paper/control/reconciler/Redis/Postgres
restart, watch install, migration or guard action. Failure: STOP/page actual
states/seek instructions. No rollback or extra restart pre-authorized.
After20:00 restart creates no bar hole (Schwab bars stop20:00), not delivery PASS.

## Close-out and next-session proof

Read by KEY from NEW v2 /proc: printtrue, handofffalse, flipfalse, pmrestfalse,
NFQtrue, gapholdtrue; retain sizing600/300/1000, target5/stop8/floorfalse,
retry-one/flip-owner, offset/band/reprice0.5, entry07:00-15:45, reclaimfalse,
polygon_30s=false. Name unchanged OMS loaded handoff value; do not claim reload.
The two changed keys must be explicit in the new PID's environment. For an
unchanged dark key absent from /proc, journal ABSENT plus the approved SHA's
default FALSE and the checker source=default; never invent a /proc value or
add a third env change. Do not read shared on-disk env as the old OMS's settings.
Every other identity unchanged; zero new-process Traceback; NRestarts0; exact
warm-up/BOOT-HOLD population. Overnight held = expected by design, not release.

Install/hash only isolated expected_flags.json from APPROVED_SHA.
Combined FLAGGATE **143/143 =135 boolean service checks+8 numeric checks**;
standalone numeric8/8. Paste actual outcome/denominator, UNKNOWN not green.
Catalog printtrue/flipfalse/pmrestfalse/handofffalse.

Backup/re-pin preopen.sh ONCE: 2026-10-06, APPROVED_SHA/new v2 identity, preserve
all verified unchanged pins/paths and0700, bash -n/diff/hash. REPORT remains
v2-restart-evidence-20261006.md. Recheck prior hash
2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3.
Do not run tomorrow's gate early. Journal deployments-20261005.md with exact
SHAs, fresh proofs, two-key diff, backups/hashes, /proc/census/logs/catalog/gates/
preopen. No COMPLETE until every mandatory proof, including(b), is established.

Codex-2: Tuesday10-06 07:00/first-nonempty BOOT-HOLD release; claude-1:
bar-continuity after first bars (~07:10). Strategy NOT restarted: no scanner
warm-up validation triggered; adding its restart voids scope.
First stream/REST crosses: confirming ask/source/age, both600/300, once-only
latch; SAIQ stray no state. PMFLIP/PMREST OFF retains latch/disarm/rearm.
M19 software rest at09:30 may still queue its legacy Webull RTH leg: unchanged,
not represented as fixed. Delivery/fills, ORB and future activations unexercised.
