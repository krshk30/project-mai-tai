# OLOX In-Memory Gate and Follow-Up Safety Assessment

As of October 6, 2026, 13:25 ET. Read-only recorded-data and local-code study;
no production change, broker order, ledger edit or ready-head claim.

## Verdict

AGREE: the separate placed-generation predicate can suppress the primary
draft, even when `_rpg_entry_owned` is false. The independently replayed
recorded 10:13 placed / 10:15 refused chain reproduces that mechanism.
The actual 10:16 and 11:06 process cache was not captured: live-cache causation
remains UNMEASURED, not established by reconstruction.

DISAGREE with declaring the current stronger-marker follow-up safe: it creates
a legacy-ticket recovery gap. The source lane is stopped, uncommitted work
preserved, and #1099 remains draft. Focused green is not sufficient for pin.

## Counter-Control

Parent independently loads the actual baseline authorization, ownership and
queue methods from `aaac903e56dfd40610a7d429162ef562e73c689e` into a local
test process. The new helper is not used to claim historical baseline success.
Historical ticket identities, phases and the 10:16 bar are recorded inputs;
cache retention, surviving mirror, quote and clock continuation are controlled.

| Input or result at reconstructed 10:16:03 ET | Recorded-generation replay | Sole generation-equality counter-control |
|---|---|---|
| Prior primary token | `48c765e5-a03a-5fe7-b488-78355518da16` | Same |
| Prior phase | `placed` | Same |
| Successor token | `c9b13cc4-d0c7-5e87-84c1-5308f001158d` | Same |
| Successor phase | `refused` | Same |
| Shared resting active | true | true |
| Primary quantity before queue | 0 | 0 |
| Prior replacement generation | `48c765e5-a03a-5fe7-b488-78355518da16:0` | Same |
| State primary generation before queue | Equal to prior replacement | Empty, controlled counterfactual |
| `_rpg_entry_owned(primary)` | false | false |
| Separate placed-generation block | true | false |
| Primary drafts | 0: RED against required one-draft outcome | 1 |
| Mirror drafts | 0 | 0 |

The surviving mirror is not duplicated. This is not a claim that a live broker
would accept the controlled draft, or that the real 11:06 cache was identical.
The parent script's control JSON captures quantity after queue (408); the
agent's independent counter-control additionally captures before queue (0).

Parent raw `/tmp/codex-t43-parent-generation-countercontrol.log` SHA256:
`c7ca71a7ef9aca97cc1b7a7f4525daeaa15566098d678319198390c5f0210913`.
Script `/tmp/codex-t43-parent-generation-replay.py` SHA256:
`a9e9fa40392adc2d609760adfa9d3a38d69c6b22ed8afc394f39b9c7af6cdb3b`.

## Legacy Blocker

The recorded all-14 fixture `tests/fixtures/rpgstuck1_startup_later.json`
contains two SCKT rows already `refused/replacement_terminal_accounted`, without
the newly proposed `replacement_terminal_proven` marker:

| Ticket | Replacement client |
|---|---|
| `6fb89c93-a101-5200-b569-320acf5f4f85` | `schwab_1m_v2-SCKT-open-494aac58d0c2` |
| `889889cd-2de7-59ca-997f-fc546b728ec9` | `schwab_1m_v2-SCKT-open-69a4f7276ad3` |

The candidate makes these own BUY admission indefinitely. Direct code read:

- `HandoffJournal.reconcile_feedback` admits only placed/submitting/submit_unknown;
  an already-refused legacy row is not examined when new proof appears.
- `_rpg_retry_jobs` selects active phases, excluding these refused rows. Existing
  evidence wakeups target a different held-unknown case.
- `_rpg_open_refusal` does not mirror the new marker-based v2 ownership rule.

Changing the SCKT startup assertion to expect blocking does not solve recovery.
Its later recorded fill consumes its own slot; it does not prove these other
replacements terminal-zero. Nor may cancellation status or absent local fills
alone manufacture that proof.

Required disposition before proceeding: narrowly bounded proof-only recovery
of legacy replacements, from exact existing positive evidence or an exact-client
broker terminal-zero read, CAS-fenced by ticket/generation/account. OMS and v2
must agree on ownership. No timer, age-based clearing, journal purge, ledger
write, historical BUY replay or silent status-only waiver. Unknown evidence
continues blocking. This correction is not yet built or approved as a ready head.

## Receipts and Limits

Current uncommitted source delta (four changed application/ops files) SHA256,
from `git diff -- <four paths>`, is
`7262df5ee475a20e9a915207810ea67454c0dd0cbfd94509a0272aba3c27255b`.
Sole source/test writer Zeno is stopped; parent writes only this assessment and
shared C-row. No commit or push of the candidate source.

| Receipt | Outcome | Raw SHA256 |
|---|---|---|
| Focused bound-final | 1,077 PASS, 84.21 s; not legacy-recovery acceptance | `0d0d10843616d64c85012678417d8cb48adafeb1d363927b684b8fcc8eec8f66` |
| Bound-final mutations | 35/35 assertion RED | `d3e3aed0b4edff6c24b6dd60612fd4e89fe6a6efbdc878cb23173bdc3db8ea3e` |
| Full main c21d8274 | 47 FAIL / 6,447 PASS, 291.24 s | `c1482a208efa2b912930fa45ebab543511cb6dc7510e06997f9cb46e8c5abb9c` |

Raw paths are `/tmp/codex-t43-safe-chain-focus-bound-final-20261006.log`,
`/tmp/codex-t43-safe-chain-mutations-bound-final-20261006.json`, and
`/tmp/codex-t43-safe-chain-full-main-c21-20261006.log` respectively.
The aborted-reader mutation initially had an import-path error and then a
misbound reader; neither counted as RED. Final harness binds the actual reader
loaded by the test. Full candidate suite is incomplete/excluded: no failed-name
pair, final CI or readiness proof is claimed. Historical failed runs retained.
