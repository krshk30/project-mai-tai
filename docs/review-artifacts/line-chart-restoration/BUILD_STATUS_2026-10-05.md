# LINE=CHART restoration build checkpoint

**NOT READY FOR REVIEW/PIN/INSTALL. No service integration or active flag.**

Branch codex/line-chart-restoration starts from main
7e10baf0319da796b84934fe38994f6db4fcfc0b. Accepted independent assessment
5a12d4d2 is retained on codex/gapkeep1-independent-assessment.
Restoration is first; the pause lane is not built here. Neither is tonight.

## Implemented Boundary

session_line_restore.py separates immutable session input, off-event-loop
rebuild and revision/epoch-checked admission. A DB read cannot attest coverage.
The provider must supply a complete full-session request/response manifest,
including the current closed bar, candle IDs and a value fingerprint computed
from the provider's own response. Matching IDs with different candle values
cannot authorize publication. Missing/conflicting history,
wrong source/window, a new revision or a re-add epoch prevents admission.
The worker receives frozen bar copies, not mutable provider objects. It has
no trading callbacks, order mutations, slot resets or historical flip emit.
All session rows remain available, not only the last250.

This is the admission primitive only. It does NOT yet rebuild/publish ATR,
gate first/reclaim buys, repair confirmation snapshots, wire provider coverage
or implement the unchanged unrecoverable-hole/GAPHOLD fallback. No claim that
RETO2.0639 or the full restoration card is implemented. Do not open a review PR
as complete until those integration proofs and recorded controls pass.

## Own New Recorded Data

Read-only root Settings read on mai-tai-vps at2026-10-05T17:13:34.463913Z.
SQL transaction READ ONLY,5s statement timeout, LIMIT1921 with refusal above
1920 (two complete04:00-20:00 sessions). No Redis/broker/service writes.
444 rows: RETO255 through11:27 ET; JAGX189 through13:12 ET. Raw:
/tmp/line-chart-restoration-fresh-bars-20261005.json.
The retained normalized fixture includes source and created_at values.

The RETO test uses the actual16 REST bars10:49..11:04 created11:10:19.
At11:10:18 those16 rows are absent; at the later snapshot they are present.
The current11:09 bar had already arrived11:10:03, not11:10:00: the first test
cut incorrectly counted17 late rows and failed; fixing only that measured cut
to11:10:18 leaves the real16-hole test passing. No recorded prices changed.

Provider coverage manifests and job scheduling in these unit tests are
controlled inputs, not a historical decision-cache coverage measurement.
JAGX current-session rows are captured, but its requested34-minute re-add
rebuild scenario is not yet asserted by this primitive.

## Checkpoint Verification

10 tests PASS in test_line_chart_restoration.py; scoped Ruff PASS. Tests pin
late-bar admission, source/start/end/completeness,255-row retention, re-add
epoch fencing, off-callback worker execution, revision invalidation and frozen
provider values and source-value mismatch refusal. No full-suite pair, mutation
census or implementation PR yet. The fingerprint test uses two recorded symbol
histories, not invented candle prices; coverage manifests remain controlled.

Next: provider manifest + service epoch integration, ordered session mathematics
and atomic snapshot publication without replaying entry state; then recorded
RETO/JAGX/September/restart controls, BENF and13-entry controls, full paired
suite and composition. Unknown coverage stays fail-closed. Source/coverage
completeness cannot be inferred from a recent bar or successful DB read.
