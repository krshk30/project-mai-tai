# October 5 Exact Candidate Composition

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

Fresh exact-head reviewer mutations/pins, final CI and an operator exact-SHA
GO remain necessary. No production service, env, ledger, database or watch
change was made. Restoration and pause lanes remain outside this candidate.
