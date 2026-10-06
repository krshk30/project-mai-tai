# WEBULL429 build verification

[codex] 2026-10-06 ET. Sole branch writer; production and shared handoff untouched.

## Superseded Pinned Full Unit Pair

Base: `4805ddc81184c76b4d5cef5c483c809edb666fe6`, detached managed worktree
`/Users/velkris/.codex/worktrees/webull429-base-proof/project-mai-tai`.
Head source: `37b563352710e7e1113e6b601113ff9ce3dd8c02`, the human-authorized
rebase of the unreviewed hardened branch onto4805. Final verification commit
adds only docs/control artifacts; subsequent CI verifies its exact published SHA.
No runtime file was edited during either full-unit run.

Same command in each worktree, normal PATH, existing requested venv:

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider tests/unit
```

| Run | Passed | Failed | Duration |
| --- | ---: | ---: | --- |
| Current pinned main4805 | 6469 | 56 | 302.59s |
| Rebased hardened head | 6521 | 56 | 302.36s |

`MAC_BASE_FAILED.txt` and `MAC_HEAD_FAILED.txt` are identical, SHA256
`6799163e6462c341489ccc9c1f1d8c1bf24cf9d8f2952543a593dbb5e69ff416`.
Zero new failure names; 52 new passing controls. Normalize only an interleaved
absolute-path warning suffix or the specifically observed timestamp appended
to `test_whole_installer_succeeds_with_one_managed_schedule` before comparison.
The underlying raw logs retain those warnings; no failure is removed.

The actual Mac baseline is56 rather than the usual47:44 fanout installer shell
controls, one log-retention shell control, one unattended-upgrade shell control,
one dead-consumer throughput timeout, and nine sync-only controls. Shell controls
encounter missing `/usr/bin/sha256sum`; sync-only subprocesses encounter normal-PATH
system Python's unsupported union annotation. No PATH adjustment, unrelated
repair, skipped test, or production installer execution was used to turn them green.

Local raw logs remain in the build worktree (git-ignored):

| Log | SHA256 |
| --- | --- |
| `.webull429-current-main-unit.log` | `c34e127b9bc5a9258435f386a5ca8ef40ee3ea5c824e17abcb5c27d19d7e2a1d` |
| `.webull429-rebased-unit.log` | `6689ad52f4817f6dbc5dbf642067164d9729eaabd69c253f74673483e8af038e` |

## Focused and Mutation Controls

411 passed across `test_webull_list_primary.py`, `test_webull_adapter.py`,
`test_rpg1_buy_readback.py`, `test_expected_flags_check.py`, `test_all_on_pm.py`,
`test_schwab_never_synthesize_flat.py`, `test_webull_attach_protection.py`,
`test_webull_exit_pair_session.py`, `test_webull_protect_retry_horizon.py`, and
`test_pmprint1_tonight_flags.py`. New file contributes52 controlled cases.
Recorded IDs/prices/timestamps are distinguished from synthetic response variants.

```sh
env -u MAI_TAI_DATABASE_URL PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider tests/integration/test_strategy_oms_roundtrip.py tests/replay tests/backtest -q
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m ruff check src tests
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python ops/health/check_log_marker_collisions.py
```

SQLite/replay/backtest:71 passed,1 xfailed. Ruff:pass. Marker isolation:pass,
77 literal consumers/380 emitted markers/975 logging formats. PostgreSQL
integration acceptance is delegated to isolated CI PostgreSQL16, not a live DB.

`all_on_check.py` passes controlled composition with the eight PM/RPG/rounding/
restoration flags plus ORB-Schwab ATR entry gate (nine booleans), and the new
Webull read flag explicitly ON. Fake process environment audit across nine
services: `PASS; checked=152/152 mismatches=0 unknown=0`. This is NOT live env
evidence. `.webull429-all-on.log` SHA256:
`28de8dbb6d1e42ab3ea5e5ce8b805702f51f929b2396e73bcf007172276a1266`.

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/webull429-list-primary-plan/all_on_check.py
```

Historical fourteen process-local mutants returned pytest1/harness0; this
return code alone is NOT semantic-kill proof. `MUTATION_AUDIT.md` records each
raw-log hash and classification:13 direct assertion/DID NOT RAISE kills plus
one behavioral UNKNOWN exception. No extraction/class-cell error observed.
Names:
`detail_first_restored`, `nested_foreign_client_adopted`, `stale_page_served`,
`budget_not_counted`, `page20_claimed_complete`, `missing_child_claimed_readable`,
`first_filled_lost`, `durable_proof_ignored`, `forever_attempted`,
`strict_read_uses_list`, `quantity_alias_conflict_accepted`,
`price_alias_conflict_accepted`, `unknown_status_alias_accepted`,
`list_not_found_swallowed`. Reproduction for each name:

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/webull429-list-primary-plan/mutation_check.py MUTATION_NAME
```

## Confirmed Terminal-Partial Correction

Parent found a real adapter-to-store regression after cfe81117's green CI.
That head is superseded, NOT ready. Own real OmsRiskService/OmsStore tests
confirmed cancelled/rejected remainder statuses were falsely retained as
partially_filled. The corrected adapter preserves broker terminal status;
store accounting records only the unbooked cumulative execution delta, with
Webull venue, broker origin, and matching adapter marker required. Existing
managed quantity grows only for the exact same owned entry, without resetting
the ladder or creating a second BUY. No cancel implementation/timer changed.

Six real lifecycle variants (terminal statuses x prior cumulative0/1/2)
prove fill deduplication, held positions, terminal order/intent retirement,
repeat poll idempotency, and later SELL accounting without BUY resurrection.
13 new cases; new Webull file65 cases; focused437 passed. Replay/backtest71
passed/1 xfailed. ALL-ON controlled effective-zero audit152/152, zero mismatch/
UNKNOWN; marker and Ruff checks pass. No live environment or repair claim.
Six new mutations have direct assertion failures. All20 mutants are manually
classified in `MUTATION_AUDIT.md`:19 direct assertion/DID NOT RAISE kills and
one separately classified behavioral UNKNOWN exception. Raw logs preserved.

Local full-unit BASE/HEAD is re-run with the exact normal-PATH command above
against unchanged pinned main4805. The final receipt below is completed only
from finished runs; no source file is changed during those runs.

| Replacement run | Passed | Failed | Duration |
| --- | ---: | ---: | --- |
| Fresh pinned main4805 BASE | 6469 | 56 | 305.67s |
| Corrected terminal-partial HEAD | 6534 | 56 | 309.94s |

Both lists match the committed `MAC_BASE_FAILED.txt`/`MAC_HEAD_FAILED.txt`
exactly, SHA256 `6799163e6462c341489ccc9c1f1d8c1bf24cf9d8f2952543a593dbb5e69ff416`.
Zero new failure names;65 passing additions versus base,13 versus cfe81117.
BASE has455 warnings, HEAD454. The observed appended2026-10-06 timestamp
on HEAD's whole-installer-success node is normalized solely for name
comparison; raw stdout is unchanged. Normal PATH was NOT prepended with the
venv: Avicenna's47-failure venv-first baseline is not this environment pair.
The difference is its absence of the nine normal-PATH sync-only failures.

| Replacement raw log | Worktree | SHA256 |
| --- | --- | --- |
| `.webull429-terminal-partial-v2-base-unit.log` | detached BASE worktree above | `02bbd641595127fb1731874aff4af81b604dcac3612281fcb052641f01e4860b` |
| `.webull429-terminal-partial-v2-unit.log` | build worktree | `97e456aee0adf53ce5f6710ffa6945aa20629262b1a8fa7ae9040e63b248ed56` |
| `.webull429-terminal-partial-v2-all-focused.log` | build worktree | `8b8276eb6d33d43a709c6c4e4fe97f997aaa2a466cb715efe6cfa1f8f746a4cf` |
| `.webull429-terminal-partial-v2-replay.log` | build worktree | `a7973bc5b24ff3f42f19e9f0e1931179e12e08a6c200a6227bd73c02a7c48a7d` |
| `.webull429-terminal-partial-v2-all-on.log` | build worktree | `28de8dbb6d1e42ab3ea5e5ce8b805702f51f929b2396e73bcf007172276a1266` |

GBPG limitation: no LOCAL full PostgreSQL golden BASE/HEAD pair was run, and
no production/VPS full-unit baseline was measured. Local SQLite/replay/backtest
controls are not represented as PostgreSQL acceptance. Hosted pinned-main
Validate below measured the full golden gate on isolated PostgreSQL16; both
replacement-head Validate runs must measure that same gate before READY.
Do not substitute the usual48 Linux failures or superseded cfe81117 CI.

Runtime files stayed byte-identical throughout the completed candidate run:

| File | SHA256 |
| --- | --- |
| `src/project_mai_tai/broker_adapters/webull.py` | `712fe87cdad454f4fcbc2295c330fc7ffb4222ba8c8c481e0be490b1c9d9a9b8` |
| `src/project_mai_tai/oms/store.py` | `2e3fadea434a165edad366676c8bac1bb70648ac47d2188fd9a7c1a8f535ec07` |
| `src/project_mai_tai/oms/service.py` | `775cc58abc38b381d5242bfc6116003e22bd10e49ac54ba4a8fee4681d06dfc7` |
| `tests/unit/test_webull_list_primary.py` | `42b0840730e7ca64dce25c2445cc8304f8d4e4235f118e32e205424eb1b2ff70` |

The shared read-budget implementation and `INTEGRATION_CONTRACT.md` remain
byte-identical to cfe81117. No WBPOWER integration API changed with this repair.

## Ubuntu Evidence and Readiness Gate

Current-main4805 [Validate run37516195904](https://github.com/krshk30/project-mai-tai/actions/runs/37516195904)
is green:6525 unit passed,86 integration/replay/backtest passed,1 xfailed.
This is measured CI, not an assertion that the usual48 Linux failures apply here.
Local CI-log SHA256:
`4e2f5b8df12fc480e7d1203ce28b2efb24173f3162c013d97c6a4d51e51b8834`.

Initial source [push run37515566921](https://github.com/krshk30/project-mai-tai/actions/runs/37515566921)
measured6535 passed/1 failed. The sole failure was the PM catalog inventory's
hard-coded151 consumers after adding the new OMS-only flag; its assertion now
expects152 and explicitly checks the new flag ON. No PM rule was changed.
This failed run is retained, not represented as green evidence.

Main advanced to `4805ddc81184c76b4d5cef5c483c809edb666fe6` during earlier testing:
only ORB page display, control-plane tests, runtime-registry tests, and their
review artifacts changed. There is no overlap with this lane. Human parent
then explicitly authorized rebase of ONLY this unreviewed branch onto4805.
It completed without conflicts; current main is an ancestor and this full
candidate pair was re-run. Final PR Validate checks the merge composition.
No main merge, production write, ledger or shared-handoff edit was performed.

The earlier candidate26678099 is superseded even if its Validate runs are green:
final manual source review added alias-conflict and native UNKNOWN controls.
Its [review-pin run37516912022](https://github.com/krshk30/project-mai-tai/actions/runs/37516912022)
was blocked at base ancestry. The authorized rebase resolves that cause.
Parent still owns independent review pin and C-row; no review-ledger or shared
handoff edit was performed. The former c21 pair is historical, not final proof.

Publish the final exact SHA and both push/pull-request Validate URLs on the PR
only after both conclude success. No production-after latency, eliminated429
rate, broker quota equivalence, install, or deployment success is claimed.

## Authorized After-Close Fixture Correction

Human parent authorized only the test fixture correction in
`tests/unit/test_roundup1.py::test_recorded_sckt_pa1_resubmit_recomputes_ceiling_then_serial_lane_sizes_wire`.
At frozen78ee3503, PR Validate37521817116 passed6590 units and the isolated
PostgreSQL16 full golden86/1 xfail. Push Validate37521807711 first crashed with
native CPython/SQLAlchemy exit139 (root cause unproved); one same-head retry
completed6589 units/1 failure. The sole failure was the exact ROUNDUP test
above, whose real-clock NFQ gate correctly returned `resting_window_ended`
after16:00 ET. Push golden/Ruff were skipped, not green. That head was NOT ready.

Own controlled original-test probes at16:06 ET failed on both untouched
main4805 and78ee with the same `request, = adapter.requests` ValueError and
NFQ reason. The test and NFQ runtime source were identical between those trees.
Original BASE log `.webull429-roundup-current-base-probe.log` (base worktree)
SHA256 `4b05610f58f70772be18068d55ea54e17b0f4891f940bb6ec3bbe35727d46b97`;
original HEAD log `.webull429-roundup-current-head-probe.log` (build worktree)
SHA256 `34e14f84e0d611b4283322aa1367620930a5988b889c4f65c18b7ae2614b83da`.

The corrected fixture pins the test-local OMS `utcnow`, initial event
`produced_at`, market metadata and quote timestamp to SCKT's recorded
submission `2026-10-05T16:47:03.808217+00:00` (12:47:03 ET), using the existing
recorded row. NFQ and its generated retry consequently share that clock/date.
All original wire-stop1.07, limit1.08, quantity278 and one-submit assertions
remain unchanged; explicit recorded-clock and real entry-window assertions
are added. No runtime source, trading window, gate, timer, or other test
fixture is changed. This remains a controlled PA1 interleaving around recorded
prices, NOT a claim of historical deferral, execution, ledger repair or live BUY.

At after16:00 ET, the corrected singled test passes1/1; log
`.webull429-clock-v3-target.log` SHA256
`142e711fac20a466998c8bbeb42619edb1e1722cf84dfe39f564f9b894494392`.
The focused ROUNDUP/deferred/NFQ/RPG-NFQ/WEBULL suite passes638, log
`.webull429-clock-v3-focused-real.log` SHA256
`b553bc444dd63361012520d2c3432d41131f76b9414c8619588a3e706f2eeeeb`.
An initial focused command named a nonexistent test file and exited4 during
collection; `.webull429-clock-v3-focused.log` is retained, excluded from proof.
The corrected focused command above collected and executed all638 tests.
Controlled ALL-ON152/152, zero mismatch/UNKNOWN, log
`.webull429-clock-v3-all-on.log` SHA256
`28de8dbb6d1e42ab3ea5e5ce8b805702f51f929b2396e73bcf007172276a1266`.
All20 mutations re-run with19 direct assertion/DID NOT RAISE kills and one
separate decoder UNKNOWN exception; v3 raw hashes in `MUTATION_AUDIT.md`.

Runtime/WEBULL-control hashes remain exactly those in the replacement receipt
above. The sole changed test file's SHA256 is
`464e3fbf5209335cf066bad3ab55c796a7daf43c376ce9badf9c8f7f80f33e50`.
The candidate full suite starts only after that change and retains that hash
through completion. No local full PostgreSQL golden BASE/HEAD pair is claimed.
Both new exact-head hosted Validate runs must succeed before readiness.

Fresh after-close full pair, exact normal-PATH command above, pinned main4805:

| Clock-correction run | Passed | Failed | Warnings | Duration |
| --- | ---: | ---: | ---: | --- |
| Untouched main4805 BASE | 6468 | 57 | 455 | 293.97s |
| Clock-corrected candidate HEAD | 6534 | 56 | 455 | 293.98s |

BASE-only failure is exactly the authorized ROUNDUP test named above;
HEAD-only failures: zero. HEAD names exactly match unchanged
`MAC_HEAD_FAILED.txt` (SHA256 `6799163e6462c341489ccc9c1f1d8c1bf24cf9d8f2952543a593dbb5e69ff416`).
`MAC_CLOCK_BASE_FAILED.txt` records all57 current BASE names without dropping
the reproduced clock-sensitive failure. The earlier identical56-name pair
is retained as historical evidence, not substituted for this new pair.
65 new WEBULL controls plus one corrected existing fixture yield66 more
passing outcomes versus this after-close BASE. No test skip or PATH change.
Canonical names are extracted from FAILED lines; raw logs remain unedited.

| Clock-correction raw log | Location | SHA256 |
| --- | --- | --- |
| `.webull429-clock-v3-base-unit.log` | base-proof worktree | `907ab32d4e42e71c07bbdb6eaa651c28f26259bb2eaa64f9335187dfa11df6f0` |
| `.webull429-clock-v3-head-unit.log` | sole build worktree | `2ce0e4670a4082374a0977b32f65f5260601f9b7f8b3a0d68b11996d24c7b427` |
| `.webull429-clock-v3-replay.log` | sole build worktree | `fc4b3d32437d6d7d0fe0be7c3d83df234055d3a4d8954dc327d3ed13e76677be` |
| `.webull429-clock-v3-ruff.log` | sole build worktree | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `.webull429-clock-v3-markers.log` | sole build worktree | `22ae18ed7788aef47a544aa32fbc36022be54bd773174d04e29bf4e9bb411292` |

SQLite/replay/backtest71 passed/1 xfailed,7.66s. Ruff and marker isolation pass.
No local PostgreSQL golden full pair or VPS baseline was run. The adapter,
store, service and WEBULL test hashes remained unchanged from78ee throughout
this candidate run; the clock-corrected test hash above also stayed unchanged.
Only this test fixture and this lane's review artifacts enter the new commit.
