# L5/L7 literal wake proofs and later APUS wire

Continuation: [fixture-only restart clock correction](REPORT_CI_CLOCK.md) records
the actual later8cf CI three-assertion failure, controlled cause, correction and
fresh pair. This report's earlier local counts remain historical evidence.

ONE tests/evidence/report-only follow-up atop
`5ddb50f55498232915bd54171d5cc21579f43240`, no rebase. Main stays
`4987353be54c4be15d4196907dec4dd0d6103236`. The reviewer's exact L5/L7 wording
was supplied at 15:45, superseding the previous missing-text limitation.
Production implementation, settings defaults, ALL-ON catalog, prior tests and
fixtures are unchanged. No source fix was needed or made. No merge, deployment,
install, restart, process-flag, service, broker, migration or production/ledger
write; parent alone owns gate policy/allowance tests and installation planning.

## Exact New Tests

New collected module: `tests/unit/test_rpgstuck1_loop_liveness.py`.

1. `test_l5_idle_real_retry_loop_actual_new_begin_wakes_four_ticks_in_four_seconds[four-tick-liveness]`.
   Real `_run_rpg_retry_loop` completes its empty startup scan and reaches idle.
   Only then the pump calls actual `begin(h, 'schwab')` through strategy cancel
   emission and the normal serial OMS consumer. Cancel/read gives proven clear.
   Clock advances one second per turn with a 0.1s asynchronous pump. Exactly four
   loop-emitted ticks arrive at begin+1,+2,+3,+4 seconds; five scans total,
   startup once then active-only. No authorization is supplied, so phase remains
   clear and no buy occurs. This is the literal four-tick experiment, not a
   mislabeled progression claim.
2. The same test `[then-loop-placed]` retains the first four ticks, then writes
   real bot/strategy authorization with fresh controlled quote and unchanged
   ALL-ON600/300 sizing. Its helper-generated tick is discarded and never
   handled. The fifth **loop-emitted** tick advances the new ticket to placed
   with exactly one simulated adapter BUY; original cancel/read remain one each.
   Thus idle -> actual new begin -> independent real-loop phase progression is
   explicitly proved, not delegated to a separate helper placement test.
3. `test_l7_recorded_four_local_unknown_jobs_real_loop_continues_after_clear[placed]`.
   Actual `startup_harness(with_deferred=True, recorded=LATER)` (via the retained
   census helper), real bot restore, and the real retry loop. Recorded tokens
   `faa55c1f`, `ee3d0d07`, `ba108172`, `4be7cb2d` each receive exactly one startup
   tick and recover held_unknown -> clear through existing persisted proof.
   Real bot authorization is written, but every bot/helper tick is deliberately
   discarded. Only subsequent loop deliveries are handled. Each receives one
   more loop tick -> placed, four unique economic-slot Webull buys total.
4. The same L7 test `[expired-after-2000]` first recovers the same four jobs in
   the valid startup window, then advances the controlled clock across20:00
   before current authorization. The independent next loop tick expires each
   by the configured window, zero buys. The separate unchanged ALL-ON census
   still covers startup that is already after20:00 and true-unknown variants.
5. `test_recorded_apus0932_distance_refusal_releases_then_later_same_segment_webull_places_once`.
   Actual recorded mirror ticket `bd6ac0b9`, raw authorization stop5.2720 and
   original precheck market4.78 preserve attempt-three `refused` with
   `webull_mirror_precheck_deferred`, zero Webull wires. Proven old clearance and
   real feedback release account ownership. Only afterward the replay advances
   one minute and supplies a distinct **controlled later quote5.25**. The actual
   normal-placement draft builder creates a new generation in the SAME economic
   segment. Real serial OMS and actual Webull adapter/SDK serialization place
   that mirror once; four identical event deliveries still yield one SDK place,
   zero Schwab POSTs. The old refused ticket and its authorization are unchanged.
   This is not merely a strategy draft assertion, ticket relabel or original
   quote substitution; simulated acceptance is not historical venue evidence.

All five use handoffON and the retained ALL-ON set including NFQ/GAP hold.
L5 initializes ALL-ON sizing consistently at fixture construction, rather than
switching from a legacy zero-notional setup just before the progression stage.
Only loop-produced `atr_reprice_tick` messages are handled in L5/L7; ordinary
order-event envelopes and bot-produced ticks cannot accidentally rescue a
broken wake. The normal consumer's production protections are not disabled.
There is no fabricated direct `_rpg_advance`, manually injected retry tick,
restart, scan or age-clearance to make either proof pass.

## Literal Mutations

`check_l57_mutations.py` operates in memory only, with exact unique source
anchors, and requires actual assertion failures (not import/anchor errors).

| Literal source removal | Expected diagnostic | Assertion-RED cases |
| --- | --- | ---: |
| Remove BOTH `_rpg_retry_dirty = True` and `_rpg_retry_signal().set()` at `_rpg_begin_cancel` entry | Startup is already idle; zero loop ticks, ticket remains clear (even the progression helper tick cannot rescue it) | 2 L5 variants |
| Remove `_rpg_advance`'s `starting_phase == held_unknown` -> ACTIVE wake block | Each of the four mirror jobs gets only its startup tick, then parks clear despite current authorization | 2 L7 variants |

Final raw outputs are `l57-L5_begin_both_wakes_removed-mutant.txt` and
`l57-L7_recovered_active_wake_removed-mutant.txt`. Existing80 controls are rerun
against unchanged production: fresh total **82 assertion-red control executions**.
Retained R15/B4 and ALL-ON/R20 repeats are not claimed as distinct falsifiers.
The separate historical APUS literal both-PLACED experiment still fails and is
excluded from the mutation count. The reviewer's amended requirement, confirmed
by parent, permits the legitimate original distance refusal and instead requires
ownership release followed by a later eligible same-segment Webull wire once.
The new case proves that amended sequence; the old literal failure is historical
evidence, not a current unmet-case blocker. This follow-up neither relaxes the
distance guard nor claims the original refusal is PLACED. Parent owns final
policy/allowance integration; the later eligible opportunity is a separate stage.

## Paired Verification

| Final frozen suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Own fresh exact main498 full | 5577 | 56 | 0 |
| Tests-only L5/L7/APUS head full | 5869 | identical56 | 0 |
| Broad RPG/PM/OWN focused | 1555 | 0 | 0 |
| New literal proofs targeted | 5 | 0 | 0 |

Complete failed-name difference: **added[] / removed[]**. Main took206.86s,
candidate249.03s, focus100.87s and targeted4.51s. Raw full warnings311/315 and
four focused JUnit-property compatibility warnings are retained, not suppressed.

Final exact counts, focused/targeted names, both complete failed-name sets,
added/removed difference, production/settings/catalog/new-test SHA256 values,
raw XML/text hashes and all82 control outputs are in `verification-l57.json`.
Production and catalog hashes equal frozen5ddb; all prior five changed test
hashes also equal5ddb. All14 recorded startup cases, actual SCKT Fill/no-rebuy,
R17-R20/B5, first/reclaim, strict terminal-proof and historical OFF regressions
remain in the final focus and full-unit collection. Full is not globally green;
the existing main56 failures must remain an exact-name match, never a counts-only
comparison. Ruff src/tests PASS; whitespace scope is code/tests, not verbatim
raw mutation traces. Prior as-of14-ticket and simulated-market limits remain.

```text
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-l57-main-full.xml
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-l57-head-full.xml
PYTHONPATH=src <repo-venv>/python -m pytest -q tests/unit/test_rpgstuck1_loop_liveness.py --junitxml=/tmp/rpgstuck-l57-targeted.xml
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_l57_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/run_b_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_fixture_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_r20_mutations.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/check_all_on_proofs.py
PYTHONPATH=src <repo-venv>/python tests/unit/mutate_all_on_pm.py
PYTHONPATH=src <repo-venv>/python docs/review-artifacts/rpgstuck1/collect_l57_results.py
```

Interpreter is `/Users/velkris/Projects/project-mai-tai/.venv/bin/python`.
The exact expanded focused command is retained in verification JSON: previous
ALL-ON RPG/PM/OWN scope plus this new unit module. Own detached main498 is used
for the fresh baseline, no shared/OWN worktree edited. Early pre-final runs were
superseded after fixing pump filtering/authorization freshness, a fixture import
lint issue, and adding the explicit L5 progression variant; none are counted as
the final frozen full/focus. No production guard was changed to solve a fixture
failure. Final test source is frozen before final full/focus collection.

## Prior CI Infrastructure

Parent's old pull Validate run `37363497022`, job `111943287588`, did not acquire
a hosted runner. This was independently re-read through GitHub's read-only API:
head5ddb, `runner_id=0`, empty runner name, `steps=[]`, conclusion `cancelled`;
failure annotation: "The job was not acquired by Runner of type hosted even
after multiple attempts". Complete job/annotation bodies are retained as
structured data in `l57-prior-ci-infrastructure.json`, with hash in verification.
No unit/integration test step started; this is not a test hang or assertion
failure, and it is also **not a CI PASS**. A new head requires BOTH exact-head
Validate runs SUCCESS plus a fresh committed independent review pin. Parent
owns rerunning infrastructure failures; no agent cancels or merges here.
