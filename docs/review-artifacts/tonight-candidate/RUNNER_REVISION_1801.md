# D1/D2/R1/R2: corrected literal runner for exact-byte review

Application remains`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`;
schema0021->0022, four switchestrue, exactlythree PMenv edits, exact sequence
v2stop->strategystop->OMSstop->0022->OMSstart->v2start->strategystart.
Abort still pages actual states and starts nothing. No staging, approval
file, install, migration, env/service/ledger/token action has occurred.

## D1 Fresh Census

Authorized serialized read began18:05:06.465996 ET and completed
18:05:22.265106 ET. Process/helperrc0. Helper bytes SHA256
`5d6007ad5afd2f64533d7c300c6e7eba3fe13efd91ba84e31593277fc456d4d7`.
All14 reviewed identities matched; all11 linked broker parents terminal:

| Account | Client order id | Direct terminal status |
|---|---|---|
| live:schwab_1m_v2 | schwab_1m_v2-APUS-open-ca105a42910f | CANCELED |
| live:orb | schwab_1m_v2-APUS-open-dc8c75efb6c7 | CANCELLED |
| live:schwab_1m_v2 | schwab_1m_v2-VEEA-open-537edc9105ab | CANCELED |
| live:schwab_1m_v2 | schwab_1m_v2-APUS-open-eb0f432e543d | CANCELED |
| live:schwab_1m_v2 | schwab_1m_v2-RETO-open-7f273d444a66 | REJECTED |
| live:schwab_1m_v2 | schwab_1m_v2-MI-open-9549c40b8349 | CANCELED |
| live:schwab_1m_v2 | schwab_1m_v2-SCKT-open-1f63cbaaf087 | CANCELED |
| live:orb | schwab_1m_v2-SCKT-open-074e9d445a42 | CANCELLED |
| live:orb | schwab_1m_v2-SCKT-open-494aac58d0c2 | CANCELLED |
| live:orb | schwab_1m_v2-SCKT-open-69a4f7276ad3 | CANCELLED |
| live:orb | schwab_1m_v2-SCKT-open-9a10bc5b0102 | FILLED |

Journal digest stayed
`ab242bfc06e511a36b50c5a0830d94aa384463a4b0df81bd20d57814a861063d`.
No failing client ids. Webull exact details serialize,2seconds between starts;
no grant, order poll, retry or persistence. Boxe1ce3b39 remains source.
Receipt`CENSUS_D1_REREAD_2026-10-05.json` is an extracted complete11-parent
array with vendor bodies, header/tail verdict and capture limits. The tool
truncated the middle of the large output, so this is NOT a whole raw transcript.
Schwab OMS-sync list is12h past/1h future, not all-time orphan proof. No fresh
flatness is inferred; the runner repeats every required execution gate.

## Corrected Runner

`job/run.sh:read_only_retry` wraps ALL flat/helper and census calls, including
post-stop calls. Logs attemptN/3 and resultingrc. Onlyrc2 causes a60second
wait; a thirdrc2 returns2 and traps; rc1 measured or unexpected code returns
immediately. No gate/allowance policy change, no helper hash change.

Census previously returned2 for every explicit STOP; it now returns1 for
measured identity/population/schema/source drift, nonterminal rows/orders,
or nonterminal exact parents, and2 for unreadable HTTP/SDK/SQL, incomplete
envelopes/overflow or unknown exceptions. No internal retries; the runner
owns the bounded retry. Identity-mismatched readable orders stop immediately.

`window_now` exists only before the firststop, latest immediately after the
unmodified v2 gate and identity check. It requires10-05 before19:15 ET.
Once v2stops, no clock/date abort runs between any stop/start or close-out.
Read-only proof helpers likewise have no post-stop time fence; data/memory,
eviction, identity and no-buy checks still apply. Delayed starts are not
retries or extra restarts. This release has NO after20:00 start option: if
19:15 is missed, re-cut for20:05+ and observed rotation before any new attempt.

D2 no longer adds an approval.json field. Exact approved runner bytes carry
the reviewed inactive-paper disposition. The candidate pins truePID0 and
unchanged identity logic stillFAILS. Its generated report is annotated with
`EXPECTED D2: momentum-paper inactive/PID0; existing active-check FAIL retained`,
and the preopen journal states the same. Neither failure counts nor routing
are rewritten. A report-annotation write failure also recordsFAIL.

## Local Tests

All29 tests in job/test_actions.py, job/test_runner_retry.py and
job/test_census_exit.py PASS; Ruff wholejobPASS; BashsyntaxPASS.

| Requirement | Test name |
|---|---|
| R1 transient then success | test_unreadable_then_ok_retries_once_and_proceeds |
| R1 bounded failure | test_three_unreadables_stop_after_exactly_three_attempts |
| R1 no measured retry | test_measured_blocker_or_unrecognized_code_never_retries |
| R1 every caller wrapped | test_every_flat_and_census_call_uses_retry_but_policy_is_not_changed |
| R1 exit-code distinction | test_census_measured_blocker_one_unreadable_two (7 cases) |
| R2 no in-sequence clock | test_clock_gate_only_before_first_stop_never_between_stop_and_start |
| R2 helper clock boundary | test_completion_proofs_do_not_abort_on_clock_after_first_stop (2 clocks) |
| D2 no extra field | test_d2_no_new_approval_field_and_inactive_identity_failure_is_retained |
| D2 report, no bypass | test_d2_report_annotation_preserves_identity_check_and_verdict |

Tests execute the literal Bash function with controlled exit codes and record
exact60second sleep requests without real sleeps; no broker or production
calls. The live census is separate and exactly hash-bound above. No whole-run
or migration PASS is claimed; exact corrected release awaits reviewer approval.
