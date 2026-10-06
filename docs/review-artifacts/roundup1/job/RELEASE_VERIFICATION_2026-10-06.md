# Local Reviewable Release Receipt

Status: **REVIEWABLE, NOT ADMITTED OR STAGED**. No approval file, production
gate/provider call, box write, trading action, application-source edit, service
action, migration, merge or push by this lane. Bounded read-only journal/file
and paper/config captures are documented in RELEASE_STATUS_2026-10-06.md.

## Immutable Identity

- Source/plan commit: `3bd2562c8df4fe490599a7d540ee47c54809884d`.
- Application: `c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`.
- Application tree: `e01851ac630ebf425de655c5c09dc11ae3c0304e`.
- BOX baseline: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`.
- Manifest: `release-manifest.json` in this directory.
- Manifest SHA256: `337b1589accb0a9789d5efa7804c698b81ff7efda3cfc1a4f9fe957c31d1a090`.
- Local-only package: `/tmp/roundup1-c21-review-package-20261006-3bd2562c`.

The manifest was generated exclusively from that source commit and exact c21
application blobs. All21 packaged artifacts and16 application blobs verified;
package release.json is byte-identical to the repository manifest and a fresh
deterministic generation. No approval.json exists. Verification raw receipt:
`/tmp/roundup1-c21-review-package-verification-20261006.json`,
sha256 `fd661199b19071dad1e6b1b75141d428f9e6a437fc824a72bcca5026337a3361`.
This separate receipt/manifest commit does not change the source/plan pin or
any hash-bound artifact. Rebuilding with a different plan commit is a different
release; no implicit moving-HEAD adoption.

## Measured Local Controls

Frozen working code: **469 PASS /16.70s**,
`/tmp/roundup1-literal-release-freeze-20261006.log`,
sha256 `e2fb0b40da4bbc1de97676c8988987d0e54938f3315a2f3ceaa0671ff8bb97ad`.
Committed-head rerun: **469 PASS /15.50s**,
`/tmp/roundup1-literal-release-committed-20261006.log`.
Ten fresh in-memory probes: all control PASS / mutant ASSERTION_RED, no source
rewriting, `/tmp/roundup1-literal-release-freeze-mutations-20261006.json`,
sha256 `5963008514a68e8576e1df728154e4755a6d3695dff797fa6c74ddd153c63d3e`.
Ruff complete job directory and git diff --check PASS; shell syntax tested.
Literal fake full sequences include normal/proven-row47/expected-paperUNKNOWN2,
missing-current-signal STOP without reset, partial-failure abort/no recovery,
backup/env/gate diffs, immutable local package and timer-only activation.
Production-destination rejection and absent approval are explicit controls.

No full application suite or real Linux/systemd/provider rehearsal was run in
this lane. Tomorrow06:20/06:22 timer delivery and positive1000 activation remain
UNMEASURED. d125 BUILD_STATUS and strict-flat helper are unchanged evidence.

## Scope And Remaining Admission

Four items only: ROUNDUP1, LINE=CHART Restoration, ORBPURPLE1 and daily preopen
timer. Only v2/OMS/orb-schwab stop/start sequence and three reviewed env enables.
RETRYOFF removal explicitly excluded after failed recorded OLOX acceptance;
retry key/catalog retained, not a no-second-trade PASS. #1099/#1100 and all other
unpinned lanes excluded. A fresh reviewed merged pin before start requires a
new immutable candidate/release/approval; those lanes do not hold this candidate.

Actual132/66 is a real admission blocker, not cleared here. Exact dated IPDN1000
policy requires fresh complete zero-bot-owner/net/managed proof and the sole
exact residual raw finding. Original gate rc/output is retained, not waived or
represented as whole-account flat. Fresh raw gates/strict proof are required
after16:00 before any box write, under exact independent attended approval.

Catalog remains143 boolean consumer checks +8 numeric =151. Only two exact named
paper UNKNOWN rows may be expected under the dated fresh unchanged09:40 clean
stop proof. Preserve **149/151, rc2, UNKNOWN2**, not151PASS; all other rows pass
or block. No runtime checker edit or paper restart. Morning dynamic paper gate
remains active/NRestarts0, today03:40 floor and guard ordering.

Armed completeness is explicit before/after the unchanged v2 gate; unsupported,
failed, absent, malformed, stale, mismatched or armed reads block. EVAL_RO on-box
support/execution is UNMEASURED. No failure/empty query is treated as zero.

Row47 reset is conditional, not pre-cleared: exact current-attempt old-PID/
invocation SIGTERM + CancelledError/entrypoint + intended manager stop/exit1,
bounded timestamps, stop rc0 and MainPID0. Prior rows are schema only. They have
no direct signal proof and an untimestamped file traceback; same incomplete
runtime evidence will STOP without reset. A reviewer must resolve this proof
contract before relying on row47 reset; no signal delivery inferred here from
KillSignal=15 and no legacy-line clearance. Abort preserves actual partial state
and starts nothing; no automatic rollback/recovery is authorized.
