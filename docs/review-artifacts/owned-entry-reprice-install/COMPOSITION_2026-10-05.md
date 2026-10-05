# PM / OWN / RPG - own three-head rehearsal

No main merge, review pin, production write, migration or restart performed.
This isolated composition is not an install authorization.

| Candidate | Exact head |
|---|---|
| PMPRINT1 / PMFLIP1 #1092 | 71f5f6bc5ae63a2a02b4a6fe5ed469b71cfbe1c8 |
| OWNMIX1 #1094 | b94bd0ff0c2c7da681da45d748db61db82250b7c |
| RPGSTUCK1 #1093 | 73e9e4c8f92f672ecc03e7c460b134123015840a |
| Shared base main | a80b51816abf0aefc269f0fdfc473468fd3fe62c |

Managed isolated worktree:
`/Users/velkris/.codex/worktrees/pm-owned-reprice-composition/project-mai-tai`.
Started at PM head; applied the OWN base-to-head diff then RPG base-to-head diff
with git apply --3way --index. All files applied cleanly, including overlapping
OMS service and v2 strategy files. No manual conflict resolution. OWN report's
attribution-only correction applied before final tree capture; source/tests
were unchanged by that correction. Source stayed frozen throughout the run.
Final staged composed tree: e5b76f9c44532409ba4019ffdd0bab6fc0825f72.

Command from that worktree:

```bash
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  -m pytest tests/unit -q --junitxml=/tmp/pm-own-rpg-composition.xml
```

| Population | Passed | Failed |
|---|---:|---:|
| Exact main baseline, independent prior run | 5412 | 56 |
| PM + OWN + RPG composed source | 5549 | 56 |

188.88 seconds, 311 warnings. Parsed FAILED node IDs compared with the exact
baseline list in OWN verification.json: added=[], removed=[], same=true.
This runs the OWN, RPG, PM, NFQ/PMREST, confirmation, native-child, managed-exit
and partial-fill regressions together; it is NOT a globally green unit suite.

| Raw output | SHA256 |
|---|---|
| /tmp/pm-own-rpg-composition.txt | 42f0cc66cfa06a90d75e84be8c9a4c2155d6f40e0c34c863f4f6832d2115c95e |
| /tmp/pm-own-rpg-composition.xml | b08f606532f3728d239233e5da33d658e3fe9a02017ad8cf17569e935b9af8a6 |

Limits: real cancel/readback/re-place timing, native partial-parent coverage,
actual production schema rollout and new live cross cache timing UNEXERCISED.
Historical 41 OWN parent mappings remain UNMEASURED. GAPKEEP is not built.
RPG report's row label "Pinned PMPRINT composition" meant exact source head,
not independent review: #1092 was NOT pinned. Its PR body has been corrected;
the exact source/test head and 587-case result are unchanged.

Before final merge/install, re-run on any changed head or conflict resolution,
obtain new exact-head review pins where needed, and bind an executable runner
and operator GO to the final application tree. This draft schedules nothing.
