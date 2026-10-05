# OWNMIX-first merge and pure RPG rebase

As-of14:53 ET2026-10-05. Repository action only: no box checkout, flags,
services, database migration or ledger changes. Install remains BLOCKED by
the per-gate audit in INSTALL_PLAN_2026-10-05.md. The raw14:42-14:45 ET
dry run remains PREFLIGHT_DRY_RUN_2026-10-05.md, not tonight's authorization.

## Exact OWN Merge

Before merge: #1094 exact head76c3b4f764c56baafed3d527a0a47dca8cbcb5b4,
base7e10baf0319da796b84934fe38994f6db4fcfc0b. Both unchanged; PR CLEAN/MERGEABLE,
latest independent-review-pin PASS37357567204, both Validate PASS. Exact
review-pins record read, and operator before-merge yes relayed directly.

```bash
gh pr merge 1094 --repo krshk30/project-mai-tai --rebase --match-head-commit 76c3b4f764c56baafed3d527a0a47dca8cbcb5b4
```

Merged14:51:18 ET, rc0. Main4987353be54c4be15d4196907dec4dd0d6103236.
Parent independently fetches main and compares WHOLE trees:

```text
origin/main                         4987353be54c4be15d4196907dec4dd0d6103236
origin/main^{tree}                  e6403bdafdc7b9670ecadbc6dbeccdcb6650c96c
76c3b4f7...^{tree}                  e6403bdafdc7b9670ecadbc6dbeccdcb6650c96c
```

OWN's additive schema has NOT been run. The future RPG target contains OWN;
the install GO must explicitly name the schema-bearing OMS stop/migrate/start
sequence. No optional no-migration shortcut to that target.

## Exact RPG Rebase

Pauli remains sole writer of codex/rpgstuck1-canonical-handoff. Clean old head
7fe27daaad522bb3aa7f921212a3179532117f7c rebased onto exact new main4987353b;
no conflicts, no hand edits, no extra content/test/report changes, no Update
branch. Push used explicit force-with-lease expecting the old7fe27daa head.
New head462c8aa1ffc2f5515f50ba14964fc38e839ddc89; all7 Codex markers preserved.
Parent independently fetched and ran:

```bash
git range-diff --no-color 7e10baf0319da796b84934fe38994f6db4fcfc0b..7fe27daaad522bb3aa7f921212a3179532117f7c 4987353be54c4be15d4196907dec4dd0d6103236..462c8aa1ffc2f5515f50ba14964fc38e839ddc89
```

Unedited stdout:

```text
1:  99b32d98 = 1:  13787c62 fix: release proven-clear RPG handoffs at canonical prices
2:  b0d14e26 = 2:  a68b182b test: replay recorded RPGSTUCK Schwab wire sequences
3:  d6865168 = 3:  740fb84e fix(rpgstuck1): prove complete recorded startup recovery
4:  271314de = 4:  1a7931ee fix(rpgstuck1): bound unknown recovery and complete merged review proofs
5:  29a17b1f = 5:  8e8e98a3 test(rpgstuck1): isolate worker sessions and pin uncertain fill replay
6:  c6548875 = 6:  488d46df docs(rpgstuck1): retain final fixture pair and mutation evidence
7:  7fe27daa = 7:  462c8aa1 fix(rpgstuck1): wake eligible old-order proof at runtime exhaustion
```

New head WHOLE tree17fcd4d927827825a70a3ae1b4f772911c7ddbe6 matches the prior
exact combined rehearsal, whose full result was5769PASS/same56FAIL vs then
main5500/same56, failed-IDadded[]removed[]. This reuses exact tree evidence,
NOT a claim of a fresh full run on the new commit. CI Validate37359176342 and
37359183078 running at14:53; fresh independent-review-pin missing, as expected.
Reviewer must rerun experiment/full suite/mutations and pin this exact head
on base4987353b before merge. Head stays frozen for that review.

The separately relayed standing operator yes permits rebase-merge of a
reviewed/pinned fix head that is not behind main after checks pass. It never
authorizes an unpinned head, installation, flag-on, migration or ledger write.
Future #1095 follows only after #1093 is reviewed/pinned/merged, rebased onto
that actual main, and independently reviewed/pinned itself.
