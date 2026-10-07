# [codex] Oct7 LINESRC1 + HOTFIX1 Executable Paired Install

LOCAL reviewed mechanics only; parent alone stages the root package. Latest
same-human standing GO authorizes UNATTENDED dated execution. No production
service, source, env, DB, broker or root-unit write was performed in this lane.
All earlier v2-only/OFF plan receipts in TEST_RECEIPT.md are HISTORICAL, superseded.

## Immutable Scope And Literal Actions

APP `994f08aee2c35b3809b3c3384e0d8628807dcfb7`.
TREE `7d827a407825d1d93aae8c3946d8b55320b9680f`.
BOX `5b8b4f642bbc3c312be436d0e92adbc22d9e9f95`.
The actual BOX..APP src/ops/scripts delta is exactly release_policy.SOURCES:
three LINESRC1 v2 paths plus oms/mirror_retained_hold.py and oms/service.py.
There are no ops changes. Assembly verifies this exact five-path allowlist,
APP ancestry, tree and every committed executable artifact/blob.

Literal actions: **stop v2 -> restart OMS -> start v2**. systemctl restart OMS
is one approved stop+start, not a migration or extra restart. Gates precede each
atomic service action. Phase1=v2 down/OMS old; phase2=v2 down/OMS new;
phase3=both new. No gate executes while OMS is deliberately down. The unchanged
strict_flat_readonly.py still requires fresh stored account/reconciliation data,
fresh direct broker GETs and complete bounded SQL. MI/NXL policy is unchanged.
Phase2 pins new OMS PID/start/invocation/monotonic/config/NRestarts0 ONCE.
Phase3 and all subsequent checks must match that complete pin, never overwrite it.
After OMS restart, new process LINE+RETAINED=true/HANDOFF=false, fresh strict
BOT-flat book and new-process published healthy OMS heartbeat are required
before v2 start. A failed restart or missing fresh proof leaves v2 stopped and
pages actual state; no start, rollback, reset-failed or recovery is authorized.

Only LINE_CHART_RESTORATION_ENABLED and OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED
change false->true in the shared env. All other bytes remain identical;
ATR_REPRICE_HANDOFF_ENABLED stays literal false and the seven other v2 live keys
stay true. Duplicate/alias/missing/already-true flags refuse. Old OMS loaded
handoff=true is admitted ONLY with its complete captured old identity; old v2
loaded handoff=false. Both new processes must prove handoff=false. #1115/RPGSTALE
and parked #1110 remain excluded. No schema, archived-row mutation, token refresh,
pip, other application unit action or automatic recovery.

The first stop is dated Oct7 strictly after16 and before midnight ET, and requires
the unmodified native v2 gate immediately beforehand. It naturally refuses
before18: read-only PENDING, never a native PASS or override. There is no fixed
launch time. After a clean stop the clock does not abort the approved completion
path. Explicit zero-armed proof (60s) surrounds native admission. General OMS and
strategy preflight remains pre-first-stop and post-both-healthy; while v2 alone
is down only unchanged direct strict-flat plus new-OMS health is used.
No global TZ change.

Global deploy flock plus existing daily run flock cover the entire attempt.
O_EXCL write/abort seals prevent repeated installation after claim, COMPLETE
or ABORT. Recognized fresh book/work/armed/ticket blockers may wait read-only
before claim, retaining raw rc1 evidence. Foreign defects and exhausted unknowns
STOP; the same blocker after claim aborts. Only rc2 is retried: three total reads,
60s apart with raw stdout/stderr. rc1 is never retried or turned green.

## Exact Baseline, Overlays And Root Handoff

Fresh independently captured full13 fleet/14 fields: paired-baseline.json
SHA256 fd05165a80683dd1979f428fa67ccd73ab36040a4d88cefff3ea3459f4c3066b.
Its three fields are fleet_before, environment_sha256, authorization_provenance.
Generic identity/environment/authority adoption refuses.
OMS1051883 started14:12:15UTC, invocation9508a735e64f40a9a561fb0530b201dc;
v2 1207761 started17:45:25UTC, invocationfc08281cd51a45639f44dd5b953e06b3.
Both NRestarts0. The other eleven complete identities/configs remain exact.
Env baseline SHA40ec5bf52b1ffb2b37dc84399e6449a9b796a3897c0207b8939402985ddfe653.
Raw independent receipt: paired-capture-readonly.json; no env secrets exported.

Parent stager APIs are unchanged:
`rollback_catalog(sourceAPPflags)` derives source catalog expected retained=true,
LINE=true, handoff=false (the only catalog overlay is handoff true->false).
`rollback_runtime(CURRENT d585 raw, newflags)` changes only that installed catalog
evidence-input hash. CURRENT raw is fixtures/paired-current-runtime.json:
d585c601eded25732dcaa05ec7886df44b42f506ed3e9efc933c5037649dc13d.
CURRENT installed flags are fixtures/paired-current-flags.json:
acf5a6dc55d1e8c54bb3b1a7c24ed7b093673c9729bd7accef83a2a848d19652.
Keep artifact names rollback-expected_flags.json and rollback-runtime.json;
they are the NEW paired overlays, not the old rollback ruling. Source APP flags
remain unchanged. Baseline admission expects these exact staged overlay bytes.

Archived preservation ONLY: bounded read-only repeatable-read SQL, <=64 rows/1MB,
5s statement/connect timeouts, exact snapshot type
oms_webull_mirror_retained_hold__archived_20261007. archived-baseline.json records
one row136db58b-4394-5999-a255-8434c21434ae and exact payload hash
a8f78112d5ad1607eddb199e30c4f2bb6c38a9333283946eba8968116780ff87.
Initial/final receipts must match; no UPDATE/DELETE, unarchive or ownership release.
Unreadable capture returns typed rc2 UNKNOWN without traceback or sensitive text.
This is preservation evidence, not a new trading authority.

Paper/guard are already clean inactive09:40 with exact original start/invocation.
Their positive actual-key scheduled-close audit remains required:
systemctl_rc=0, action=stop_paper, reason=scheduled_session_close, bounded262144B
tail of /home/trader/after-hours/2026-10-07/option-a-daily/run-20261007T074001494845Z/option-a-guard.jsonl.
Whole mutable file is not hashed. One-second systemd displayed timestamp
precision is disclosed; no invented09:45 cutoff or arbitrary PID adoption.
No paper/guard action. Existing inactive-paper shape permits only two truthful
UNKNOWN checks, never a paper start to obtain green.

Root target EXACT:
`/home/trader/after-hours/2026-10-07/linesrc1-hotfix1-994f08ae/job`.
Same unit names project-mai-tai-linesrc1-20261007.service/.timer, same calendar.
Parent has paused ONLY the superseded installer.timer, service MainPID0, to
prevent obsolete package execution. Old approvals/jobs remain immutable.
Parent root stager verifies paused/unclaimed units and exact old service
60e696ec8519340de0739e3abbd13b004ae548b0a9d536c2c6acc6aa596e115e /
timer0ce5a48081cd9e462483b3fb5c1bc3450e7c4d8af5f9dd73f9133f884e988fe6,
locks, actual env/fleet/catalog/runtime and clean BOX before any write. It backs
up catalog/runtime/env/units, creates a distinct immutable package, applies only
metadata overlays atomically per file under daily lock, verifies Linux unit
syntax, replaces the same named unit and enables ONLY its installer.timer.
No app/env/service changes are part of staging. Never run the old stager unchanged.
A partial overlay replacement fails hash admission, not automatic recovery.

Parent stages exact approval.json NOW under same-user standing GO after verified
handoff. Both service conditions require approval and absence of write-started.
Approval never bypasses native/clock/flat/identity gates. No attendance latch.
After COMPLETE or STOP the runner disables/stops only its installer timer.
Existing daily preopen timer remains enabled and unchanged.

Local assembly (exact committed bytes; no remote effects):

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:docs/review-artifacts/linesrc1-install-20261007/job \
  .venv/bin/python -B docs/review-artifacts/linesrc1-install-20261007/job/make_release.py \
  --plan FULL_COMMITTED_PLAN_SHA \
  --baseline docs/review-artifacts/linesrc1-install-20261007/job/paired-baseline.json \
  --package NEW_LOCAL_DIRECTORY
```

APP object must be fetched into the box clone without checkout advance before
staging/runner; unavailable object fails. The package binds APP/tree/BOX, artifacts,
source blobs, exact baseline, archived receipt and standing approval to release
SHA. Root invocation (normally named one-shot, not manual duplicate):
`sudo /home/trader/project-mai-tai/.venv/bin/python -B /home/trader/after-hours/2026-10-07/linesrc1-hotfix1-994f08ae/job/runner.py RELEASE_SHA256`.

## Proof And Truthful Boundaries

Backups: exact BOX archive <=64MB (parent actual42,393,600B), env/preopen/catalog,
runtime/binding/ack hashes. Both OMS/v2 log inode/device/offsets captured before
service actions. New active owners/NRestarts0/process flags, startup BUY absence,
bounded census/Redis/book/health are post-action proofs, not predicted receipts.
Full HandoffJournal <=1024rows/4MB, count informational; requested/price_wait/
submitting0 mandatory. Five Redis owners/markers/evictions unchanged.
Canonical no-wire aborted proof remains exact; unproven aborted rows block.
Operator holdings need ZERO session bot orders AND ZERO fills exact account+
symbol and fresh complete direct/book proof; any record/net-zero roundtrip blocks.
No unowned SELL or foreign finding waiver; MI180/NXL2 remain separate. Native
holding policy is unmodified (CYN/TE only); an actual operator holding can still
BLOCK the native gate and must be disclosed, not bypassed.

Direct UNMODIFIED repository ops/health/v2_restart_evidence.py report is called
with both --restarted v2+oms and flags only on that pair, BEFORE any morning
ack rebind. Never call the installed old-APP ack-aware wrapper first.
Raw official FAIL/UNKNOWN is retained verbatim. Literal HELD/restoration0 is
accepted after16 only under existing disclosed held-pair/pending-first-bar/
exact restart-window gap mechanics, not fabricated warmup PASS.
If long closeout observes the pinned source's safe boot release, it must prove
the exact restoration_complete=1/reconstructed_uncapped=0 marker and actual
official REST warmup AND BOOT-HOLD PASS rows; the unchanged official collector
validates exact369s seeded-fallback population/marker. A release alone is not
proof. Late release is re-collected before COMPLETE, not required forever HELD.
Every new ERROR/Traceback/token-dead/unsafe hold remains a blocker.

Before20 measure exact watched population and per-stock restart-window minutes;
bounded1024rows/1MB SQL, raw official hole verdict stays literal. Three60s-separated
reads may await the first closed postrestart candle. Restart-spanning missing
minutes are disclosed, not zero holes or N/A; outside-interval/unreadable blocks.
No independent print or actual fill claim is invented. NextOct8 actual first
07:00-or-later callback/live contiguous line acceptance is UNMEASURED.

Closeout narrowly repins BOTH new OMS/v2 PID/start, APP and paired record/
snapshot. Historical Oct6 actions/evidence remain historical; no stale grouped
restart flags. Record service_actions truthfully classifies both restarted,
all others deliberately untouched. Existing daily.py/restart_report.py/root
06:20timer, dynamic ET date, numeric10 catalog and ORB acknowledgement
PID765206/NRestarts1/start06:31:14UTC/invocationde16f6a3ae8f438a8aae3d294d7db686
remain unchanged. Update scoped binding APP/tree, upgrade_ack helper+record APP,
runtime hashes and new evidence paths under daily lock. Verify runtime/current
after repin. Interrupted multi-file closeout fails closed, never repairs itself.

Catalog denominator147boolean+10installed numeric=157; do not copy source8numeric.
Abort seals raw phase/fleet/actions, pages actual states and retires own timer
without any recovery. Notification delivery is unmeasured unless observed.

Known criterion limitation: zero literal LINE-REBUILD-CYCLE without re-add FAILS
parent's recorded pinned streamer case: one source GET plus nine suffix updates
produced ten markers. Therefore zero such markers is NOT promised. Fetch count
is diagnostic only, not a replacement criterion; reviewer disposition pending.
Pinned trading source is unchanged by this plan.

Parent main994 source performance receipts are separate from local mechanics:
first focused run260PASS/1FAIL142.04s; retained-ON watchdog79.737ms exceeded50ms
at14:42:19..14:43:19, handler20.516ms, SQL1 firstturn/0 after1s, GC0.
Original FAIL remains unwaived. Later isolated ON PASS60.58s at14:46:25..14:47:26:
14400events/60.000593s=239.9976/s, stall23.378ms, handler16.294ms, SQL1 firstturn/
0after1s/0buys, GC0, no other pytest visible at launch. No threshold/source change.
NFQ proof e4578d44 is parent-reported scoped performance, not this release or
a source inclusion. #1115/RPGSTALE remain PARKED.

Local tests distinguish measured fixture byte hashes/production readonly
identities from CONTROLLED future clocks/service/SQL/collector scenarios.
Staged Linux verification, actual new processes/fills/catalog/native PASS,
notification delivery and morning acceptance remain UNMEASURED until parent
executes and records them. A local package/test PASS is never a deployment PASS.
