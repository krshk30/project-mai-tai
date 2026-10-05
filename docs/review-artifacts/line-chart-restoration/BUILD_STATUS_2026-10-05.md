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
