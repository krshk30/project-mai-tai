# LINE=CHART restoration build checkpoint

**DRAFT CHECKPOINT ONLY. NOT READY FOR REVIEW/PIN/INSTALL.**

## 2026-10-06 Frozen Integration Milestone

Sole writer in the operator-released `line-chart-restoration-1006` checkout.
The five earlier primitive commits were rebased without conflicts onto exact
main `03b26293624f87e43eb167431fc308b1aec309b3`; merge-base verifies that
identity. Main's service blob is `b5ff34f3`, which is a FILE identity, not the
branch base. Installed #1093/#1094 are ancestors; OMS recovery/ownership files
have zero diff against that main. Own worktree Codex commit marker is installed.
No ROUNDUP/shared-handoff write, production action, push, merge or PR.

Provider/service integration is implemented as an OFF draft. Catalog entry:
`strategy_schwab_1m_v2_line_chart_restoration_enabled=false`. The bar poll uses
a strict 04:00-anchored response with no cursor/250-bar truncation. It rejects
foreign symbols/windows, malformed/duplicate candles, pagination/truncation,
missing current closed candle and unknown/empty payloads. The manifest records
IDs and a fingerprint computed from the provider response, not DB success/count.

The source batch retains older/backfilled values even when C3 skips their
normal callback. Rebuilds run in an independent event-driven task on frozen
inputs, ordered per symbol and coalesced by revision. Publication checks the
active ledger, fresh process/re-add epoch, session, revision, current bar,
coverage fingerprint and GAPHOLD reset boundary. Math, corrected confirmation
cache and readiness publish without an await. Initial/re-add/restart/revision
repair never emits historical flips. Only an unchanged adjacent current live
append can reach the ordinary signal readers. Recorded RETO remains long at
11:18/11:21, line2.0639, without the false SELL/rest.

First/resting/reprice/reclaim, quote/stream crosses, RPG replacement admission
and direct/main/fanout order drains use readiness/version fences. Cancels and
exits retain their delivery paths. Publication does not replay order state,
ownership, consumed slots or retry budgets. Same-session removal retains these
fields while revoking arms and cancelling both legs. Pending confirmation uses
the corrected same snapshot; issued one-shot evaluations are not repeated.
DB session-skip, boot hold, sparse-bar TR clamp and GAPHOLD reset/clean2xperiod
wait are retained. Restoration does not implement the independent pause lane.

### Fixed Replay Failure

The parent's 107PASS/1FAIL working focus exposed the provider/service test
feeding through11:27 but asserting11:21's2.0639. Full recorded11:27 produces
2.22998328139567. The service test is now pinned explicitly through11:21 and
still asserts2.0639. A separate11:27 control asserts2.2300. No candle value or
recorded11:18/11:21 expectation was weakened.

### Measured Focus And Refresh Policy

127PASS focused:44 integration +12 primitive + existing flag/gap/sparse controls.
Earlier broad service/boot/resting/EH/RPG/confirmation focus:237PASS.
Scoped Ruff and diff-whitespace checks PASS. All tests explicitly use this
checkout's `PYTHONPATH=src`. Recorded fixtures retain source/created_at;
13 actual entry-line controls are committed with raw provenance and pass.
These 13 controls compare indicator state/trail, not a complete replay of
recorded order placement, cancellation, ownership and consumed-slot lifecycles.
Broader restoration-ON composition with the installed ALL-ON controls is pending.

Removed the second serial full-REST-fetch/sleep-per-symbol lane after parent
review. Anchored history now REUSES the normal bar request, with at most four
concurrent source fetches. The configured15s interval is a full-cycle cadence
in the ON lane; bar requests are capped at90RPM, reserving30RPM. At16 symbols,
configured source demand is64RPM before errors/slow responses, plus the
existing default12RPM quote poll. Worker has no per-symbol cadence sleep and
source-fetch slots are released before ordinary persistence callbacks wait.

Controlled16-symbol test uses sixteen named copies of recorded RETO prices,
10ms simulated source delay and blocked ordinary callbacks. All16 publish
before those callbacks release: latest run0.176s, fetch concurrency<=4.
Actual late-REST callback -> ordered worker -> publication test:0.008s after
the final recorded missing RETO row. These are LOCAL CONTROLLED measurements,
not real provider network, runtime DB, broker or historical chart coverage.

### Outstanding Safety/Readiness Evidence

Full unit pair completed with identical venv-first PATH and PYTHONPATH=src:
base03b:47FAILED/5894PASS in249.60s; frozen source checkpoint62de631f:
53FAILED/5944PASS in251.85s. All47 baseline failure nodes are retained and
six nodes are added. Raw logs: /tmp/line-chart-baseline-03b2-unit-20261006-path.log
and /tmp/line-chart-restoration-62de-unit-20261006-path.log. First attempts
omitted venv from PATH and were stopped; they are not evidence. Parent's7823
control is not substituted for this branch's exact03b base.

Two added ALL-ON catalog failures are fixed by updating only expected check
counts139/147 to140/148; no ROUNDUP entry or behavior is copied. Final focus
passes163 cases in3.76s: test_line_chart_restoration.py,
test_line_chart_restoration_integration.py, test_schwab_1m_v2_gap_hold.py,
test_schwab_1m_v2_gap_hold_prints.py, test_schwab_1m_v2_atr_bar_gap.py,
test_expected_flags_check.py and test_all_on_pm.py. Raw:
/tmp/line-chart-final-checkpoint-focus-20261006.log. Scoped Ruff and diff check
pass. The full suite has NOT been rerun after this count-only correction;
there is no final-head full-suite/no-regression claim.

Four added failures remain concrete checkpoint blockers:
- test_no_test_reimplements_logic.py::test_no_test_computes_its_expected_value_from_production_code:
  the new provider fingerprint test derives its expected hash from production
  history_fingerprint instead of independently pinning the recorded literal.
- test_pmprint1_tonight_flags.py::test_s6_catalog_matches_all_on_live_set_and_147_checks:
  another total-count assertion still expects147 instead of148.
- test_rpg1_pending_first_quote.py::test_quote_callback_emits_both_legs_without_waiting_for_bar:
  its SimpleNamespace service harness lacks _line_draft_allowed.
- test_v2_held_symbol_exit_coverage.py::test_held_delisted_symbol_can_never_enter:
  its _EmitHarness lacks _line_draft_allowed.
The latter two expose new emitter-method coupling even in OFF tests; do not
waive them as merely environmental. No test or safety gate is weakened. Source
work is frozen at the parent's request rather than claiming completion.
All four residual nodes reproduce in a final targeted run:4FAILED in1.95s,
/tmp/line-chart-final-residual-focus-20261006.log. Sole-writer ownership is
released to the parent after the local handover commit; this lane will not make
further writes without renewed assignment. No push/PR is authorized.

Live provider full-window delivery/omission behavior and shared REST quota
composition remain UNMEASURED. Structurally valid full response is the adapter
contract, not proof that the historical feed served every traded candle.
No DB count, successful read or recent bar certifies missing interior history.
The seven September first-rest decision-time/source-coverage controls remain
UNMEASURED; retained retrospective prices do not supply their provider manifests.

Real network and confirmation/intent delivery tail latency are UNMEASURED.
With16 symbols, four concurrent requests and a10s socket timeout, the SOURCE
budget model is four waves/40s; this is not a hard end-to-end wall-clock bound.
Socket/read behavior, worker/post-publication delivery and other REST consumers
require measured composition before activation. New bars revoke readiness;
failed/stale source requests or worker results cannot authorize buys. No claim
of instantaneous restoration or guaranteed live flip recovery is made.

Concrete scheduling limitation: source-fetch semaphore slots are released
before persistence, so the current batch can publish while ordinary callbacks
wait. However, the source pass still gathers those callbacks before its NEXT
cycle. A stuck callback therefore has no established next-cycle bound. The
ordered worker also awaits confirmation/intent delivery after each publication;
a stuck delivery can delay later symbols. The controlled fast-publication tests
do not establish bounded ongoing service under these conditions. This is an
activation blocker, not merely a missing real-network timing number.

Parent review, resolution of added failure nodes, final-head full paired proof,
broader ON composition and
live source/quota/latency evidence remain required. Default OFF draft is coherent
for a local commit; it is not complete, review-ready, installable or push-approved.

### Final Rebase And Range Proof

Clean checkpoint62de631f was inspected before the final fetch/rebase. Remote
main remains03b26293624f87e43eb167431fc308b1aec309b3; rebase says up to date.
No conflicts occurred, so there are no conflict resolutions to review.
Range-diff7e10baf0..25c31d39 against03b26293..ff062bcf marks all five primitive
commits '=':84f3d0ef->778bfbf2,2a86cebd->eb2104d9,475fe844->c2661128,
c9aca7c0->9f003969,25c31d39->ff062bcf. The integration checkpoint follows
that range; the handover follow-up changes only BUILD_STATUS and two ALL-ON
catalog assertions. Parent-reported checkpoint focus112PASS/1.59s on62de631f
is independent evidence, not a substitute for the failing full pair.

## Historical 2026-10-05 Primitive Checkpoint

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
JAGX's34-minute re-add hole is now pinned at the admission boundary:33 stored
REST rows11:18..11:50 were created12:20:12.521834 ET. The live11:51 bar arrived
11:52:14.283561. At11:52:15 the33 rows are absent; admission refuses until they
are supplied. This is not yet a mathematical rebuild or historical provider
manifest proof; the full source response remains a controlled test input.

## Checkpoint Verification

12 tests PASS in test_line_chart_restoration.py; scoped Ruff PASS. Tests pin
late-bar admission, source/start/end/completeness,255-row retention, re-add
epoch fencing, off-callback worker execution, revision invalidation and frozen
provider values and source-value mismatch refusal. No full-suite pair, mutation
census or implementation PR yet. The fingerprint test uses two recorded symbol
histories, not invented candle prices; coverage manifests remain controlled.

The full255-row RETO worker/oracle control now pins long/trail2.0639 with no
SELL at11:17 or11:18 and still long at11:21. The11:05 oracle trail is1.9629;
2.0639 refers to the later continuous line, not the re-add instant. This is a
pure recorded-bar replay through the admission worker; no live ATR publication,
order gate or historical provider coverage is claimed by that test.

Next: provider manifest + service epoch integration, ordered session mathematics
and atomic snapshot publication without replaying entry state; then recorded
RETO/JAGX/September/restart controls, BENF and13-entry controls, full paired
suite and composition. Unknown coverage stays fail-closed. Source/coverage
completeness cannot be inferred from a recent bar or successful DB read.

## Read-Only Integration Audit,14:04 ET

Live files remain identical to merged main7e10. The current REST client cannot
attest a complete04:00-anchored session: `schwab_v2_rest_client.py:277` uses a
rolling window, cursor filtering, malformed-row skips and returns only bars.
It can return more than250 rows, but that is not completeness evidence. Its
cursor advances before callback completion and survives desired-symbol removal
(`:172`). A dedicated anchored response manifest is required; the controlled
coverage manifests in these12 tests do not supply live provider proof.

The DB seed at `services/schwab_1m_v2_bot.py:4325` reads latest250 rows and
`_db_seeded` prevents later rereads. No current hook notices external late
backfill/revisions. Stream/REST ingestion and pending drain at3734/3804/3870
must update restoration history even when the normal callback rejects an older
bar; C3's old-REST skip at4449 must not discard restoration evidence.

Restart setup at786/881, add/re-add/remove at3214/3305 and session rollover at2071
must initialize/invalidate per-symbol epochs before callbacks. Existing fresh-bar,
boot timeout and strict-admission readiness cannot substitute for full history.
Removal may discard consumed-slot memory (`strategy_core/schwab_1m_v2.py:2687`),
which restoration must not interpret as a new opportunity.

Do not rebuild through `on_bar` or completed-bar evaluation: those mutate
trading state, and equal-minute corrections do not recompute ATR. The math-only
snapshot fields at3618 are the publication boundary; `seed_atr_state`:4028
currently mutates live state and is not an isolated worker. Preserve slots,
working orders, ownership, retry budgets and pending SELL delivery. Publish
math/readiness atomically after checking active symbol/session/epoch/revision;
never replay a historical BUY/SELL or a cancellation.

Readiness gates must cover first-entry/resting/reprice, reclaim/reactive, both
EH crossing paths and RPG replacement authorization, not only one callback.
Confirmation snapshots at bot4510 and late-fill evaluation at1510 also read
the line: correcting only current ATR fields leaves stale exit evidence.
Version/reconcile unevaluated snapshots without repeating issued exits.
The implementation remains NOT live-wired or PR-ready. This read-only audit
adds no source, service, database, flag or install change.
