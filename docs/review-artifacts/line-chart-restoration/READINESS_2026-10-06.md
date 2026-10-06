# LINE=CHART Restoration - Scoped Review Deadline

**Review-ready ETA: Tuesday 2026-10-06 at21:00 America/New_York.**
This is a target, not a pin, completed replay or install claim. Reviewer review
is targeted for Wednesday morning, then a separately approved install after
Wednesday's close. Restoration remains default OFF. It is not in today's
ROUNDUP1/preopen install.

## Scope Disposition

The reviewer's October6 scope cut supersedes the full04:00-prefix admission
requirement in the historical STEERING report/PR body. Completeness is measured
over the stored session series **from its first available bar**, with no
inference of `prefix_complete` from candle count, a successful GET or a later
bar. Session-date/anchor filtering still excludes stale prior-session bars.
Printed04:00-07:00 history is the parked row32 decision, not this PR's blocker.

Interior/tail/current-bar gaps, conflicting source identity, delayed backfill
and stale rebuild revisions still fail closed. Use stored series and its
source/arrival times; do not rewrite a provider manifest to certify a DB merge.
Fresh bars alone do not release entries. No historical flip is emitted late,
no cancelled rest resurrected and no consumed first/reclaim slot unconsumed.

Pause is a **separate PR**. APUS pause/no-reset acceptance is removed from
#1097, not relabelled PASS. Sparse intervals without recoverable bars remain
held under the existing missing-data rule; no assumed tape silence or #620
clamp change is hidden in Restoration.

## Counted Remaining Work - As Of08:44 ET

Count is five independent review-readiness work groups, not the number of
individual assertions. Each requires evidence before its count goes to zero.

| ID | Remaining blocker | Done only when |
|---|---|---|
| B1 | Stored-series admission from first available bar | Source/DB/revision/current-bar fences implement the narrowed window; JAGX07:01-start is admitted only after its recorded07:03/07:04 REST repair, not on candle count or a fresh bar. Completeness removal mutation is assertion-RED. |
| B2 | Bound remaining callback/rebuild/delivery waits | Planned fixed limits: stored-reader wall6s with SQL statement5s/lock500ms; mathematics worker3s; deferred delivery3s. Timeout/late worker keeps entry_allowed=0 and cannot publish a stale result; exit evaluations are not discarded or marked delivered. Per-stage tests pin each limit. These limits are proposed, not yet implemented/measured. |
| B3 | Replays and restoration lifecycle controls | JAGX10-06, RETO10-05, MI recorded stored-series cases, late REST, re-add/restart, >250 bars and seven September attempts have exact source/time/result tables. Missing historical evidence is explicitly UNMEASURED, not replaced by invented bars. Thirteen post-reset controls and BENF remain pinned. |
| B4 | Six installed-path buy/exit composition controls | One real-path test each for PMREST1, PMPRINT1, PMFLIP1, NFQ1, RPG hand-off and ROUNDUP1: incomplete/rebuilding emits no buy and changes no installed exit; existing first/reclaim/one-entry/boot/gap/owner safeguards hold. Failure of an exit control blocks readiness, not a scope waiver. |
| B5 | Final rebased integration and review evidence | Rebase on current reviewed main, resolve integration explicitly, run assertion mutations, same-environment full unit pair with failed-name diff, focused ON/OFF suite and Validate x2; publish exact head and report before marking ready. |

Own source read at current draft head
`49e9be4b1b6fc55bbf5f26db2e729f75a9cec585`: the strict prefix check still
requires full-session provider proof; stored-read and mathematics `to_thread`
waits and post-publication confirmation delivery have no fixed wall bound.
No fresh tests or completed narrowed-scope acceptance are claimed by this file.
Earlier180-focus / paired47-failure receipts belong to the earlier checkpoint.

## Acceptance Numbers To Reproduce, Not New PASS Claims

| Recorded input | Narrowed acceptance / provenance |
|---|---|
| JAGX10-06 | Stored07:01-07:16,16 bars;07:03/07:04 created07:10:10.891523. Existing independent stored-series oracle trail5.7955, long age7 at07:16. The bot publication must equal that series and must not start long age0. Provider07:00-start17-bar5.7804 is a different input and is not substituted. |
| RETO10-05 | Sixteen10:49-11:04 late REST bars,255 retained rows; earlier controlled rebuild trail2.0639/long at11:18 and11:21 with no late SELL/rest. Reproduce through the narrowed real service path and preserve hold/slot state. |
| MI10-05 | Own428 stored rows08:15-15:59; conditional continuous14:06 short/trail9.2013,14:11 short8.6240,14:20 short8.2551. Several interior holes remain; this is not a full-chart BUY claim or a Pause acceptance. Pin entry-blocking/rebuild/exit invariants and identify unrecovered intervals explicitly. |

## Checkpoints And Slip Rule

Publish C-rows and append narrative in shared handoff #1096 at **12:00 and
16:00 ET October6**, containing exact head, remaining IDs/count, new evidence
and an explicit revised ETA if necessary. Do not reduce counts because the
clock passed, because focused tests alone pass, or because a guard was removed.

If source completeness or an unchanged-exit control cannot pass, state its
exact recorded failing case and how many hours/days it adds. Do not mark ready
to meet21:00, silently move a blocker out of scope or activate a partial fix.
After readiness, send PR/head, acceptance values, mutation table, full-suite
failed-name comparison and restart/flag plan for reviewer pin; no production
action or operator install GO is implied by this target.
