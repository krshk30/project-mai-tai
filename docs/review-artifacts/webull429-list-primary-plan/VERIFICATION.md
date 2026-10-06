# WEBULL429 build verification

[codex] 2026-10-06 ET. Sole branch writer; production and shared handoff untouched.

## Pinned Full Unit Pair

Base: `c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`, detached managed worktree
`/Users/velkris/.codex/worktrees/webull429-base-proof/project-mai-tai`.
Head runtime: `b848ca261df41d91cbf171ffe6600411dcb9d0d6` plus the final
comments-only quota clarification and flag-inventory test correction.
These are the complete source/test changes in the verification commit; subsequent
CI verifies its exact published SHA. No runtime file was edited during this pair.

Same command in each worktree, normal PATH, existing requested venv:

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider tests/unit
```

| Run | Passed | Failed | Duration |
| --- | ---: | ---: | --- |
| Pinned main | 6438 | 56 | 281.98s |
| Finished head | 6480 | 56 | 269.20s |

`MAC_BASE_FAILED.txt` and `MAC_HEAD_FAILED.txt` are identical, SHA256
`6799163e6462c341489ccc9c1f1d8c1bf24cf9d8f2952543a593dbb5e69ff416`.
Zero new failure names; 42 new passing controls. Normalize only an interleaved
absolute-path warning suffix after a pytest failure node before comparison.

The actual Mac baseline is56 rather than the usual47:44 fanout installer shell
controls, one log-retention shell control, one unattended-upgrade shell control,
one dead-consumer throughput timeout, and nine sync-only controls. Shell controls
encounter missing `/usr/bin/sha256sum`; sync-only subprocesses encounter normal-PATH
system Python's unsupported union annotation. No PATH adjustment, unrelated
repair, skipped test, or production installer execution was used to turn them green.

Local raw logs remain in the build worktree (git-ignored):

| Log | SHA256 |
| --- | --- |
| `.webull429-base-unit.log` | `7458a9ffaf9acca9513c52789abf6b3e1e73aebfa76954b36862121bf87db4e5` |
| `.webull429-ready-unit.log` | `d85897b50950f9a675aefe84fbd7acfb51ee873de1cf3319a66abd1f505a5e9a` |

## Focused and Mutation Controls

401 passed across `test_webull_list_primary.py`, `test_webull_adapter.py`,
`test_rpg1_buy_readback.py`, `test_expected_flags_check.py`, `test_all_on_pm.py`,
`test_schwab_never_synthesize_flat.py`, `test_webull_attach_protection.py`,
`test_webull_exit_pair_session.py`, `test_webull_protect_retry_horizon.py`, and
`test_pmprint1_tonight_flags.py`. New file contributes42 controlled cases.
Recorded IDs/prices/timestamps are distinguished from synthetic response variants.

```sh
env -u MAI_TAI_DATABASE_URL PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider tests/integration/test_strategy_oms_roundtrip.py tests/replay tests/backtest -q
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m ruff check src tests
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python ops/health/check_log_marker_collisions.py
```

SQLite/replay/backtest:71 passed,1 xfailed. Ruff:pass. Marker isolation:pass,
77 literal consumers/380 emitted markers/975 logging formats. PostgreSQL
integration acceptance is delegated to isolated CI PostgreSQL16, not a live DB.

All ten process-local mutations killed (pytest return1, harness return0):
`detail_first_restored`, `nested_foreign_client_adopted`, `stale_page_served`,
`budget_not_counted`, `page20_claimed_complete`, `missing_child_claimed_readable`,
`first_filled_lost`, `durable_proof_ignored`, `forever_attempted`,
`strict_read_uses_list`. Reproduction for each name:

```sh
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/webull429-list-primary-plan/mutation_check.py MUTATION_NAME
```

## Ubuntu Evidence and Readiness Gate

Pinned-main [Validate run37499436771](https://github.com/krshk30/project-mai-tai/actions/runs/37499436771)
is green:6494 unit passed,86 integration/replay/backtest passed,1 xfailed.
This is measured CI, not an assertion that the usual48 Linux failures apply here.

Initial source [push run37515566921](https://github.com/krshk30/project-mai-tai/actions/runs/37515566921)
measured6535 passed/1 failed. The sole failure was the PM catalog inventory's
hard-coded151 consumers after adding the new OMS-only flag; its assertion now
expects152 and explicitly checks the new flag ON. No PM rule was changed.
This failed run is retained, not represented as green evidence.

Main advanced to `4805ddc81184c76b4d5cef5c483c809edb666fe6` during testing:
only ORB page display, control-plane tests, runtime-registry tests, and their
review artifacts changed. There is no overlap with this lane. No rebase/merge
was performed; final PR Validate checks GitHub's current merge composition.

Publish the final exact SHA and both push/pull-request Validate URLs on the PR
only after both conclude success. No production-after latency, eliminated429
rate, broker quota equivalence, install, or deployment success is claimed.
