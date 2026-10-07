# RETAIN1 build verification

Base 994f08aee2c35b3809b3c3384e0d8628807dcfb7; isolated worktree.

Focused command:

```text
PYTHONPATH=src python -m pytest -q
  tests/unit/test_retain1.py
  tests/unit/test_momentum_paper_service.py
  tests/unit/test_momentum_paper_engine.py
  tests/unit/test_prune_reconciliation.py
  tests/unit/test_prune_strategy_bar_history.py
  tests/unit/test_scanner_cycle_history_restore.py
  tests/unit/test_linesrc1_event_driven.py
  tests/unit/test_line_restore_r6_spanning.py
  tests/unit/test_momentum_gateway_handoff.py
```

Initial 217 passed, 8.82 seconds; follow-up 218 passed, 8.54 seconds, including
test_batch_receipt_measures_delete_not_selector_proxy. Delete/count SQL is exercised on SQLite rows with FK
enforcement; PostgreSQL-specific size/lock/vacuum operations are mocked. Read-only
PostgreSQL planner receipts for the paper/scanner statements are recorded separately.
This is not a production deletion/VACUUM execution or production load benchmark.

## Individual mutations, each reverted

| Mutation | Failed | Verdict |
|---|---:|---|
| M1 remove scanner type restriction | 1 | RED |
| M2 remove paper type restriction | 2 | RED |
| M3 age bars by created_at instead of bar_time | 1 | RED |
| M4 remove surviving-findings parent guard | 1 | RED (FK protection) |
| M5 do not decrement the shared run budget | 1 | RED |
| M6 delete the exact cutoff boundary too | 2 | RED |
| M7 remove dry-run write barrier | 1 | RED |
| M8 persist PATH_PRINT again | 10 | RED |
| M9 reduce the bar floor from 45 to 7 days | 2 | RED |
| M10 remove the runtime budget | 1 | RED |
| M11 follow-up: replace measured batch elapsed time with zero | 1 | RED |

100 tests per mutation turn (retention + Momentum service); all ten mutations
produced assertion failures or FK enforcement failures, not collection/import errors.
M11 ran the new receipt test alone, was restored, and then that test passed.

## Full unit pair

Same command/env, serial runs on this Mac: PYTHONPATH=src python -m pytest
-p no:cacheprovider -q tests/unit --junitxml=<receipt>.

| Tree | Passed | Failed | Wall time |
|---|---:|---:|---:|
| base 994f08ae | 7,114 | 56 | 484.27 s |
| candidate source | 7,184 | 56 | 489.28 s |
| follow-up source (batch timing receipt) | 7,185 | 56 | 479.03 s |

Failed-name difference: candidate-only=[]; base-only=[]. All 56 names are committed
in FULL_UNIT_PAIR.json. Raw XML: /tmp/retain1-base-unit-20261007.xml and
/tmp/retain1-head-unit-20261007.xml. The additional 70 tests passed.
The pair is NOT green: failures include platform-specific installer/hash utilities
and the paced producer throughput test. They are not waived or rewritten by RETAIN1.
Follow-up failed-name difference: candidate-only=[]; base-only=[]. Receipt:
/tmp/retain1-followup-unit-20261007.xml, summarized in FULL_UNIT_FOLLOWUP.json.
No independent review pin or installation completion is claimed. Both hosted Validate
runs on previous head 6561b642 passed; new-head CI must be checked separately.

Dependency ruling accepted: scheduled capture readers are recent-only; historical studies
are hand-run; pullback uses retained bars. Production 50k SELECT times: findings
1.159158 / 0.481628 s; runs 0.813721 s. No DELETE/VACUUM was executed on the box.
Actual DELETE timing/batch choice remains a post-pin, post-job-2 execution prerequisite.
No before/after purge sizes exist because no purge was run.
