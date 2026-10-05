# RPGSTUCK1 R20 Runtime Exhaustion Wake

One follow-up atop `c6548875bab15fb52bf35ca491e2e0ea63cc7148`; no rebase.
Own fresh baseline remains main `7e10baf0319da796b84934fe38994f6db4fcfc0b`.
Only production change: `oms/atr_reprice_runtime.py`. No OWN/PM/other-worktree or
shared-handoff edits, production/service/broker/ledger writes, merge or install.

## Defect And Narrow Fix

The running retry loop excludes held_unknown from scans after startup. A waiting
ticket can consume read30, then change to held_unknown/readback_budget_exhausted
on its next scheduled turn. Previously it never entered the one-time strict
rejected-old probe route until restart. Its already-claimed blocked notice could
also return before any later wake code. This is a runtime recovery gap, not an
authorization or ledger policy change.

Immediately after controller.advance and BEFORE the blocked-notice guard,
the non-held -> held edge checks existing rejected-old eligibility using
`asyncio.to_thread`. Only an eligible edge adds its exact token to the existing
pending set and signals the retry task. The next scan excludes unknowns as before,
reads only that pending token, rechecks eligibility and emits one proof-only tick.
Retry-task eligibility DB checks also run off-loop. The serial consumer retains
the existing durable claim-before-GET and all strict identity/zero/fill guards.
No admission, resubmit, cancel budget reset, ownership release without proof,
periodic unknown scan, quiet DB work, age rule or new timeout is introduced.
The existing serial probe/admission path itself is otherwise unchanged.

Pending set deduplicates repeated wakes; the durable probe marker prevents a
second GET after replay/crash/restart. A crash before tick delivery is recovered
by the existing startup scan, not a timer. Eligibility/DB failures retain unknown
ownership; startup remains the bounded recovery route. Evidence arriving only
out-of-process without notification still requires restart as previously reported.

## Complete Held-Transition Audit

| Transition / Site | Next Check |
| --- | --- |
| New local preparation with unproven target, runtime:180 -> journal.prepare_local:100 | held_unknown/exact_old_order_unproven. Existing startup scan or matching committed exact-generation notification/hash schedules proof. Missing evidence never gets a periodic tick. Proven pre-wire holds prepare clear instead. |
| waiting or fills_waiting read budget exhausted, coordinator:208 | New runtime edge:260 queues ONLY eligible rejected-old token. Fills/no_rebuy, already-claimed, wrong account/identity, non-rejected row are ineligible and retain ownership. |
| Eligible exhausted ticket restored at startup | Existing startup eligibility route; same durable lifetime claim, no cancelled/read-budget reset. |
| Probe unknown, failed GET, crash or positive fill leaves held phase | No new transition or new wake; durable claim blocks repeats. Positive fill retains no-rebuy and existing accounting/UNMEASURED route. |
| Ineligible exhausted ticket later gains a rejected row without the exact-old notification route | No quiet scan or invented proof. Restart re-evaluates existing eligibility; claimed/no-rebuy tickets remain ineligible. |
| Unknown replacement dispatch | submit_unknown, NOT held_unknown. Existing exact-client reconciliation stays unchanged; never resubmit. |

Source search finds only two held constructors: local preparation and read-budget
exhaustion. Notice stamping and probe results retaining held are not new entries.
No hidden hold constructor was omitted or relabeled as clear.

Explicit limit: a readback_budget_exhausted ticket that is INELIGIBLE at its
runtime edge does not wake later merely because its old DB order becomes
committed rejected. The existing committed-generation notification filter is
exact_old_order_unproven ONLY, not readback_budget_exhausted. A later restart
can re-evaluate eligibility and admit the unclaimed one-time probe; age/quiet
ticks never do so. Already-claimed or no-rebuy still forbids that probe after
restart. R20 deliberately does not broaden this notification filter.

## Real Loop And Review Guards

`test_r20_recorded_reto_runtime_exhaustion_wakes_one_proof_then_quiet` retains the
actual recorded RETO ff6464ff ticket/order and full strict broker body. Rewinds
only the controlled replay phase to waiting/reads29, keeping its existing notice.
One real retry loop pumps emitted ticks through the real serial stream handler;
no restart or direct probe injection. Clock/pause are controlled, not wall-time
broker observations. Eight combinations cover rejected-zero/unknown answers and
eligible/already-claimed/accepted-row/no-rebuy gates.

Eligible sequence: normal read30 -> waiting -> budget exhaustion/held -> exactly
one additional proof tick/GET. REJECTED-zero yields refused/proven clear; unknown
stays held with completed lifetime claim. Ineligible cases get no proof tick/GET.
Each case verifies180 empty turns with no extra scan, SQL or tick, no opens or
cancels, reads remain30. The proof GET is additional to normal read30, not a
new normal read budget. OFF/printON tonight flags remain the actual fixture flags.

Notice replay is separately named
`test_held_notice_replay_claims_once_and_preserves_unknown_ownership`; NOT R15.
Reviewer R15 is existing
`test_b4_hold_authorization_waits_for_unproven_old_but_expires_proven_old`.
Both entry/gap-hold proven expiry and unproven WAIT remain pinned. Exact reviewer
literal mutation changes `"expired" if old_buy_proven_clear(job) else "wait"`
to `"expired"`: two unproven cases assert-red. Opposite wait mutation separately
makes two proven cases assert-red. Existing R17-R19 strict proof/claim/no-rebuy,
B5 startup/notification/dedup/quiet work, all14 recorded startup, existing50 fill
repeats, canonical Schwab wire sequences and legacy OFF behavior stay in focus.

Literal disabling the new edge condition yields two eligible runtime assertion
failures. No import/anchor error counts. All67 prior controls plus four explicitly
retained R20/R15/notice runs are rerun. The exact R15 literal duplicates the prior
B4 control and is not claimed as a new distinct falsifier. Fresh counts, exact
IDs, both full failure sets, source/test hashes and raw assertion outputs are
retained in `verification-r20.json` and `r20-*-mutant*.txt`.

Parent independently ran the provisional frozen snapshot and exact `and False`
wake mutation: two eligible rejected-zero/unknown assertion failures, no branch
edit. Raw `/tmp/oct5-parent-r20-mutant.txt` SHA256
`e3e10dcbb6e838de02442a505ecfd7e29db3376aa22175c833801e5fd2cd4f86`.
This independent rerun is separate from the own mutation-run count.
Read-only hash verification of the parent's provisional composition confirms
the runtime/new-test content equals both frozen hashes below; no parent files
were edited. Parent still owns final exact-head full composition validation.

Final source hash runtime:
`ff33fd285f23b7da0ac5d8beda85f5d5ad680bec9321f567b95b862e5d6cf137`.
Final new test hash:
`9e5c39c9b9d6502105d8006413961a068447ddfbd9079d02b8e6afe6f8c6d8a4`.
The first targeted prototype lacked controlled clock advancement and was stopped;
the corrected test is the retained final scenario. Initial full/focused runs were
superseded after correcting the notice test label; no source semantics changed.
Parent owns independent new-head composition/review. Historical c654 composition
and CI are not inferred to cover this changed source. All as-of census/strict-body
limits remain unchanged; standalone full is not globally green with baseline56.

## Frozen Verification

| Suite | Passed | Failed | Skipped | Seconds |
| --- | ---: | ---: | ---: | ---: |
| Fresh main 7e10 full unit | 5500 | 56 | 0 | 267.75 |
| Frozen R20 full unit | 5692 | 56 | 0 | 309.22 |
| Focused RPG/PM/ownership/strategy companions | 1378 | 0 | 0 | 111.87 |

Exact XML failed-name comparison: added `[]`, removed `[]`. The full pair
therefore retains all56 baseline failures, not an unconditional full-suite PASS.
Fresh retained mutations: 71 assertion-red control executions, including the
explicit repeated R15 literal; not71 unique falsifiers. New runtime-wake mutant
is RED in both eligible real-loop cases; exact R15 literal is RED in both
unproven entry/gap-hold cases. All67 prior controls remain RED.

Full commands use `PYTHONPATH=src` and the project venv interpreter with
`-m pytest tests/unit --junitxml=...`; fresh main and frozen head XML are
`/tmp/rpgstuck-r20-main-full.xml` and `/tmp/rpgstuck-r20-frozen-full.xml`.
Focused XML is `/tmp/rpgstuck-r20-frozen-focused.xml`; its complete1378 exact
test names, both56-name failure lists, XML hashes, final production hashes and
all71 mutation outputs/hashes are in `verification-r20.json`.
Reproduction drivers are `run_focused.py`, `run_b_mutations.py`,
`check_fixture_mutations.py`, `check_r20_mutations.py` and
`collect_r20_results.py`; focus additionally includes the recorded startup,
wire-sequence, review-completion, later-census, new real-loop, PM and ownership
companion modules retained in the JSON exact-name list.

Source/tests were frozen before both final runs and are unchanged afterward.
Only evidence/report files were added afterward. Runtime deployment remains
unperformed: any future approved install needs the OMS restart plus strategy
companion pairing already specified by the existing restart list, not a lone
live OMS change. New-head CI, independent composition and fresh reviewer pin
must be checked separately; historical c654 results are not substituted.
