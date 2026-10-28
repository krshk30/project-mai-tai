# L7 STAK Fake-SDK Input Correction

Task: L7-ORB-WEBULL-TICKCAP-20261022 (same sole source owner).
Source: human/main actual381e focused79 result and request for test-only repair.
Priority/status: immediate source-only correction; new runtime claim pending.
Parent frozen FAIL: 381e9ab51bf944ff43fbf5c84dd43ef305641b68.
Branch: codex/l7-orb-stak-sdk-fixture-20261022.
Worktree: /private/tmp/mai-tai-l7-orb-stak-sdk-fixture-20261022.
Next: main source review, exact new command/output claim, then main-only79 run.
Completion: reviewed source plus actual new run, not static evidence or live GO.

## Retained Evidence And Assessment

Read actual raw L7-381E-FOCUSED79-20261022.log under the main queue analysis
directory and the actual adapter mapper/fake SDK/helper/no-symbol controls.
Main run39193: exit1, 67 PASS /12 FAIL /6.62s. All twelve failures are the new
test_selected_stak_prices_reach_actual_webull_fake_sdk ask/session combinations
at line126, with the same warning: lookup returned AAPL913256135/BMGL951002116
only, no STAK. This result is retained FAIL, not reclassified or rerun by author.

_resolve_instrument_id normalizes the requested symbol, reads
Instrument(client).get_instrument(symbols=key, category="US_STOCK"), requires
an exact matching row symbol and nonempty instrument_id/instrumentId, ranks
status (OC preferred), then caches that ID. No matching row returns None.
_submit_blocking correctly rejects that before PlaceOrderRequest/placement.
The shared fake_sdk registers only AAPL/BMGL by default. The failure is a missing
input in this newly authored STAK positive fixture, not a price guard or mapper
production defect and not evidence the intended wire assertions passed.

## Exact Test-Only Correction

Use the EXISTING _instrument_stub helper, after fake_sdk setup and only inside
the twelve STAK positive controls, to supply a list with symbol=STAK,
instrument_id=synthetic-STAK-instrument, status=OC. These are explicit SYNTHETIC
fixture identity values, not guessed native STAK identity or live attestation.
The SDK response schema and actual mapper/adapter remain in the exercised path;
no monkeypatch of _resolve_instrument_id, instrument cache or production source.
An added assertion pins the outgoing instrument_id to that fixture mapping.
Every existing accepted/type/cap/stop/EH/tick-adjustment/one-placement assertion
is unchanged. No test added/removed/skipped, same intended two-module79 selection.

The shared SDK fixture and all existing no-symbol/no-ID, recycled-ticker and
no-matching-symbol refusal behavior are untouched. Its per-test module setup
continues to isolate this local response from other tests. New source/test result
is UNRUN; no success beyond the observed lookup miss is inferred from source.

Production delta vs381e: EMPTY. Unsent-cleanup test module unchanged. No app,
economic writer, broker adapter, POM, actor/runtime/UI/ops or shared queue/status/
LATEST edits. Prior 8330/381e worktrees and actual failure receipts untouched.

Proposed MAIN-ONLY new command after exact-head/source review and fresh claim:

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider tests/unit/test_l7_orb_webull_tickcap.py tests/unit/test_l7_orb_webull_unsent_cleanup.py --junitxml=/Users/velkris/.codex/task-queues/analysis/L7-STAK-SDKFIX-FOCUSED79-20261022.xml
```

Main must claim fresh raw log and output paths and actual clean new head before
launch; this is not a run receipt or permission to overlap another executor.
Full preservation modules from prior handoffs remain separately owed, including
test_webull_adapter.py mapper/no-ID controls. No new worker tests, imports,
runtime, compile/AST/parser, DB/provider/SSH/native order/live replay/push/install/
restart were performed. No PASS from whitespace/static Git checks.
