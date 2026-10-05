# ALL-ON follow-up: isolated proofs, not install approval

Parent `462c8aa1ffc2f5515f50ba14964fc38e839ddc89`; main baseline
`4987353be54c4be15d4196907dec4dd0d6103236`. One follow-up, no rebase.
The operator's new ALL-ON ruling supersedes the earlier print-only/OFF rollout.
PMPRINT, PMFLIP, PMREST, RPG handoff, NFQ and GAP hold are all enabled in these
tests. No production implementation or settings default changes are included.
Install, process-environment changes, broker requests, service operations,
production/ledger writes and merge remain outside this work.

## Requirement Limits

**Literal both-broker PLACED: UNMET.** The actual APUS 09:32 mirror authorization
has stop `5.2720`, limit `5.2983`, and recorded precheck market `4.78`.
The actual strategy -> OMS -> adapter path preserves the distance refusal and
attempt-three terminal outcome `refused`, reason
`webull_mirror_precheck_deferred`, with zero wire calls. Old-order clearance
remains proven, so finished entry ownership releases. The separate experiment
that changes only the test's expectation to `placed` is assertion-RED; it is
retained as an unmet requirement, not counted as a killed production mutant.
No favorable quote is substituted into this recorded-distance proof.

**L5/L7: UNMEASURED, exact text missing.** The visible/retained RPG conversation,
GitHub #1093 comments/reviews, and retrieved parent conversation contain only
references to a reviewer 14:52 block, not its L5/L7 reproduction steps or expected
behavior. Parent has requested that exact block from the human. Existing B5/R20
real-loop proofs below are not relabeled as guessed L5/L7 coverage.

**Complete live MI/SCKT tape: UNMEASURED.** Stored four-decimal authorizations are
available, but complete underlying ATR tape for those moments is not. Their
independent pre-placement stage replay uses a controlled line in the intersection
consistent with all three stored rounded fields. APUS/VEEA stage replays use
retained full-precision ATR probes. All positive adapter acceptances and future
quotes are simulated. These prove serialization, identity, ownership and dedup
under explicit inputs, not historical venue acceptance or a complete live day.

## Fourteen-Ticket Startup

Fixture `tests/fixtures/rpgstuck1_startup_later.json` retains the parent bounded
capture with linked fills as of 13:17:02.125832 ET: 14 tickets, 11 linked orders,
15 intents and the actual
SCKT Webull BUY Fill (280 shares at 1.06). Strict RETO rejection uses the retained
actual HTTP-200 body in `rpgstuck1_startup_broker.json`, not an invented status.
The census uses real bot/OMS startup and five feedback passes, no production I/O.
Full IDs, before/after phase, clear proof and account ownership are retained for
all four scenarios in `all-on-dispositions.json` and JUnit properties.

| Ticket prefix | Account / symbol | Recorded | Proven, in-window | Proven, after 20:00 |
| --- | --- | --- | --- | --- |
| fbfd692d | Schwab APUS | refused | refused | refused |
| bd6ac0b9 | Webull APUS | refused | refused | refused |
| a007716c | Schwab VEEA | refused | refused | refused |
| faa55c1f | Webull VEEA | held_unknown | placed | expired |
| 7c7c5afb | Schwab APUS | refused | refused | refused |
| ee3d0d07 | Webull APUS | held_unknown | placed | expired |
| ff6464ff | Schwab RETO | held_unknown | refused, strict rejected-zero | refused, strict rejected-zero |
| ba108172 | Webull RETO | held_unknown | placed | expired |
| c539a57f | Schwab MI | refused | refused | refused |
| 4be7cb2d | Webull MI | held_unknown | placed | expired |
| a9eac442 | Schwab SCKT | refused | refused | refused |
| 6fb89c93 | Webull SCKT | refused | refused | refused |
| 889889cd | Webull SCKT | refused | refused | refused |
| d86d5d38 | Webull SCKT | filled | filled, no rebuy | filled, no rebuy |

The four local mirror jobs recover through anchored pre-submit evidence, not
age, purge, or DB repair. The in-window case uses a controlled eligible future
quote and produces four distinct-slot Webull buys, once each. A clear active
transaction owns its leg until it finishes; only finished placement/expiry
releases that transaction. After 20:00 the same four expire via the configured
window and produce **zero opens**, no stored buys or next-day ownership.

Controlled removal of local proof plus unknown RETO readback leaves all five
unknown jobs held/owned in-window and after 20:00, with zero opens. A previous
cleared APUS Webull job does not override the same account's later held ticket.
No unknown job is age-cleared. Every census makes exactly one strict RETO GET,
zero cancels; the actual SCKT Fill remains byte-for-field unchanged and its
consumed slot prevents re-entry. Existing strict identity/zero/positive-fill,
accepted/rejected-generation veto, partial fill and terminal guards are retained.

## Exact Proof Scope

`tests/unit/test_rpgstuck1_all_on.py` adds 32 collected cases:

- `test_all_on_all14_startup_dispositions_one_buy_or_owned_no_saved_late_buy`: four recorded/unknown and in-window/after-20 scenarios above.
- `test_all_on_recorded_authorization_stage_actual_adapter_wire_once_with_controlled_eligible_quote`: twelve independent stages; nine actual authorizations plus three proven-local mirror legs, across APUS/VEEA/MI/SCKT. Actual Schwab POST body and Webull SDK request assert four-decimal authorization -> existing cent quantization, unchanged quantity/identity, 600/300 sizing, one wire after repeated feedback. All transport acceptances are simulated.
- `test_all_on_actual_apus0932_distance_stays_no_wire_not_literal_both_placed`: actual refusal and released finished ownership, as distinguished above.
- `test_all_on_first_reclaim_serial_cancel_proof_current_authorization_one_buy`: both brokers x first/reclaim, strict old read before replacement and no duplicate.
- `test_all_on_recorded_apus1025_1026_1027_actual_strategy_schwab_rounding_chain`: actual APUS full-precision sequence 5.32/5.35 -> unchanged 10:26 -> 5.24/5.27; mirror distance precheck is retained.
- `test_all_on_r20_recorded_runtime_edge_without_restart_one_probe_then_quiet`: eight real-pump cases, reads29 -> reads30 -> budget-held, exactly one eligibility-gated wake/probe before held notice. Rejected-zero clears; unknown remains held; already-claimed/accepted/no-rebuy do not probe; 180 quiet turns do not recur.
- `test_all_on_b5_real_loop180_quiet_turns_never_scans_unknown_without_wake`: two quiet startup-loop cases with/without unnotified evidence. No arbitrary unknown scan or periodic DB work.

Mencius proof origin `430686a32c581160ed211c895e6c390bac315e64` was integrated
with cherry-pick --no-commit into this one follow-up. Both files were moved to
`tests/unit`, so full-unit/CI collects all 36 PM cases; no PM source was imported.
Public stream and five-second REST routes preserve SAIQ/CLRO no-cross outcomes,
recorded VEEA ask5.27 -> 114/57 shares, software PMREST ownership without handoff,
RTH first/reclaim on both brokers, thin streaks, flip/takedown, NFQ and holds.
Signals/transport where noted in the fixture are controlled, not an ATR replay.

The two old-catalog characterization assertions were intentionally adapted to
the new operator ruling: the live catalog now has no ALL-ON mismatch; the
negative test explicitly mutates only a **controlled catalog** back to three
OFF expectations. It still fails closed against ON process values, now six
mismatches because both consumers are checked. No skip or dropped guard.

The historical print-only TONIGHT tests remain as OFF regression tests; their
flip test now explicitly covers both historical OFF and current ALL-ON latches.
Catalog assertions no longer certify those historical OFF rollout values.

## Catalog And Verification

| Frozen suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Own exact main498 full | 5577 | 56 | 0 |
| Final combined ALL-ON candidate full | 5864 | identical 56 | 0 |
| Broad RPG/PM/OWN focused | 1550 | 0 | 0 |
| ALL-ON RPG/PM/catalog targeted | 140 | 0 | 0 |

Exact full failed-name diff: **added[] / removed[]**. Neither full suite is
globally green. Main took 202.01s; candidate 250.45s. Raw full output retains
311/314 warnings respectively and a candidate pending-task teardown diagnostic
from `_attach_webull_protection`; no failure/warning suppression or production
fix was used. New targeted/focused runs retain four JUnit-property warnings.

Only four catalog entries change: PMprint stays true; PMflip/rest/handoff change
false -> true, and all four explicitly also check OMS. Total denominator is
**147 = 139 Boolean + 8 numeric**. All four Settings defaults remain false.
Twenty-four false/missing/invalid process cases exercise both consumers; missing
or false fails, invalid is UNKNOWN, and eight explicit true values pass.
These are controlled process readers, not fresh live-process certification.
Parent owns the final hash-bound installation catalog and GO.

Final counts, exact failed-name lists/difference, all focused/targeted test IDs,
source/settings/catalog/test/fixture SHA256 values and mutation raw hashes are
retained in `verification-all-on.json`. Production source equals parent462;
the existing merged OWN implementation is already part of main498, not another
branch edit. Raw mutation outputs are preserved verbatim, including whitespace;
whitespace checks are scoped to code/tests/catalog, not claimed for raw traces.

Fresh controls: prior64 + fixture/exactR13/exactR16/R20/R15 seven + new ALL-ON
runtime/catalog three + PM six = **80 assertion-red control executions**.
Literal reviewer R15 repeats B4 and ALL-ON repeats R20; these are not claimed as
80 distinct falsifiers. The separate literal both-PLACED test is UNMET, excluded.
The earlier test-import-alias control prototype was NOT_PROVEN and superseded by
the retained final assertion-red run; no anchor/import error is a killed mutant.

Commands use `PYTHONPATH=src` and the repository's existing venv interpreter:

```text
python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-all-on-main-full.xml   # own detached main498
python -m pytest -q tests/unit --junitxml=/tmp/rpgstuck-all-on-head-full.xml   # frozen candidate
python docs/review-artifacts/rpgstuck1/run_focused.py <extra unit modules recorded in verification-all-on.json>
python -m pytest -q tests/unit/test_rpgstuck1_all_on.py tests/unit/test_all_on_pm.py tests/unit/test_pmprint1_tonight_flags.py tests/unit/test_expected_flags_check.py
python tests/unit/mutate_all_on_pm.py
python docs/review-artifacts/rpgstuck1/check_all_on_proofs.py
python docs/review-artifacts/rpgstuck1/run_b_mutations.py
python docs/review-artifacts/rpgstuck1/check_fixture_mutations.py
python docs/review-artifacts/rpgstuck1/check_r20_mutations.py
python docs/review-artifacts/rpgstuck1/collect_all_on_results.py
```

The earlier head full run was stopped when the PM proof arrived and replaced by
the final combined collection; it is not counted as a completed suite. An early
broad-focus command named nonexistent `test_ownmix1.py` and exited at collection;
the final command uses the three actual OWN unit modules and supersedes it.
Four JUnit-property compatibility warnings are retained; dispositions are
present in the resulting XML. No warning/failure is hidden to declare ready.
