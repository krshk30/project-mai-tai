# 4805 / Retry-Zero Literal Release Verification

Status: **LOCALLY TESTED / REVIEWABLE / UNREADY FOR EXECUTION / NOT ADMITTED**.
No production staging, approval file, install, environment/DB/Redis write,
service action or control restart. This lane edits mechanics/docs only, not
application source or parent handoff. Preserve d125 and the historical c21
manifest/receipt unchanged. No local test substitutes for fresh admission.

## Immutable Binding

Source/plan/runner commit:
`a69c6e71ad617cf8cb08e2f73f6ac3f44066d810`, published and remote-verified on
codex/1006-after-close-install-plan. This receipt and generated manifest are a
separate later metadata commit, not circular manifest inputs.

Application: `4805ddc81184c76b4d5cef5c483c809edb666fe6`.
Whole tree: `4248057079864f93066a69f355e2607840c701b9`, equal to pinned
#1100 head56c9357b66c591acb83a1aa7ccbcbc0847883dd9. Independently read local Git
and hosted mergedAt2026-10-06T19:04:24Z. Latest pin37514987598 SUCCESS,
Validate37513768160 SUCCESS and37513775601 attempt2 SUCCESS19:03:42Z. The
attempt1 SQLite139 remains historical, not waived or a head edit.

`release-manifest-retry-zero.json` SHA256:
`a62e279927b165b9a4f94b3009c551c43f1bb74048f833aa886b5ef408d488f2`.
Generated from exact committed blobs; deterministic regeneration byte-equal.
Local-only package:
`/tmp/roundup1-4805-review-package-retry-zero-20261006-a69c6e71`.
All24 artifact bytes equal their source-commit Git blobs and declared hashes;
all18 application blob hashes match the exact application. Package manifest
equals the repository manifest. Exact file inventory contains no approval.json.
No box files or unit names are staged/listed by this local generation.

Verification raw:
`/tmp/roundup1-4805-retry-zero-package-verification-20261006.json`, SHA256
`27c76455b3952ee5b1c09d127f32b29c022b924bf24713f59adfa46898b08db1`.

## Tests And Safety Controls

Final freeze568PASS/28.44s:
`/tmp/roundup1-retry-zero-release-freeze-20261006.log`, SHA256
`82e1074c3045b02918f50b01b8c07f5d753092b0d17b9c5ee0455f54fba3af1d`.
Committed-source rerun568PASS/28.46s:
`/tmp/roundup1-retry-zero-release-committed-20261006.log`, SHA256
`cd0792aa7954c72549433830468864f4af6fdee3697e7babe2da8ae4c40b1700`.
Ruff complete job directory and git diff --check PASS. No full application
suite, real broker/production gate or Linux/systemd rehearsal run in this lane.
The earlier parent484PASS clean4e8d control does not validate this integration.

14 fresh in-memory mechanics probes: PASS controls / ASSERTION_RED mutants,
no source rewriting. Raw
`/tmp/roundup1-retry-zero-integrated-mutations-20261006.json`, SHA256
`d86ade52e7270f591976a098fe7ea354ed4f254c3cffce3570f97688afbe94c0`.
Four added probes: retry_explicit_raw_values, retry_env_zero_write,
retry_receipt_value_binding, retry_catalog_hash. Ten existing armed/paper/
row47/untouched-identity/raw-residual/timeout/report/floor probes stay RED.

High-signal tests:

- test_literal_full_sequence_backups_gate_diff_hashes_units_timer_only:
  normal/proven-row47 complete sequences; enabledtrue preserved, max1->0 only,
  pre-write env/catalog hashes/diffs, backups,153 coverage, timer-only activation.
- test_literal_catalog_drift_stops_before_first_source_env_write and
  test_literal_env_changed_after_admission_stops_before_source_switch:
  no source switch on artifact/env drift.
- test_literal_enabled_missing_off_stops_before_any_write and
  test_literal_wrong_new_process_values_stop_before_catalog_timer:
  missing/off/nonzero/negative/malformed/alias process/env states block.
- test_each_owner_missing_nonzero_or_malformed_blocks and
  test_each_owner_read_failure_blocks: both numeric consumers independently
  fail closed, no Settings-default admission.
- test_daily_zero_guard_failure_never_runs_gate_one_onfailure:
  missing/wrong/stale/incomplete/read-failure/timeout/PID-drift proof stops
  before the gate; existing OnFailure adapter called once.
- test_no_control_action_allowed_without_scope_update:
  stop/start/restart/reload control all refused by current mechanics.

Controlled process/system/provider fixtures are explicitly simulated boundary
responses, not invented live holdings/trades/bars or acceptance history. Parent
owns the separate33PASS recorded retry decision receipt; its exact hashes,
max0->1 RED mutation and IPDN correction are bound in
RETRY_ZERO_RECEIPT_2026-10-06.md, not claimed as a replay run in this lane.

## Scope And Remaining Gates

Only v2/OMS/orb-schwab restart exactly once, in the literal six stop/start
phases. Three reviewed boolean enables plus max-retries0; retry-enabledtrue
must already be explicit and remain byte-for-byte unchanged. No removal/false
override, protected-symbol edit, sizing/price/exit change, migration or recovery.
Golden source catalogs remain151. The exact isolated numeric artifact adds
explicit max0/require_process_env for v2 and OMS;153=143 boolean+10 numeric,
dropping no row. Raw nighttime paper151/153 rc2 UNKNOWN2 stays UNKNOWN2 only
under unchanged fresh exact scheduled-stop proof. No paper restart/force-green.

Display source is included in the4805 checkout, not activated. Control compiles
handlers with reload=False/no ExecReload and owns SchwabTokenRefresher. User's
activation decision is still unanswered; no control action is authorized or
implemented. The manifest explicitly excludes display activation. Any future
authority requires new mechanics/rehearsal/manifest/approval, not mutation of
this release or a quiet extra restart.

Execution remains UNREADY until exact independent approval and fresh after16:00
October6 admission. Last pre-close IPDN132/66 is not cleared; remeasure actual
positions/managed/virtual/bot fills/SELL/in-flight ownership. Exact dated1000
residual policy is guarded and positive live activation UNMEASURED. Preserve
original v2/OMS raw gate results and proof-dependent residual admission; no
blanket NO-GO waiver or whole-account flat claim. Armed reads must be explicit,
complete and fresh. Row47 reset requires current-attempt old-PID/invocation,
explicit SIGTERM+CancelledError+entrypoint and manager stop/exit1, MainPID0;
recorded prior schema cannot clear it. General legacy clearance/recovery is
not solved by the conservative bounded14-ticket census; new/unknown/overflow
evidence blocks. Unsupported EVAL_RO, unavailable signal proof, fresh gate
failures, extra holdings or UNKNOWNs still stop. Real systemd/calendar, bar-hole
and next-session provider/timer delivery remain UNMEASURED.
