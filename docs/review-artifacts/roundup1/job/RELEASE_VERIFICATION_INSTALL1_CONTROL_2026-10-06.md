# Install1 Control-Inclusive Frozen Release

**LOCALLY TESTED / FROZEN / NOT RUNTIME ADMITTED.** This lane performed no
production staging, gate invocation, service action, broker request, token
refresh, environment/DB/Redis write or application-source edit. Parent alone
owns production execution. d125 and all historical manifests/receipts remain
unchanged. Install2 operator-holdings classifier is excluded and not imported.

## Exact Immutable Binding

Source/plan/runner: `121f8e09f76313bc8ebc6d07566bf67b3473742a`, pushed and
remote-verified on `codex/1006-after-close-install-plan`. This metadata receipt
and manifest are a separate commit, not circular manifest inputs.

APP: `4805ddc81184c76b4d5cef5c483c809edb666fe6`.
TREE: `4248057079864f93066a69f355e2607840c701b9`.
BOX: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`.
Scope: six items, exactly v2/OMS/orb-schwab/control once; control atomic restart
last, no separate stop/start/reload/reset/repeat. No migration or extra owner.
Retain retry-enabled=true unchanged, set retry-max=0 plus the three reviewed
boolean enables. Golden source catalogs unchanged; isolated143+10=153.

`release-manifest-install1-control.json` SHA256:
`b36f12380d9288df4fae7460079eec85042fe530e573d8f256ca7d38bf24a61b`.
Local package: `/tmp/roundup1-4805-install1-control-20261006-121f8e09`.
All26 artifacts exactly equal their committed source blobs and declared hashes;
all20 application blobs match APP. Manifest regeneration is byte-identical.
Exact inventory is26 artifacts + release.json + the separately generated local
approval.json. Approval SHA256:
`96ebf1c08e99aae242b973e3315161a2624b96f22f06251f5f6a4edd7bcd5c83`.
It records `authority=operator-standing-mechanics-authority`, not a fabricated
reviewer. Latest explicit instruction authorizes tested local generation; no
fresh reviewer-hash roundtrip is required. It waives no runtime gate.

Raw local package verification:
`/tmp/roundup1-install1-standing-package-verification-20261006.json`, SHA256
`4ad39403614c8bf8201db518995dbd95467abe8d294fa6493f7c00b32e3b678c`.

## Test Receipt And Remaining Gates

Freeze **672PASS/44.85s**:
`/tmp/roundup1-install1-standing-freeze-20261006.log`, SHA256
`bf41bfa80308a8cef09bb42e088f039b8435026fda9e0cc1496f7f6aa0acb3da`.
Exact committed-source rerun **672PASS/45.33s**:
`/tmp/roundup1-install1-standing-committed-20261006.log`, SHA256
`b514edd8af6189c8ffdc97eb2a813d26e354bdf453a2500d3634a27fe9fc157a`.
Eighteen fresh in-memory controls PASS / mutants ASSERTION_RED:
`/tmp/roundup1-install1-standing-freeze-mutations-20261006.json`, SHA256
`b36f2de7ead64b185de49f1cc99a3c14a23a5fe4cccd5e8ffcf585266aa83a87`.
Ruff and whitespace PASS. No source rewriting or application full-suite claim.

Full controlled sequences cover normal/proven-row47/paperUNKNOWN2 branches,
source/env/catalog/gate backups and diffs, immutable approval, one control
restart, phase-aware unchanged identities, exact new control PID/invocation,
OMS token-owner binding, raw page/API and logs, timer-only closeout and partial
failure/abort without recovery. New test names include
test_control_http_only_four_readonly_uncached_bounded_gets,
test_adapter_owner_bracket_canonical_false_or_pinned_default_only,
test_collect_exact_readonly_boundaries_failclosed,
test_final_control_new_identity_and_invocation_each_required,
test_literal_control_wrong_oms_token_owner_binding_stops_before_restart,
test_local_approval_exact_committed_blobs_and_standing_authority,
test_standing_approval_drift_unready_or_fabricated_reviewer_blocks.

`blocking_acceptance=[]`: no unfinished code/policy acceptance card remains.
This is NOT fresh runtime admission. Parent's15:22 APUS78/MOBX504 bot holdings
are reported blockers, not allowances. Historical IPDN132/66 is not cleared;
dated exactIPDN1000 positive activation remains UNMEASURED. Strict-flat helper,
MI/NXL rules and exact residual gate policy remain unchanged. Every attempt
must prove fresh direct/books/net/managed/virtual/intents/SELL/unknown evidence.

Before first write: after16:00 October6, exact BOX/source, fresh strict admission,
complete armed0, bounded census/Redis, raw unmodified v2/OMS gates and old
control/token-owner proof. Repeat phase-aware gates before every service action.
Row47 reset only if the current intended old PID/invocation has explicit
SIGTERM/CancelledError/entrypoint/manager exit1 and MainPID0; ambiguity STOP.
No old signature waiver. Night paper only retains exact151/153 rc2 UNKNOWN2
with fresh dated09:40 clean-stop proof, never153PASS or a paper restart.

Control requires healthy enabled refresher/no dead retries/error, token180s
margin and OMS alternate writer false; no forced refresh. After one restart:
old PID retired, sole new owner, new logs and exact ORB LIVE/SCHWAB JAGX1 trade
page/API. Runtime Linux validation, token handoff, loaded page, bar continuity,
new-process warmup/restoration and tomorrow's06:20/06:22 results are UNMEASURED.
The runner enforces refusal/STOP, not automatic retries or force-green.

## Independent Local Reproduction

Use the frozen source commit, not moving HEAD. From its checkout:

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:/usr/bin:/bin /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider docs/review-artifacts/roundup1/job -q --tb=short
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/roundup1/job/mechanics_mutation_probe.py
```

For an independent NEW local package (existing target refuses):

```sh
env PYTHONDONTWRITEBYTECODE=1 /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/roundup1/job/make_release.py --plan 121f8e09f76313bc8ebc6d07566bf67b3473742a --package /tmp/roundup1-install1-parent-121f8e09
shasum -a 256 /tmp/roundup1-install1-parent-121f8e09/release.json
env PYTHONDONTWRITEBYTECODE=1 /Users/velkris/Projects/project-mai-tai/.venv/bin/python /tmp/roundup1-install1-parent-121f8e09/make_approval.py --release /tmp/roundup1-install1-parent-121f8e09/release.json --expected b36f12380d9288df4fae7460079eec85042fe530e573d8f256ca7d38bf24a61b --output /tmp/roundup1-install1-parent-121f8e09/approval.json
shasum -a 256 /tmp/roundup1-install1-parent-121f8e09/approval.json
```

Literal staging/attended commands and refusal conditions are in
STAGING_INSTALL1_CONTROL_2026-10-06.md. They were not executed. There is no
production dry-run CLI: the available read-only evidence phase is initial(),
which also writes exclusive local attempt receipts. The fake full sequence is
a local rehearsal, not a real Linux/provider preflight or production admission.
