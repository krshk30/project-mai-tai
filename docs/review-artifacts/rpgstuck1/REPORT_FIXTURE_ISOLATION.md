# RPG Runtime Fixture Isolation

Parent/source head: `271314defe4079517869d0dec9e1366e36dfa7c4`.
Fixture-only follow-up: no production code, protective policy, sleeping,
suppression, production/ledger writes, other-worktree edits or merge.

## Actual Failure Root Proof

The corrected parent combined full result was5701 passed/57 failed versus
main5500/56. ONLY added failure was
`test_rpgstuck1.py::test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit[fills]`.
Unattended-installer already failed in main; the earlier attribution was wrong.

Original RPG runtime imported an in-memory SQLite
`StaticPool(check_same_thread=False)` factory. Every overlapping Session shares
one physical connection. OWN's background protection binding reader and RPG's
threaded evidence reader overlap the serial journal writer. Notifier hashes only
held_unknown exact-old tickets, not submit_unknown. This is a lost transaction,
not a legitimate notifier revision/CAS conflict.

Actual failing R-T5 `fixture-static50.txt` lines243-261:

```text
251 thread8509144960 connection4515023264 UPDATE dashboard_snapshots
    payload revision10, phasefilled
252 thread6135394304 connection4515023264 rollback
253 thread8509144960 connection4515023264 commit
254 thread8509144960 connection4515023264 SELECT dashboard_snapshots
```

Preceding statements identify thread6135394304 as the OWN managed-position/entry
binding reader. Its close rolled back the SAME physical transaction between
final UPDATE and serial commit, leaving old submit_unknown. Fill accounting had
already committed. No competing legitimate journal UPDATE occurs in that
interval. Parent independently confirmed this exact trace.

Original full R-T5 path: **44 passed/6 assertion failures out of50**.
Changing only the factory in memory to temporary file SQLite with NullPool gives
**50/50 passed** against the same read-only composed production source. A separate
two-thread probe confirms reader close loses writer update with StaticPool, but
not separate connections. The actual R-T5 trace establishes causality, not only
the probe. Pending-task errors in original failing processes remain in raw output.

Static50 raw SHA256:
`f75ab1da5fe5ad3aedb7a9df7099e87e9532d616db0a81d0d0975a1e3718f149`.
File50 raw SHA256:
`fe49f95b1d8044eed8fa7901aea51133d7ed1f888173496ee1643b279524f7f6`.
Imported composed production path:
`/Users/velkris/.codex/worktrees/oct5-reviewed-followup-composition/project-mai-tai/src`.
No files there were edited.

## Fix And Final Assertions

Only RPG runtime's own `_session_factory` now uses a temporary SQLite file and
NullPool, with directory lifetime retained by its factory. Separate Sessions get
independent physical transactions. Unrelated helper/production factories unchanged.

`test_runtime_worker_reader_close_cannot_rollback_serial_writer` proves distinct
connections, committed-reader visibility, and preserved writer commit.
`test_r_t5_uncertain_webull_dispatch_to_fill_50_repeats[0..49]` repeats the complete
unknown dispatch -> exact-client read -> actual accounting -> durable completion.
Original race-sensitive assertions execute BEFORE awaiting actual protection
tasks. Then three replays assert durable filled/replacement_filled, exactly one
exact-client Fill (1 share at3.05), one managed position quantity1, consumed slot,
and no extra read, BUY, draft, Fill or rebuy. No tasks/protection are suppressed;
cleanup awaits completion rather than sleeping. Original four outcome guards stay.

Final new composed cases: **51 passed in10.41 seconds**. Final broad focused:
**1369 passed in76.30 seconds**. Fifty repeats are a regression bound, not proof
of all possible scheduling. Initial runtime modules each passed148 before the
durable assertions were strengthened; those are not final-source substitutes.

Fresh prior64 mutation controls all assertion-red; shared-connection fixture
control assertion-red; fresh R13 one case and R16 all six B2 cases assertion-red.
Current R16 actual call is `self._rpg_persisted_local_open(session, event, candidates)`;
the control changes candidates to `[]`. Initial reviewer-shorthand anchor attempt
correctly failed and was NOT counted; corrected anchor was rerun successfully.

Source/test checkpoint `29a17b1fafe354ca230d677578a00f3fe25f14cc` was published with
its full pair pending. This subsequent evidence-only update records completion;
no test or production content changed while the full suites ran.

| Fresh Final Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Own exact main7e10 full | 5500 | 56 | 0 |
| Frozen fixture source full | 5683 | 56 | 0 |
| Broad focused | 1369 | 0 | 0 |
| Strengthened composed new cases | 51 | 0 | 0 |

Exact full failed-name difference **added0 / removed0**, identical56 IDs retained
in `verification-fixture-isolation.json`. Main232.58 seconds; fixture279.91
seconds. Full suites are NOT called green. All67 fresh controls are assertion-red:
64 prior, fixture shared-connection, exact R13 and exact R16. Each fresh raw
mutation output is retained as `fixture-*-mutations.txt`, with SHA256/provenance
and exact counts in the JSON. No anchor/import error counts.

Final `test_rpg1_runtime.py` SHA256:
`9f78957dfda6923749086d3499e94147b26011755a023de3447c3f59c2be191e`.
Final `test_rpgstuck1.py` SHA256:
`5bc19c995868a2dc6e004f363a097e3b91036c51c47c6aceade2d2d72894c56e`.
All five production hashes equal271314de and are retained in the JSON. Focused
1369 and composed51 exact test IDs and full XML hashes are also retained.

The initial fixture full was terminated/superseded after strengthening durable
assertions; no earlier result substitutes for this final pair. Historical
B-review counts remain historical. Parent owns independent complete composition
full validation; these standalone/limited composed results alone do not clear
that integration. Captured ticket/fill and earlier broker-body as-of limits remain.
