# Restoration Review Delivery

As of 2026-10-06 10:17 ET. This supersedes the readiness disposition in
R6_BUILD_2026-10-06.md, without rewriting that earlier checkpoint. No production
action, merge or activation has occurred. The runtime source is unchanged from
reviewed 4b803f08; the setting's code default remains FALSE. The install catalog
now expects TRUE under: "operator 10-06: fix item 7; installed when the acceptance
runner is green". The catalog denominator is 141 boolean + 8 numeric = 149.

## Corrected Reviewer Acceptance

Reviewer commit ed871725e06db3218790779eb99d94ce4a27f236 is cherry-picked
unchanged, retaining claude-1 ownership. Runner SHA256:
5ca1547bba7f74919b2f4d2d419d7bebf28cbf01f4d6284007d27d25294d138b.
Correctness compares against the SAME supplied bars; comparison with the
full stored chart is a separate measurement, not a correctness waiver.

Own runs use the original CSV/events, SHA256
680042a1c4472c4914d36859701b68ebf6fbcf7f21896ad67f24cecee271758d /
888d6ccab0b81b289b1c714f1fef1193d23f6b709951a016b2dabd04ab2b648c.
Both the unmodified factory settings and the eight-switch configuration pass:

| Measurement | Normal factory | Eight switches ON |
|---|---:|---:|
| Cases | 123 | 123 |
| Line MISMATCH / ERROR | 0 / 0 | 0 / 0 |
| Buys / entry_allowed while incomplete | 0 / 0 | 0 / 0 |
| First reading MATCH / safely HELD | 90 / 33 | 90 / 33 |
| +10 MATCH / HELD / n/a | 98 / 0 / 25 | 98 / 0 / 25 |
| +10 vs full stored chart: same / differs | 83 / 15 | 83 / 15 |

The 15 chart differences are late bars not yet supplied at that reading.
The 25 n/a cases have fewer than 11 available bars; they are not invented
successful resumes. Inflation disposition is accepted by the reviewer:
SHORT-state maximum 0.1859%; above-buffer LONG/LONG values are counterfactual.
Pause classification remains a separate lane, not claimed implemented here.

Eight-switch population command uses the unchanged runner and real factory,
with only its settings supplied at the factory boundary:

```sh
PYTHONPATH=src:. python -c 'import runpy; import tests.line_restore_acceptance_factory as f; from tests.unit.test_all_on_pm import ALL_ON; original=f.make_line_restore_case; f.make_line_restore_case=lambda **kw: original(**kw, settings_overrides=ALL_ON); runpy.run_path("scripts/line_restore_acceptance.py", run_name="__main__")' --bars "$BARS" --events "$EVENTS" --json /tmp/codex-line-eight-switch-acceptance-20261006.json
```

No readiness, mathematics, provider attestations or emitters are patched in
that run beyond the runner/factory's existing offline boundaries.

## Composition Boundaries

The legacy price/ATR-state fixtures explicitly supply a completed-worker
output via CompletedLineControl; they are NOT full-session history proofs.
All eight settings remain ON. The strategy gate and real service draft-version
check are exercised. VEEA's recorded current-bar signal now enters through
the worker publication boundary: with restoration ON the ordinary callback
only advances bookkeeping. No historical flip is emitted late.

The gap-held handoff now explicitly asserts the existing restoration semantics:
proven-clear ticket stays clear, authorization WAIT/session_line_unproven,
zero replacement. The old seven-switch expectation of immediate EXPIRED is
not asserted under eight switches; no runtime code changed to get this result.

Separate real-factory controls use retained RETO candles, an explicit controlled
provider attestation, real reconstruction and both real service emission gates:
no proof => zero buys; complete proof => permitted controlled drafts; protective
sell unchanged in either state. This does not infer completeness from candle
count. The population run above is the historical acceptance.

| Test group | Result |
|---|---:|
| Existing composed scenarios plus five new admission/version controls | 41 PASS |
| Real factory (including two eight-switch provider/exit controls) | 12 PASS |
| Composition + real factory + current catalog tests | 69 PASS |
| Restoration, integration, R6, composition, catalog, GAPHOLD, boot controls | 258 PASS |

New tests:
- test_all_eight_on_incomplete_line_blocks_both_cross_paths_without_consuming_state: first/reclaim x stream/REST.
- test_all_eight_on_service_refuses_draft_after_completed_line_is_revoked: stale version and revoked coverage.
- test_all_eight_on_real_factory_requires_provider_proof_preserves_protective_sell: complete/missing proof, both legs.

New in-process assertion mutations (no on-disk source edits):
- Strategy line_buy_ready always TRUE: four cross tests RED.
- Service draft version comparison removed: stale-version test RED.

The prior six R6 source mutations remain recorded and RED in the rebased receipt.
Ruff and whitespace checks PASS. Full-unit pair: main a7fed34d 6,314 PASS /
47 FAIL versus delivery 6,416 PASS / 47 FAIL; failed names added 0 / removed 0.
FINAL_UNIT_RECEIPT_2026-10-06.json includes every failed name and both log hashes.
This is baseline parity, NOT an all-green local suite. Exact-head CI remains
required before ready. Prior 4b's PR Validate crashed exit 139 in the SQLite
ORM worker path; its push Validate passed. No assertion or infrastructure
explanation is invented for that crash; the final head must pass both jobs.

## Raw Receipts

| Path | SHA256 |
|---|---|
| /tmp/codex-line-corrected-acceptance-20261006.log | dbbf00ca1f494592dba672b612bca54da68209b499d25d7397ae3e63fd5dacc7 |
| /tmp/codex-line-corrected-acceptance-20261006.json | b3104ee6525171e5ae427f4a8c806b2925e8586166244b68d2aa0851122a7f81 |
| /tmp/codex-line-eight-switch-acceptance-20261006.log | 5d3ddc199743a64d03075cd39cf8b40073215706a20b73b77b1f58c15e9d46b2 |
| /tmp/codex-line-eight-switch-acceptance-20261006.json | 67ff35a6b906fd9440409185a15f3eb965f8b471e49b9b3aac280fe50bd08e5e |
| /tmp/codex-line-final-focused-20261006.log | 2669357187147ca4ca8b7bb92f29173b5410cee3388d23f4a556d044ea73e293 |
| /tmp/codex-line-eight-switch-final-20261006.log | 9772a186d808bf3c332e66ce17c4cef94126cf4645dc853b78cca2f3790fe63f |
| /tmp/codex-line-ready-mutation-20261006.log | 3c3f3db357c450b24bd86c38a0ea51dd02bdb0379c0265502022ada2d3af51c8 |
| /tmp/codex-line-version-mutation-20261006.log | c9aa35225de8993682019e3e5cbfdf142c918f56ac3d053ebd489998efd6e15e |

Both Validate jobs must be green at the exact delivery head before ready.
Pin, merge and the joint after-close install are subsequent reviewed actions.
No ORB-purple build, production change, separate restart or activation here.
