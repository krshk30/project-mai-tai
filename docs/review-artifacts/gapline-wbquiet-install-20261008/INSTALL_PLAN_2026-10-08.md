# October 8: exact reviewed B/A/G/H P1 install

## Authority and Candidate
Lane C owns only this plan/job directory. Parent owns landings and handoff.
Latest operator GO covers B1127, A1130, G1134, H1135 on main1e15 plus merged
GAP1126; F1133 is EXCLUDED. I1136 may join only if independently pinned,
reviewed and explicitly in the final set before execution. No non-P1/data lanes.
B landed 4ec93308aab379894b69de0b84622d6ce3a7c756; A/G/H rebased landings and
the final APP are NOT yet supplied. That B SHA is NOT a substitute final APP.
No staging, deployment, env/schema/service write or approval hash is claimed.

Release generation binds full APP/plan/box SHAs, the exact explicit candidate
set, per-PR reviewed base/head/landing trees and binary diffs, committed pin
verification and Validate receipts. Required PRs:1126/1127/1130/1134/1135.
Only optional1136 is allowed; F and future WB data additions refuse packaging.
Every artifact is from committed blobs, never dirty files or moving main.
Exact clean box baseline identities need an actual fresh acknowledged read.
No arbitrary PID adoption, archived snapshot/ledger mutation or recovery.

## Literal Sequence
First write is after16:00 ET October8, flat; held work waits for its close.
Native clock gate remains >=18:00 with NO invented override or threshold.
A clock refusal is recorded literally. After the first write, clock
advancement alone never aborts. Real holding/order/unreadability/start or
migration errors remain fail closed.

1. Verify all manifest/approval/artifact bytes, exclusive attempt/nonblocking
   deployment lock, exact clean box checkout, fetched immutable candidate
   ref, source hashes and all actual baseline identities.
2. Before any application write run the read-only helper AND both unmodified
   native OMS/v2 fences. OMS rehearsal is before strategy can be stopped;
   repo OMS deployment still runs that fence after stopping strategy.
   Preserve stdout/stderr/rc/hash for every call. Retry only unreadable rc2,
   three attempts 60s apart; measured blockers never retry as unreadability.
   Read-only snapshots/Redis/log offsets/DB counters and identities remain
   bounded. Re-read trading conditions and identities immediately before write.
3. Back up env and catalog, hashes/atomic writes. Change only LINE=false->true,
   add/activate GAP_LINE_CARRY=true and FALSE_FLIP=true. Retain MIRROR=true,
   SLOTCLEAR fresh-flip/fresh-sell=true and RPG=false; all other bytes unchanged.
   Catalog starts from actual BOX rows: add only reviewed GAP/FALSE rows,
   expected=true; LINE expected=true. Preserve unrelated owners/rulings,
   process population and existing RPG=false. No wholesale repo replacement.
4. Fresh trading gate; run only reviewed additive migration
   sql/migrations/versions/20261008_0023_entry_classification.py,
   revision20261008_0023 from20261005_0022: nullable JSON entry_classification
   on oms_managed_positions. It is NOT upgrade head. Extract exact hash-bound
   APP migration archive into an exclusive attempt directory (no checkout or
   service action); execute Alembic upgrade20261008_0023, record actual revision.
   Existing0023 is read/proven, not re-applied. Unexpected revision/error stops.
   No other schema/ledger/archived-row action or automatic downgrade.
5. Fresh gate before each repo deployment, in order OMS -> v2 -> control:
   OMS script stops/starts strategy itself. No orb-schwab restart, no extra
   strategy restart, no paper retirement write. Literal command for each:

   sudo -u trader env MAI_TAI_EXPECTED_SHA="$APP" MAI_TAI_RUN_MIGRATIONS=0 \
     MAI_TAI_ALLOW_LIVE_RESTART=0 bash /home/trader/project-mai-tai/ops/systemd/deploy_service.sh \
     /home/trader/project-mai-tai "$IMMUTABLE_RELEASE_BRANCH" oms

   Repeat with schwab-1m-v2, then control. Source reads prove that the supplied
   branch's origin ref equals EXPECTED_SHA; never substitute origin/main.
   Bootstrap refreshes runtime per target but migrations stay0 after explicit0023.
   Preserve native post-health SLA: OMS/v2/control60s, strategy240s. A runner-
   local loopback health view forwards each actual 8100/health response,
   with ONLY fresh exact-shape v2 off-hours dryness admitted in memory.
   Every admission logs original status/timestamp/details; stale, unreadable,
   unhealthy-loop, exception, disconnected, disabled, incomplete-warmup,
   wrong-session or pre-session-end stall remains blocking. Other services
   are unchanged. No native gate/source edits, new thresholds or new gates.
6. Require four new active/NRestarts0 identities (OMS/v2/strategy/control);
   orb-schwab and other untouched identities stay unchanged. Manual paper ORB
   retirement is already complete: consume its actual hash-bound historical
   receipt, never disable/publish again. Ten minutes after all starts collect
   new-PID journals plus timestamped live/rotated logs, actual /proc on OMS/v2,
   boolean/numeric audit, DB tx/s, OMS sync-ms, per-name v2 observations,
   scanner/warm/prefill/alert counts and OMS-FALSE-FLIP events. Missing workload
   is UNMEASURED, not invented PASS. Tracebacks/ERRORs are counted and reported.
   Redis five-owner/marker/evictions/memory and actual bar-hole proof retained.
7. Audit requires zero real mismatches; inactive momentum-paper UNKNOWN stays
   truthful. No paper start or process-check retirement for green. Seal actual
   snapshot/install record for ONLY OMS/v2/strategy/control restarted.
8. Re-pin daily preopen SHA, four PIDs/starts, snapshot/install-record/current
   catalog flags and all dependency hashes, backups/atomic writes/runtime last.
   Date/paper shape/root adapter remain unchanged. orb-schwab's old upgrade ACK
   remains CURRENT only if its exact untouched state matches; rebind APP while
   preserving historical receipt, never adopt a replacement identity.
   No orb-schwab expect-flag argument in the restarted group. No fake daily
   PASS or early tomorrow gate. COMPLETE only after actual receipts.
9. ABORT records exact stage/rc/unit states/logs, pages, starts nothing.
   Rollback/recovery/downgrade is NOT pre-authorized.

## Real Read-Only Receipt, Not Approval
October8 15:10:14.992278->15:10:24.766323 ET-equivalent UTC19:10:
gate_readonly.py rc1; fresh bound broker/SQL reads, unknown[], managed/virtual
rows[], in-flight[]. Schwab FLYE1000, working order1008231547198;
Webull flat/zero working. FLYE has22 Schwab session orders and8 fills, so no
operator-only classifier admission is inferred. Native OMS15:10:12 rc1:
FLYE1000, managed0, account stamps9/10s. Native v2 rc1: AIXI armed, FLYE1000,
before18 clock block. NO writes, overrides, cancels or retry of measured work.
This is a real blocking read, NOT after-close approval or a final candidate pass.
Repeat the same full read-only commands at the actual after-close gate time.

Actual read-only box catalog preview:137 rows ->139, added GAP/FALSE only,
changed LINE only, removed0. RPG and all unrelated row objects unchanged.
The preview uses reviewed H catalog bytes only as addition templates, NOT
as final APP selection or proof of live activation.

## Packaging and Remaining Work
Stable historical path:
docs/review-artifacts/gapline-wbquiet-install-20261008/job/runner.py.
Unit/path names retaining wbquiet do not authorize WB studies or source edits.
Standalone runtime: application venv Python/redis/SQLAlchemy/Alembic/Settings,
systemctl/journalctl/git/bash. All helpers, official APP snapshot collector,
completed-retirement receipt and approved-migrations.tar are hash-bound.
No unconditional permission-mode/log-signature/control-page self-stops.

Await parent exact final merged APP and optional I decision; fresh acknowledged
baseline and complete retirement receipt. Only then generate actual manifest,
read-only rehearsal at real time, stage once afterclose and execute if green.
No source writer outside this owned directory; no production action so far.
Tomorrow scanner rule9b and first organic FALSEFLIP workload stay UNMEASURED.

Rollback description only: FALSE_FLIP=false requires OMS AND v2 reload;
LINE/GAP rollback requires v2 reload. Retained flags are not changed. Any
rollback is a later separately authorized action, not part of this GO.
