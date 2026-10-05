# October 5 Exact Candidate Composition

## Current R20/F3 Follow-Up, 14:26 ET

Local tests plus hosted Validate, not independent pins or an install GO.
Each PR is one normal-pushed follow-up atop its previous reviewed head; no rebase.

| Source | Exact SHA |
| --- | --- |
| Merged PM main | 7e10baf0319da796b84934fe38994f6db4fcfc0b |
| RPG R20 #1093 | 7fe27daaad522bb3aa7f921212a3179532117f7c |
| OWN F3/H4 #1094 | 76c3b4f764c56baafed3d527a0a47dca8cbcb5b4 |
| Combined tree | 17fcd4d927827825a70a3ae1b4f772911c7ddbe6 |

The parent's staged tree equals `git merge-tree --write-tree` of both exact
heads, with no conflict. Main remains7e10; no merge commit or production action.

| Run | Pass | Fail | Skip | Full failed-name diff vs fresh main |
| --- | ---: | ---: | ---: | --- |
| Fresh clean main | 5500 | 56 | 0 | baseline |
| RPG standalone | 5692 | 56 | 0 | added[] / removed[] |
| OWN standalone | 5577 | 56 | 0 | added[] / removed[] |
| Exact combined full | 5769 | 56 | 0 | added[] / removed[] |
| Exact combined focused | 327 | 0 | 0 | selected tests, not a full-suite comparison |
| Exact targeted R20/F3/H4 | 51 | 0 | 0 | selected tests, not a full-suite comparison |

Parent independently parses the complete XML failed/error IDs. No existing
failure is waived; full suite is not globally green. Exact combined full257.85s,
focused40.64s, targeted7.16s. Focused selection includes tonight's flags,
PMPRINT/PMFLIP, PMREST, all14 startup tickets, unknown quiet-turn proofs,
RPG/NFQ composition and owned exit/stand-down paths. Full units also include
the50 uncertain-dispatch fill/no-rebuy repetitions.

| Independent literal control | Assertion failures |
| --- | ---: |
| Disable R20 runtime edge wake | 2 |
| Remove F3 bounded fetch deferral | 4 |
| Remove F3 same-episode pending guard | 2 |
| Remove H4 account filter | 1 |
| R15 unproven hold returns expired | 2 |

Correction: the earlier provisional pending-writer test indexed a missing key
under the mutation, yielding KeyError rather than an explicit assertion.
Final head uses `.get`; the exact rerun gives two AssertionErrors/no KeyError.
The provisional5769/same56 run is not substituted for the final exact-head run.
Initial failed mutation anchors/class names were not counted; corrected runs
above are assertion failures. Mutations are memory-only, no source-file edit.

| Raw XML/control | SHA256 |
| --- | --- |
| /tmp/rpgstuck-r20-main-full.xml | 4f20bfb15785705973e7a0d72c46a5d7c912f0db4e584ce53787cd5258e2f43b |
| /tmp/oct5-r20-f3-exact-heads-full.xml | 3ea821a940fbaa4ce16a400bedc8bb45f48cba87e31e54731acd38e996ec50a9 |
| /tmp/oct5-r20-f3-exact-heads-focused.xml | 1712a0bba9f191fe883bf1a33ae685f6b9468104236499f43fee227b62d8f865 |
| /tmp/oct5-r20-f3-exact-heads-targeted.xml | c32f561af2fd8fd9b06cf032c4b76c8c790d60127b86cbe65d129694552089aa |
| /tmp/oct5-parent-r20-mutant.txt | e3e10dcbb6e838de02442a505ecfd7e29db3376aa22175c833801e5fd2cd4f86 |
| /tmp/oct5-parent-f3_retry-mutant.txt | a5044b1a3a91d1d31f35d0d42faa04422149fd41f54986507f303de2d673b6e5 |
| /tmp/oct5-parent-f3_writer-exact-head-mutant.txt | a59b73b033106d48dcf7a2777e173981c4b419d846ac543efc7a4494422b0835 |
| /tmp/oct5-parent-h4-mutant.txt | 46976e4ebbedeefb3fbfa88bfb5682f4a2e03f74d0b8bdba846a0014e91fb8e9 |
| /tmp/oct5-parent-r15-mutant.txt | 7158b3b07670ec8d4c12758794683b5ca751b9a35c3f12e3021b8e94d69e5f8a |

Both current-head Validate pairs PASS. RPG runs37354917991/37354925360 finish
14:25:42/14:25:45 ET; OWN37355112849/37355121700 finish14:24:15/14:24:23 ET.
Both independent-review-pin checks lack fresh records and are not PASS.
RPG REPORT_R20.md audits every held transition and the startup-only limit for
ineligible exhaustion; OWN report maps real-exit tests on both accounts and H4.
The conditional plan requires fresh ticket census before execution and exact
operator GO; no install has been scheduled or performed by this work.

## Historical 13:55 Composition

As of13:55 ET. Local verification only, not a review pin, merge or install GO.

| Source | Exact SHA |
| --- | --- |
| Merged PM main | 7e10baf0319da796b84934fe38994f6db4fcfc0b |
| RPGSTUCK final | c6548875bab15fb52bf35ca491e2e0ea63cc7148 |
| OWNMIX final | 55563e6f3abe004d484195c202b5118ea75e0169 |
| Exact combined tree | 5e16cf4d13ca00e82dab91a4f4f8896fb19eb7cb |

Generated changes were applied in order RPG then OWN in an isolated checkout.
The RPG fixture-only continuation and final docs were then added; production
source is unchanged from271314de. Finalc6548875 source/tests equal29a17b1f.
`git merge-tree --write-tree c6548875... 55563e6f...` returns exactly the staged
tree above, without conflict. Main remains7e10; no merge commit was created.

## Final Counts

| Run | Passed | Failed | Skipped | Exact failed-ID difference vs main |
| --- | ---: | ---: | ---: | --- |
| Parent clean main7e10 full unit | 5500 | 56 | 0 | baseline |
| RPG final standalone full unit | 5683 | 56 | 0 | added0 /removed0 |
| OWN final standalone full unit | 5570 | 56 | 0 | added0 /removed0 |
| Parent exact combined full unit | 5753 | 56 | 0 | added0 /removed0 |
| Parent final focused composition | 411 | 0 | 0 | none |
| Parent fill repetitions + connection isolation | 51 | 0 | 0 | none |

Parent parses complete JUnit XML failed/error node IDs, not counts alone.
All56 failure names are identical. The full suite is NOT globally green.
Combined full240.73s; focused51.92s; repeat/isolation10.67s.

## Additional Failure And Root Cause

The first combined full run was5701 passed/57 failed. Added only:
`test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit[fills]`.
It remained submit_unknown after actual fill accounting. Initial inference
from the log tail incorrectly named an unrelated Linux-tool test; exact XML
comparison corrected that error, which remains explicitly recorded.

The failed SQL trace shows the filled journal UPDATE, then a different reader
thread rolling back the SAME StaticPool DBAPI connection, then writer commit.
That erases the journal update. Same production source with the original
fixture fails6/50 fresh processes; independent file-backed connections pass50/50.
The fixture-only follow-up retains production notifications/protection and
adds50 complete no-rebuy repetitions plus a connection-isolation regression.
Original phase assertions run before protection cleanup. No sleeps, suppressed
tasks, lowered guards or production workaround. Final full parity clears this
additional test failure; it does not substitute for a fresh independent pin.

RPG report retains67 assertion-red controls; OWN report retains15. Exact R13
removal gives1 assertion failure; R16 candidates removal gives6. A failed first
R16 mutation anchor was NOT counted; the corrected actual call was rerun.

## Commands And Raw Hashes

All runs use the shared `.venv/bin/python` and `PYTHONPATH=src`, with imports
verified from the isolated composition checkout, not the primary checkout.

```sh
python -m pytest -q tests/unit --junitxml=/tmp/oct5-composed-isolated-connection-full.xml
python -m pytest -q tests/unit/test_rpgstuck1.py -k '50_repeats or worker_reader_close'
git merge-tree --write-tree c6548875bab15fb52bf35ca491e2e0ea63cc7148 55563e6f3abe004d484195c202b5118ea75e0169
```

The411-case focused command includes PMPRINT/PMFLIP, tonight's exact flags,
PMREST, native OCO, allthree OWN modules, allfive RPGSTUCK modules, RPG/NFQ
unit composition and `tests/composition/test_rpg1_nfq1.py`. Zero skips.

| Raw JUnit XML | SHA256 |
| --- | --- |
| /tmp/oct5-main-7e10-parent-full.xml | 2978b959ae8056eb35023a05ed45ea6f0ab28285b6f3b7c7e94361c09f011af7 |
| /tmp/oct5-composed-isolated-connection-full.xml | ca68a97c5b64660ecfe46120b582b20886e9abd49626d86c30833e7193501392 |
| /tmp/oct5-composed-isolated-connection-focused.xml | ca62b8ee168c835c47f608403b85ed7451f7504eb6693f9666ed6d485e08fedc |
| /tmp/oct5-parent-composed-fill50.xml | a54b7b9b4fb0e5b305a9a99fab17c73f58f9c53c79b1623cdc861d8a254fd526 |

Final exact-head RPG Validate x2 PASS: runs37351785657 at13:59:31 ET and
37351790055 at14:01:01 ET. OWN Validate x2 PASS at13:27:34/13:27:56 ET.
Both independent-review-pin checks still await new records for the changed
heads. No fresh pin is inferred from the old reviewed segments.

Fresh exact-head reviewer mutations/pins and an operator exact-SHA
GO remain necessary. No production service, env, ledger, database or watch
change was made. Restoration and pause lanes remain outside this candidate.
