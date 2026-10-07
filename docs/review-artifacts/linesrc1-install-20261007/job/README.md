# Codex LINESRC1 Oct7 Executable Install Plan

LOCAL runner candidate only. Parent alone stages, reviews exact package bytes,
activates the named one-shot/timer, attends production and records installation.
No application source change, OMS restart, merge, service action or broker request
was performed to develop this package. Runtime measurements remain UNMEASURED.

## Exact Scope

APP `f9c9bd332392e2c905fc39b954421c88970844d7`;
TREE `0fa89d5d40473e29e1039cd6a80200b36945d696`;
BOX `5b8b4f642bbc3c312be436d0e92adbc22d9e9f95`.
Only the three v2 source paths in `release_policy.SOURCES` differ under src/ops.
The running v2 baseline is PID917354, Wed2026-10-07 11:14:07 UTC, NRestarts0.
Only `MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED=false`
becomes literal `true`; duplicate/case aliases, missing and already-true refuse.
All other EnvironmentFile bytes remain identical. Explicit already-live keys
and full 157-check Boolean/numeric catalog are verified independently.

Stop v2 cleanly -> start v2 once. No other service actions, pip/dependency
installation, migrations, token refresh, trading writes, clearing Redis/ledger,
rollback, reset-failed or automatic recovery. A failed stop NEVER starts v2.
Global deploy flock and existing daily run flock cover the whole attempt.
The exclusive write/abort seal prevents any second deployment after claim,
COMPLETE or ABORT; only an unclaimed clock/window PENDING can be retried.

First stop must be Oct7 strictly after16 and before midnight ET. The unmodified
native v2 gate also must pass: it normally refuses before18. That is read-only
PENDING, not an override or a promised install time. Explicit fresh zero-armed
published state (60s) is read on both sides of the native gate. General preflight
for OMS and strategy remains before first stop; after v2 is stopped only direct
strict flatness is requested, not an invented inactive-service health failure.
After a clean stop the clock does not abort the single approved start path.

Fresh actual direct holdings=0, working orders=0, open managed/virtual rows=0,
inflight intents=0 remain mandatory. No operator-holding waiver. The byte-exact
reviewed strict helper retains existing MI/NXL historical reconciliation policy;
its Oct6 IPDN allowance cannot authorize a nonflat Oct7 install. Full all-date
HandoffJournal inventory is bounded1024rows/4MB; count is informational,
requested/price_wait/submitting must be0, immutable bindings stay unchanged.
No per-old-order terminal-detail population, bulk history or snapshot-batches.
Read-only rc2 retries three total times, 60s between, stdout/stderr retained.
rc1 never becomes success. Redis proof is bounded owner metadata: five owners,
migration marker, stream types and unchanged eviction counter; no owner clearing.

## Assembly And Invocation

Fetch the reviewed APP object into the production clone without advancing its
checkout BEFORE invoking the runner. This is parent staging, not a runner fetch.
The source-object/tree check deliberately fails if that object is unavailable.
Backups retain original env, preopen, runtime/binding/ack, and exact BOX archive
under a measured64MB bound (parent measured42,393,600 bytes). Hash receipts and
clean source checks precede the only source advance and flag replacement.

Parent supplies a LOCAL JSON metadata file with exactly these fields:

```json
{
  "environment_sha256": "6677c4bd25fadc5c229b2a0c682f5d1ae8a4bc30052b9121bc044ea2932f35ce",
  "fleet_before": {"each of the13 release_policy.SERVICES": "actual complete runner.FIELDS object"},
  "authorization_provenance": "same human standing GO; exact reviewed runner/package receipt"
}
```

Do not use an08:13 active-paper fleet as the after-close baseline. Capture the
actual full fleet AFTER the normal09:40 paper/guard transition. Do not predict
future identities or loosen adoption of a changed owner. Assembly has no effects
beyond a newly created LOCAL package. Environment secrets are NOT exported.
The supplied env hash must still match at actual first admission; any drift blocks.

```sh
python -B docs/review-artifacts/linesrc1-install-20261007/job/make_release.py \
  --plan FULL_COMMITTED_PLAN_SHA --baseline actual-after-close-metadata.json \
  --package NEW_LOCAL_DIRECTORY
```

This binds executable artifacts to exact committed bytes, APP/TREE/BOX, original
recorded morning hashes, actual full fleet and same-user authority provenance.
It emits release.json, release.sha256 and literal standing-GO approval.pending.json.
Parent publishes the release hash and alone stages the immutable root package at
`/home/trader/after-hours/2026-10-07/linesrc1-f9c9bd33/job` (root-owned; no group or
other writes). Only after exact review, parent may stage/verify these distinct units:
`project-mai-tai-linesrc1-20261007.service` and `.timer`. They do not install or
duplicate the existing `project-mai-tai-preopen.timer` (nextOct8 06:20ET/10:20UTC).
The parent stages approval.pending.json but leaves approval.json ABSENT. Both
service conditions require actual approval.json and no write-started.json.
Only while actively attending after close, after verifying every staged hash
and the gates, may the parent install those exact pending bytes as approval.json
and invoke the one-shot. Same-human standing GO needs no new approval roundtrip;
this file is an attendance latch, not an unattended18:00 first-write permission.
The dated timer permits a read-only pending check once per minute during16..23;
there is no fixed first-stop time, Restart=no, and a seal stops subsequent runs.
Parent prefers activation after the authorized window opens, never this agent.
If a read-only PENDING attempt ends attendance, parent removes approval.json;
the timer must never retain permission to begin a later unattended write.
After COMPLETE or STOP, parent disables/stops only
`project-mai-tai-linesrc1-20261007.timer`. Do not leave the finished every-minute
installer timer active. These approved root installer-unit actions are separate
from the application scope of one v2 restart. The runner never stops its own
service/timer. Existing daily preopen timer remains enabled and unchanged.

Direct attended invocation of the staged package:

```sh
sudo /home/trader/project-mai-tai/.venv/bin/python -B \
  /home/trader/after-hours/2026-10-07/linesrc1-f9c9bd33/job/runner.py RELEASE_SHA256
```

## Proof And Existing Morning Repin

Log offsets/inode are captured BEFORE stop. Active new PID, new UTC start and
invocation, NRestarts0, true restoration in /proc, unchanged old flags, no startup
BUY DB activity, bounded fresh flat/Redis/census are actual runtime receipts.
An after16 no-source-event empty state must prove literal fresh BOOT-HOLD; it is
HELD, NOT restoration/warmup PASS. NextOct8 first actual07:00-or-later closed-source
callback and live line coverage remain UNMEASURED until observed.

Before20, exact restart-window per-stock persisted minutes are read with <=1024
rows/1MB and5s SQL timeout, not bulk full-session history. Three measurements with
60s gaps bound waiting for first closed post-start bars. Complete pre-stop watched
population comes from the official actual snapshot, never armed_count=population.
Live-at-stop uses the collector's90s convention. Missing persisted buckets whose
minute intersects the actual stop-call/start-return interval are explicitly
reported, never called zero holes or N/A. Independent prints remain UNMEASURED in
this mechanical reader; the official collector retains its independent tape and
original verdict. A symbol/minute outside that narrow interval or unreadable
evidence blocks, not a generic gap waiver. Quiet/not-live-at-stop names carry no
restart-spanning claim; a future callback may remain explicitly PENDING.

The raw official report is NEVER filtered FAIL/UNKNOWN->PASS. A mechanical
COMPLETE may disclose the literal held pair, exact bounded restart gap and/or
specific pending-first-bar continuity evidence. Any other failure/unknown still
blocks; in particular foreign/startup errors and the morning wrapper's unreadable
ORB log evidence cannot become PASS. The local report disposition is NOT added
to daily.outcome's accepted vocabulary. Next morning keeps the unmodified strict
reporting policy; only actual healthy release can satisfy restoration then.

Closeout patches actual `40e57465...` morning gate, NOT the old builder. It repins
only new v2 PID/start, APP and new one-v2 snapshot/record; removes stale grouped
restart flags/declarations. Existing dynamic ET date, paper shape, UTC timestamps,
all other identities, exact Redis-upgrade acknowledgement PID765206/NRestarts1/
06:31:14UTC/invocationde16f6a... remain intact. Historical evidence_inputs and
original Oct6 record paths/hashes remain retained, with new v2 evidence added.
The unchanged daily.py, restart_report.py, numeric10 catalog, units/timer and
old evidence are not replaced. Scoped binding.json APP/TREE, upgrade_ack.py APP,
upgrade-ack.json application and runtime.json hashes are updated together under
the daily lock. No masquerading old evidence as a new full-source acceptance.
Each file is atomic; interruption between files fails closed on hash mismatch,
never auto-recovers. Current() and verify_runtime() are rerun after repin.

Full catalog has147Boolean+10installed numeric=157checks. The source's8numeric
checks are NOT copied over the installed10. After09:40, the two exact clean
inactive-paper UNKNOWNs are retained under existing dated shape admission; no
paper start for green. Abort pages the actual fleet and phase, writes raw journal
and deployment note, and does not restore/start anything. Notification failures
and lack of future delivery proof are disclosed, never invented as success.

Fixtures are parent's bounded read-only Oct7 export from
`/tmp/linesrc1-20261007-live-inputs/home/trader`; file hashes are measured locally.
They contain no EnvironmentFile secrets, no positive new install or fill evidence.
Tests use these exact gate/ack/runtime/numeric bytes plus controlled process,
clock, SQL and service responses. Tests are mechanics proof, not production proof.

Future acceptance must distinguish literal rebuild-cycle logs from full-history
authority requests. Parent reports the exact merged streamer test produced
one source GET and nine live-only suffix updates (ten LINE-REBUILD-CYCLE markers,
nine live_append=1) without re-add. Therefore zero such markers is NOT promised.
The runner preserves pinned source and leaves that reviewer clarification open;
it does not substitute a marker count for provider-fetch or line-coverage proof.
