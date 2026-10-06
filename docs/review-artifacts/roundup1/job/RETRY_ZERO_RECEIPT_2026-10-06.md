# Corrected RETRYOFF1 Zero-Budget Policy Receipt

Operator/reviewer October6 authorizes enabled=true unchanged and max_retries=0
on c21 source, env-only. The earlier enabled-OFF/removal request is withdrawn;
its failed result is not converted to PASS. No application-source change.

Parent owns the recorded decision replay and bundled inputs at
docs/review-artifacts/retryoff1/ZERO_BUDGET_REPLAY_2026-10-06.md in the shared
handoff worktree. Independently read, not rerun in this lane:

- Focus raw /tmp/codex-retryoff1-zero-budget-focus-20261006.log:33PASS/0.87s,
  SHA256 f1b5045ce8b761ce455a07695a73f230c809b3b1b8890fc7a5acf04f876222aa.
- Results /tmp/codex-retryoff1-zero-budget-results-20261006.json:
  SHA256 b574d462278c677d607d90e558ef373835d84eddd1c082fca2ce95472f9e40e4.
- Mutation /tmp/codex-retryoff1-zero-budget-final-mutation-20261006.log:
  max0->1 makes both blocked-second assertions RED (2failed/4PASS),
  SHA2563791d1c4ea27da3f2d7f9194c79047a889f6cb383e4769a29d71b029ad0431cb.

OLOX12:20->12:21 and IPDN actual12:16->12:17 remain consumed/count1 with zero
primary/mirror drafts at max0; max1 positive controls draft1/1. Fresh SELL resets
the budget and first new-cycle trade remains allowed; unfilled waiting ownership
is preserved. These are controlled offline decision replays, not broker-wire,
full-session, live-cache or observed deployment receipts.

IPDN correction: prior first10:23/old127-share close11:06 precedes fresh
SELL11:41:02.394 (bar11:40 ts1791301200000), closes0 reset and first rest11:44
opportunity1791301442514. Unfilled11:56 waiting/reprice is not a second closed
trade. Rowa53adba2 fills12:14, confirmation12:16:02.804, release12:16:07.952 and
second rest12:17:02.924 identify the actual blocked second. No fabricated11:5x
second-after-close case or assertion waiver.

Reviewed isolated numeric artifact preserves the five original c21 entries and
adds retry_one_max_retries expected0, owning_service=schwab-1m-v2,
also_check_services=[oms], require_process_env=true. Its SHA256 is
bff3fad73fa593b48af08fc3cb8ab6cad5d2d055e1bc787706ff79ad34dbf205.
143 boolean plus10 numeric process checks =153. Golden application catalogs
remain unchanged. After-close paper UNKNOWN2 is retained as151/153, not PASS.

Local mechanics proofs must separately require exact raw enabled=true/max0,
reject aliases/duplicates/defaults and bracket stable active PIDs. These checks
are admission/provenance, not a new trade replay. Production effect remains
UNMEASURED until fresh flat/raw gates, exact approval and scoped restart.
