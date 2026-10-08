# Lane C mechanics evidence, October 8

This receipt is isolated local verification, NOT production preparation,
staging, flatness, review of trading code, or an install COMPLETE.

## Verified

- Import path: this worktree's absolute `src/project_mai_tai/__init__.py`.
  Shared .venv Python, normalized PATH. No baseline suite duplicated.
- Five-service package mechanics: 265 PASS, 2.10 seconds. XML
  `/tmp/oct8-lane-c-mechanics-publish.xml`, sha256
  `ce3b78516f59e0de49f1ab84960d7cef6e33ddeccdec53f85f744bd5af683e65`.
- Assertion controls: 16/16 mutations RED, each applied alone to a disposable
  private package and discarded. Receipt `/tmp/oct8-lane-c-mutations-final.json`,
  sha256 `1840b4501a88ff55fa0d75ce9df76a4aed9042688da053e9ccee3b080c130f90`.
- Ruff PASS excluding immutable historical box fixtures; no application
  source, ops, scripts, shared handoff or ledger delta against main1e15adb0.
- Actual existing daily `verify_runtime()` executes successfully AFTER the
  full five-service re-pin/retirement in the isolated root fixture. SHA/tree,
  runtime dependency hashes, all required evidence and root ownership/modes
  are checked by that unchanged implementation. Daily paper/date logic is
  byte-identical. No test invokes a live daily gate or broker/service action.
- Literal isolated runner completes the target order OMS (strategy via repo),
  v2, orb-schwab, control, retirement; fresh gate before every step. It records
  the official new-inventory snapshot and five-service install record, not
  the old paper ORB PID. Measured blockers prevent the target/recovery.
  Actual identity drift between capture and the first write also refuses;
  the runner never adopts a different PID as its approved baseline.
- Old orb-schwab upgrade ACK preserved as history, superseded only by the
  actual recorded authorized new NRestarts0 identity, not an arbitrary PID.
  Missing dependency, altered historical evidence, omitted record, extra
  restart or retired-ORB snapshot population refuses the re-pin.

## Mutation Population

| Mutation | Deciding control |
| --- | --- |
| First write before close | ET first-write window |
| Retry a measured blocker | rc2-only three-attempt policy |
| Turn RPG handoff ON | Retained false env key |
| Omit WB from union | Required WB+ORBLIVE source provenance |
| Accept changed union tree | Exact reviewed landing tree/diff |
| Stage before close | Refusal before remote directory/unit write |
| Omit runtime artifact | Twelve required daily dependencies |
| Omit evidence input | Ten required evidence dependencies |
| Keep old upgrade ACK current | New authorized identity + historical ACK |
| Adopt retired ORB | New official fleet excludes paper ORB |
| Alter other owner | Four owners/marker preserved byte-for-byte |
| ORB tombstone not empty | Empty normal replace proof |
| Unrelated cursor accepted | Applied ID >= our exact request ID |
| Duplicate empty replace | Exactly one request |
| Skip retirement proof | Separate positive inactive/disabled proof |
| Skip restart gate | Real literal target sequence stops before target |

## Outstanding, Not Hidden

Final APP and its reviewed source receipts are blocked until #1127 is pinned
and merged (+ lane A only if pinned). No final release/approval hash can be
claimed yet. A fresh AFTER-CLOSE baseline capture and acknowledgement of
v2's observed08:58:11 restart must be provided, not the obsolete PID1895743.
All actual after-close trading gates, five starts, ten-minute proofs,
retirement cursor and preopen write receipts remain UNMEASURED.

## Full-Unit Pair: Failed Names NOT Identical

Parent's retained exact-main1e15adb0 run: 48 failed / 7674 passed / 55 skipped,
606.61 seconds. Own real Git checkout e90dcccc89cd9220e2cfb8dd8916454e0b5b4db1:
48 failed / 7674 passed / 55 skipped, 621.62 seconds. XML
`/tmp/oct8-lane-c-head-unit.xml`, log `/tmp/oct8-lane-c-head-unit.log`.
No application source, ops, scripts or unit-test delta against the baseline;
subsequent changes are confined to the owned docs/job package.

The XML failed-name comparison has 47 common failures, one added and one
removed. The two deciding failures are wall-clock loop-stall thresholds:

- Added on head: `tests.unit.test_nfq2_hotfix1_combined_proof::test_combined_240_events_per_second_60_seconds_active_hold`,
  155.979 ms against the unchanged <50 ms assertion (PASS on baseline).
- Removed on head: `tests.unit.test_hotfix1_symbol_tick_cache::test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-off-existing-row]`,
  baseline 52.598 ms against the unchanged <50 ms assertion (PASS on head).

Sorted classname::name joined with newline and NO terminal newline:
baseline SHA256 `e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`;
head SHA256 `5e63ad6caec5865100ce4c8b64524e2fcdc12810adc7b807d53b9e074e61e20f`.
Equal totals are NOT suite parity. Host contention is not proven as the cause;
no threshold or trading code was changed to obtain green.
