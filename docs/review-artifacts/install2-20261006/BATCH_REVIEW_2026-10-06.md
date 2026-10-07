# Install2 Batch Review Receipt

As-of2026-10-06 20:46ET, codex-2. Application frozen at
aed1a358c0da4043ce248550c2b1d02cecdc76ff, tree
b28df7b3d492d0be4e72e1233ce0fff55e43fca4, draftPR1108.
Basef80a9c4af5e79beaa214e99f448bfe408c596df1 after reviewed #1106 merge.
#1102 already merged at52659779bcf4b6e28a2896e6eef3002488a6d2c9.
This is review evidence, NOT independent pin, merge or install acceptance.

## Preserved Patch Stages

| Governing PR | Original reviewed head | Integrated stage tip | Source/ops normalized changed lines |
| --- | --- | --- | --- |
| #1104 | 030df3d2639074b60fefbed552b43cfca36cc667 | 2a31d2cd21206722d7979d9480c5d6f8f149e800 | EQUAL |
| #1099 | f4e2d4ed100d361c1dfcd48ecc1231f42d2c932a | 35d0587f3b38c1f8cd1417622073f1dcbf156c7b | EQUAL |
| #1105 | 6c1846a45a4037050143906a83be3e06f84644c2 | 8902eb5ed7ace39e4d32c97920759599431d08a6 | EQUAL |
| #1101 | e1db2d14b69a2523be12f9baca265bba5022e137 | ecee77903c95018330154441755fa8b958dc556e | EQUAL |

Own per-stage audit: git diff --no-ext-diff --unified=0, retain file headers
and exact payload, remove only index and hunk-header lines, src/ops scope.
This establishes changed-line equality, NOT whole-tree/behavior equivalence.
Original27 commits/messages preserved, plus one explicit test-only integration
commit; all28 current commits recognized as single codex-2 marker.

Conflicts kept both MIRRORHOLD dispatch/reservation metadata and T43 durable
abort/release in the OMS; both KEEP/CLEAR catalog entries; upstream retained
mirror and list-primary expected=true assertions. Tests-only integration
refreshes five exact combined catalog counts and retains both real proven
rpg_old_buy_still_owned callback refusal and invented-code unproven/no-submit
negative control. No new trading algorithm, ownership waiver or source patch.

## Actual Tests

- Focused final application run:527PASS58.88s,
  /tmp/install2-batch-focused.xml. Includes all four lanes, retained mirror,
  installed ALL-ON controls, flags catalog and tonight flags.
- Same-environment full-unit baselinef80a:56FAIL/6581PASS,0errors,0skips,
  293.44s; /tmp/install2-batch-base-unit.xml and .log.
- Frozen applicationaed1a358:56FAIL/6931PASS,0errors,
  0skips,330.15s; /tmp/install2-batch-head-unit.xml and .log.
- Own XML failed-name comparison:exact equal56 names,only_base=[],only_head=[].
  Baseline failures preserved, not a green full-suite claim.
- HostedPRValidate37552847225 SUCCESS00:44:53Z; pushValidate37552843800 still
  running at this receipt. Independent batch pin absent on last read.

## Exact All-On Caveat

Forced Install2 overlay on old test_all_on_pm:33PASS/8FAIL3.24s;
/tmp/install2-exact-combination.xml. Six helpers require NFQ retry token/evaluator
with retained mirror now the owner; two assert buy-flip take-down now replaced
by KEEPREST. This result is NOT ALL-ON PASS. Old flag-OFF policy controls remain
unchanged. External exact-set retained-owner/frozen-wait controls are being
executed; no deployment source proof will claim complete until that evidence
is measured. Source unchanged during the full-pair run.

No source activation, service restart, migration, broker write, ledger change,
production approval/staging or daily timer installation accompanies this file.
