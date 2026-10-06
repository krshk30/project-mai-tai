# CLEARWAIT1 defensive build verification

AGREE with the approved RELEASED card and its explicit fail-closed clarification.
The unsafe ordinary-submit rollback edge was reported, reproduced and stopped,
not hidden. `STOP_2026-10-06.md` and `unsafe_counterexample.json` retain that
historical evidence. This build addresses it within lane C; no production,
handoff, environment, service, broker or ledger intervention occurred.

## Exact Proof, Not Row Absence

A durable `v2_wait_dispatch` begin record is written only when a fresh episode
is bound, before any dispatch. Every V2 producer emission records an exact
account/episode attempt token before publishing to the serial OMS lane. The
token follows the ordinary intent/order metadata; no broker wire or OMS
submission behavior is changed. Journal failure prevents that emission.
RPG/NFQ/deferred retry ownership and their existing pre-wire durability remain
authoritative and are not waived.

Removal records its request, drops still-local opening drafts and revokes entry
permission. Both configured account cancellation acknowledgements must settle.
Every recorded attempt must match a positively classified terminal opening
intent and either its exact broker-terminal order evidence or the existing
explicit pre-submit refusal provenance. A lost transaction leaves a durable
unresolved attempt and reports `dispatch_attempt_unproven`. Missing, malformed,
contradictory or incomplete dispatch history reports `dispatch_history_unknown`.
No-target cancellation is only an acknowledgement, NEVER proof of no earlier
wire. An earlier cancelled generation does not absolve a missing later one.

An episode with a complete durable begin and no attempted publications has
positive local NEVER_WIRE coverage. Identity-zero waits remain UNKNOWN: idle
state, empty opening tables, restored state and no-target acknowledgements are
not positive history. No retroactive local NEVER_WIRE boolean is accepted.

Existing episodes restored from before this journal cannot have complete
history inferred retroactively. They stay blocked even when their known order
rows are terminal. The recorded positive controls explicitly supply a complete
future journal plus synthetic future removal acknowledgements around the
recorded order/intent evidence; these are not production clearance receipts.
This conservative upgrade behavior is the approved unknown-history rule, not
a promise that all six historical removals re-add normally.
Actual recorded AIXI/XHG legacy-episode clearance acceptance is explicitly
UNMEASURED/blocked: the capture predates the required complete journal. Actual
XHG also retains its same-episode unknown RPG block. No invented historical
read or reconstructed journal is used to change either disposition.

Any own fill, position identity, open managed position or consumed owner keeps
the filled lifecycle and managed feed. Foreign fills never manufacture V2
ownership. Waiting-request completion after a newly observed own fill does not
retire its filled episode. Restoration, seed caps, SELL-cycle retry budgets,
held/fade/purge coverage and KEEPREST's on-list waiting flip remain intact.
No historical BUY replay, authorization age waiver, sizing bypass or RETRY
shortcut is added. Unknown reasons are logged once per token/reason change.
A last dispatch check requires the draft's exact current episode, preventing
an old draft held across an asynchronous wait from entering a fresh episode.
Positive durable fill events remain blocking even if their fill-table row is
missing. Broker absence/cancel-request acknowledgement fallbacks do not count
as exact terminal broker-state proof.

## Recorded Controls And Verification

The first post-rebase comparison crossed the 16:00 session cutoff: its baseline
ran the existing SCKT PA1 replay inside the entry window, while the candidate
ran it after the cutoff and correctly refused with `resting_window_ended`.
That raw comparison has one new failure name and is preserved separately in
`preclose_mismatch_unit_pair.json`, not silently relabeled PASS. The exact
isolated SCKT test on unchanged 4805 reproduced the same refusal at
2026-10-06 16:06:38 ET; see `baseline_clock_control.json`. The full
after-close baseline comparison confirmed zero new candidate failure names.
Both Validate runs at historical head 3ff2a7f2 then failed only this same
SCKT control (6,608 passed / one failed each). Those failures are retained in
the PR receipts, not substituted by an earlier successful main run.

The human now authorizes the one test-only simulated-session pin. Exact test:
`tests/unit/test_roundup1.py::test_recorded_sckt_pa1_resubmit_recomputes_ceiling_then_serial_lane_sizes_wire`.
OMS/NFQ, initial event, market metadata and quote clocks use SCKT's recorded
submission, `2026-10-05T16:47:03.808217+00:00` (12:47 ET). The actual NFQ
clock/window predicates are positively asserted; every original wire-price,
quantity and one-submit assertion remains. No runtime source, trading window,
authorization age, sizing or retry rule changes. The pin passes after close.
Fresh unchanged-current-main 4805 reproduces the original failure at
2026-10-06 16:30:07 ET on inherited normal PATH.

Read-only coordination inspected Sagan's PR #1101 at e624ed13. Its complete
test file and this lane's pin have the same SHA256:
`464e3fbf5209335cf066bad3ab55c796a7daf43c376ce9badf9c8f7f80f33e50`.
No WEBULL429 runtime change was imported and no other branch/worktree edited.
Preliminary venv-first runs were interrupted and are not final pair receipts.
The final pair invokes the shared venv Python with inherited PATH unchanged.

The initial evidence-only checkpoint remains
`a2de4ec07323d2c30d844a62fea31725e4a717a9`. Production capture was
2026-10-06 13:30:13.519782 ET. Counts remain six removal markers / three episodes
today and 63 retained markers / eleven dates; these are not clearance receipts.
Raw SHA256:
`09de15282a22bcef6db5063a43f269a3d7aa217a5f882ac20e78afe6dcefcd6b`.
Released fixture SHA256:
`f0a1c09c9d4bca902b52934528d23ea7802e1c19e615109a0a3c2f4b3efd9f02`.

- AIXI recorded terminal/no-wire rows clear with complete controlled coverage;
  its different older unknown ticket is not retired or bypassed.
- XHG broker-reject-only subset clears with complete controlled coverage;
  its actual same-episode unknown RPG ticket still blocks.
- AIFA includes all eight order generations. The final Webull cancel at
  11:22:09.772 plus fifteen-second settlement prevents clearance at 11:22:11.
- JAGX foreign fill/ambiguous owner and MI own-fill controls stay closed.
- The actual SQLite/FakeRedis/mock-adapter ordinary OMS rollback probe still
  observes one possible mock wire, zero surviving opening rows and two actual
  local no-target receipts. It now returns `clear=false` with the unresolved
  dispatch reason; see `defensive_counterexample.json`.
- Latest normal-PATH focused acceptance: 812 passed, including 87 CLEARWAIT1 cases,
  the complete recorded ROUNDUP suite, all-ON
  settings, OWNMIX, Restoration, seed-cap, RETRY-ONE and held-feed regressions.
- Latest full unit pair invokes shared venv Python with inherited normal PATH
  unchanged and PYTHONPATH=src:. on the frozen candidate versus immutable
  current main 4805 archive: candidate 6,556 passed / 56 failed (279.81s);
  baseline 6,467 passed / 58 failed (280.01s).
  Exact failure-name comparison: ZERO new failures. Baseline-only names are
  the authorized SCKT clock fixture and archive executable-mode control.
  Remaining 56 names match exactly, including measured Mac shell dependencies,
  nine sync-only subprocess failures and dead-consumer timing control.
  The earlier venv-first 3ff pair is preserved in
  `pre_clock_pin_unit_pair.json`; its 48/49 counts are not normal-PATH counts.
  Full failure names, log/XML hashes and frozen
  source hashes are retained in `defensive_unit_pair.json`.
- Sixteen CLEARWAIT1 guard mutations were killed, including missing history,
  later lost dispatch, broker-terminal origin, either-account cancellation,
  XHG unknown ticket, AIFA settlement, fills/positions and positive no-wire
  classification. See `acceptance_mutations.json` for controls and guard lines.
- Replay/backtest: 71 passed. Ruff, marker isolation and diff whitespace passed.

The existing all-ON mutation harness killed five of six mutations. Its
`gap_authorization` mutation also survives on unchanged c21 (41 tests pass):
Restoration rejects that selected path before the mutated branch. This is a
baseline coverage gap, not a CLEARWAIT1 behavior regression; no all-ON mutation
sweep PASS is claimed. The same 41-pass gap mutation was also verified on
unchanged 4805. The current accepted RETRYOFF1 rule is
`enabled=true, max_retries=0`, NOT flag OFF. Added CLEARWAIT1 controls prove
fresh unfilled release spends no retry budget, while actual fill/close
transitions remain consumed through removal/re-add at zero retries.
The prior flag-OFF confirmation-close DISAGREE is obsolete-scope historical
evidence, not a disagreement with the current accepted true/zero rule.
No composed KEEP/RETRY card PASS or historical AIXI 10:22 outcome is claimed.

The original attachment explicitly authorizes default-ON at deploy with a
rollback-only switch. That separate switch and expected-ON registry are wired;
this build does not authorize activation. The preceding pair used
`c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`. The parent explicitly requested
a pure rebase onto `4805ddc81184c76b4d5cef5c483c809edb666fe6` before readiness;
the rebase completed without conflicts at
`c3ad424df9a8a6f70d55d9a92c983aee5de5941e`. All eight commits are
patch-equivalent by range-diff; only the five main ORBPAGE1 files changed
between the old and rebased trees. The latest full pair and CI must cover
that baseline and the rebased candidate. The latest full pair does so.
Both latest push and pull-request Validate runs are required before a ready
review pin. Their exact-SHA receipts will be published with the PR, never
in the parent's shared handoff. The inherited fixture blocker has an
authorized test-only remedy; the new full pair and both new-head Validate
receipts are required before readiness.
The PR stays draft until the gates pass; no readiness or production eligibility
is claimed. Conditional review target: 2026-10-06 17:00 ET, subject to the
final full pair and both latest Validate receipts.
