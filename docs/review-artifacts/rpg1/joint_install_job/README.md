# October 3 Joint Install Job

Local build only. No approval receipt is supplied. `release.json` is deliberately
NOT READY; neither null fields nor branch tips are executable authorization.
Parent owns staging and the dated plan. Do not commit `approval.json`.

## Staging Contract

Stage these **nine** files together, without renaming:

```
policy.py review_gate.py proofs.py install.py run_install.sh
project-mai-tai-rpg1-coldstart1-install-20261003.service
project-mai-tai-rpg1-coldstart1-install-20261003.timer
test_joint_install.py README.md
```

Stage `release.json` separately after filling its application, plan, reviews and
fresh host evidence. Job directory:
`/home/trader/after-hours/2026-10-03/rpg1-coldstart1-job` root:root 0700.
All staged files root:root 0600 (runner may be 0700). Installed unit copies in
`/etc/systemd/system` root:root 0644 must match their staged SHA256 exactly.
No symlinks. Hash all nine artifacts in `release.artifacts`, keys exactly above.
Do not stage Python bytecode or an approval fabricated by the staging script.

The manifest schema is the accompanying JSON plus the checks in `review_gate.py`.
`application` binds BOX_SHA, final merged SHA/tree, both merge SHAs, ordered
BOX..approved commits, full changed-path inventory and SHA256 of **every src
file**. `plan` binds the October 3 document at a full commit. `reviews` contains
exact records for PR1085 and PR1088: `pr`, `head`, `base`, `record_commit`, `pin_path`,
`pin_sha256`, and two successful exact-head Validate `ci_urls`.
`host` is fresh read-only evidence, not historical PIDs copied from the plan.
Its file pins and unit-text hashes are rechecked before writes. See the validator
for required file paths and durable-state terminal allowlists. Unknown states
refuse; they are never treated as completed merely because absent from a list.

Read-only staging capture (no approval required): repo `.venv/bin/python -s
proofs.py capture` prints the exact `host` object as clean JSON to stdout, and
diagnostics only to stderr. It performs fresh broker/DB/Redis reads; no service
control or real simulated-publish I/O. `review_gate.py --validate-release` checks
release shape, staged artifacts, staged dated-plan basename/hash and installed
unit hashes without requiring approval. It does not authorize execution. The
normal gate additionally requires the exact receipt. Pin paths are
`records/<head>/pr-<number>--<base>--claude-1.json` at the bound `record_commit`.

Approval is root:root 0600 `approval.json`, exact keys:
`schema_version:1`, `reviewer:"claude-1"`, `decision:"APPROVED"`,
`release_sha256` (literal staged release file hash), `window` (exact release
window), and `dispositions` with keys `broker_inventory`, `idle`, `reset_failed`,
`denominator`. Values must be the complete strings exported by `policy.py` as
`BROKER_DISPOSITION`, `IDLE_DISPOSITION`, `RESET_DISPOSITION`, `COUNT_DISPOSITION`.
This explicitly approves **140/140 = 132 boolean + 8 numeric**, numeric-only8/8,
not136. The broker/idle limits are accepted limitations, not claims of complete
inventory or an OMS drain ACK. Unexpected orders or new intents still stop.

Deadlines are inclusive **23:00:00 ET** for preparation/initial mutation and
**23:59:00 ET** for every subsequent mutation on **2026-10-03**. No clock override.
The timer is nonpersistent. Missing approval skips without claiming an attempt;
after a valid approval, one exclusive run directory plus the common deploy lock
prevents a second attempt. The runner disables only its own timer. Failure never
starts stopped producers, rolls back, retries, clears Redis, or repairs ownership.

Monday scanner windows/real trading reprices remain separately assigned live
UNEXERCISED checks. Tonight proves five consumer restarts, not an empty-Redis
cold boot. Do not turn an empty BOOT-HOLD into a released/pass claim.

## Review Handoff

`KNOWN_CANCELLED_STACKS` is deliberately empty until the reviewer provides the
recorded row-47 ordered `(filename, function)` fingerprint. Therefore reset-failed
currently REFUSES rather than accepting an arbitrary CancelledError. Populate
only from recorded evidence, re-test, and re-hash before approving that recovery
exception. The journal proof additionally requires old InvocationID and exact unit
on both the stop and exit1 messages; no invocation-free text is accepted.

Read-only input capture is deliberately Saturday-empty-population-only. Nonempty
restoration refuses instead of equating owner-hash contents with independent input
evidence. Actual post-start replaces remain required for all five consumers.

Successful completion requires both `COMPLETE.json` (fsynced proofs) and
`JOURNALED.json` (completion journal receipt), and no `ABORT.json`. A journal error
after durable proofs is still INCOMPLETE and is paged as a journal/receipt failure.
No production action is performed by the local tests. The small-event Redis
decoder enforces client allocation bounds; COUNT/HLEN/HSTRLEN bound requests but
are not a claim of a hard server-side allocation limit for an oversized field.
