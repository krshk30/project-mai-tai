# LINESRC1 Candidate Evidence

[codex] Source-only candidate on `codex/linesrc1-anchored-session-poll`.
Local comparison base: `4805ddc81184c76b4d5cef5c483c809edb666fe6`.
Main was independently observed at `52659779bcf4b6e28a2896e6eef3002488a6d2c9`
after that baseline was frozen. No rebase, merge, production action or review
pin is claimed here. Hosted CI and independent exact-head pin are separate
requirements; this lane cannot release installation.

## Source Boundaries

Only the anchored full-session poll is gated to `06:55 <= ET < 16:00`, with
`America/New_York` conversion rather than fixed UTC offsets. Outside that
window it neither fetches nor changes epochs, validates source payloads or
logs source failures/cycles. Ordinary quote/recent-bar paths and cadence,
concurrency and quota budgets are unchanged.

Validated history lacking the current closed candle returns its actual bars
and `None` proof. The service preserves its previous coverage/revision but
records a same-epoch source wait; strategy and final draft readiness remain
closed. An old rebuild cannot release that wait. Only a valid same-epoch
current response clears it. No partial-history bar is dispatched to strategy
or persistence in this waiting branch, and no missing candle is invented.
Duplicate/foreign/malformed/truncated sources remain genuine errors and
invalidate coverage. Warning suppression compares semantic state, not dynamic
error text; each actual error still invokes failure invalidation.

The source files for the R6 ledger, reviewer-owned acceptance script/factory,
ops and default flag catalog are byte-unchanged from the comparison base.
No ten-clean-live-bar, real-hole or draft-version requirement is weakened.

## Verification

Final source/test files were frozen before the full-unit comparison. An
earlier full-head run exposed a test assertion that counted an unrelated INFO
log as a source warning. That run is superseded and retained at
`/tmp/linesrc1-pre-final-head-unit.{xml,log}`; it is not a valid final receipt.
The assertion now selects only the source marker, and a new dynamic-error
control checks quiet warnings without skipping failure invalidation.

- Focused baseline: 94 PASS; final focused candidate: 149 PASS, zero errors.
- Complete baseline: 6468 PASS / 57 FAIL / zero errors, 259.76s. Final candidate:
  6523 PASS / the same 57 FAIL / zero errors, 275.71s. Added/removed failed names:
  `[]` / `[]`. No complete-unit GREEN claim: all 57 inherited failures remain,
  including the base4805 host-clock-sensitive SCKT test after16:00 ET.
- Isolated mutation controls: 20 baseline GREEN, 20 semantic kills. Actual
  failed call phases: 35 `AssertionError`, two `pytest.fail.Exception` (`Failed`).
  Zero setup/teardown/non-assertion failures and zero harness errors.
- Unchanged recorded acceptance runner: 123 cases, byte-identical base/head
  output. First checkpoint: 90 MATCH / 33 HELD; plus10: 98 MATCH / 25 n/a.
  Aggregate: 188 MATCH / 33 HELD / 25 n/a, zero errors or incomplete entries.
  This is preservation, not a claim that every case was admitted.
- Targeted lint and whitespace validation PASS.

All commands use `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:.` and the local
existing virtual environment. No production broker GET, DB connection,
token refresh, service operation or order dispatch occurs in these controls.

```sh
python -m pytest -q -p no:cacheprovider tests/unit --tb=line \
  --junitxml=/tmp/linesrc1-head-unit.xml
python -m pytest -q -p no:cacheprovider \
  tests/unit/test_linesrc1_anchored_session_poll.py \
  tests/unit/test_line_chart_restoration.py \
  tests/unit/test_line_chart_restoration_integration.py \
  tests/unit/test_line_restore_r6_spanning.py \
  tests/unit/test_line_restore_acceptance_factory.py \
  --junitxml=/tmp/linesrc1-head-focused.xml
python docs/review-artifacts/linesrc1/MUTATIONS.py
python scripts/line_restore_acceptance.py \
  --bars /private/tmp/claude-502/-Users-velkris/c1695851-2452-4d8f-aa52-221e7faecd49/scratchpad/v2_bars_0928_1006.csv \
  --events /private/tmp/claude-502/-Users-velkris/c1695851-2452-4d8f-aa52-221e7faecd49/scratchpad/v2_events_0928_1006.txt \
  --json /tmp/linesrc1-head-123-acceptance.json
```

The full baseline command ran from the separate clean
`/Users/velkris/.codex/worktrees/mirrorhold1-main4805-proof/project-mai-tai`
checkout. Candidate commands ran from
`/Users/velkris/.codex/worktrees/linesrc1-anchored-session-poll/project-mai-tai`.
The mutation runner compiles the entire target module/class in isolated
processes, preserving original method class closures. It changes no checkout
files, and classifies runtime/harness errors separately from semantic kills.

## Raw Evidence

Bounded production-read receipt and exact cutoff are documented in `STEP0.md`:
77 headers paired with 77 tracebacks, all outside the polling window, from
October6 `20:00:05.344` through `20:11:08.069 ET`. This is not a daily census.

| Own local artifact | SHA256 |
| --- | --- |
| `/tmp/linesrc1-base-unit.xml` | `71667f843ecbc6d79d562cde4eb1511eedfbfdf9ff82dcb302dfbfb0192f7395` |
| `/tmp/linesrc1-head-unit.xml` | `599e5430b450b163c1a0a1e52c1c4d4c33a09d128e9d6b24e11960eb1699f5a4` |
| `/tmp/linesrc1-unit-pair.json` (all failed names) | `cdad8ad6c74b55d3b3f14f2acaa4a3d1366e859fc2af6acada117a333fd70ee4` |
| `/tmp/linesrc1-head-focused.xml` | `ee0c773efc3286ad4a007f52d3c1157663a6006ab531d9c73e2e8597e87b6ba1` |
| `/tmp/linesrc1-mutations.json` | `691999ed1b9129ceea40adb0a3275b5e31825775633bb3ad9821ce5b42cda3b5` |
| `/tmp/linesrc1-{base,head}-123-acceptance.json` (each) | `b3104ee6525171e5ae427f4a8c806b2925e8586166244b68d2aa0851122a7f81` |
| Retained `v2_bars_0928_1006.csv` | `a16ae56fa2d0acc0520dc69ca1b0112b22accdb7c241e0b5a71d5776d1d1719c` |
| Retained `v2_events_0928_1006.txt` | `888d6ccab0b81b289b1c714f1fef1193d23f6b709951a016b2dabd04ab2b648c` |
| Unchanged reviewer runner | `5ca1547bba7f74919b2f4d2d419d7bebf28cbf01f4d6284007d27d25294d138b` |
| Unchanged reviewer factory | `907b6fc955f57e1ddff9396134a54c7131fcb6b6cc208231b73dbe28c5a4a486` |

Per-control raw logs and phase/exception receipts are retained under
`/tmp/linesrc1-semantic-controls/`. Base/head full-unit and focused stdout are
the corresponding `.log` files. Exact input paths are in the command above.

| Frozen candidate file | SHA256 |
| --- | --- |
| `src/project_mai_tai/market_data/schwab_v2_rest_client.py` | `8a219896233d84e1bca2afb510017440bd1d40c11b6cca4d3af5c29f6021ede7` |
| `src/project_mai_tai/services/schwab_1m_v2_bot.py` | `6d6d20252876d7175898145b64cc5f0282eb11d2ebd36d6a34895a9eddcf3194` |
| `tests/unit/test_linesrc1_anchored_session_poll.py` | `35da2dcd84482080149b4d9728b50530ded0b577531a038180f258313420c636` |
| `tests/unit/test_line_chart_restoration_integration.py` | `757d45ae891bb123c9da0be3195db855bb3ea0397258f9167e04549bf4866ba9` |
| `docs/review-artifacts/linesrc1/MUTATIONS.py` | `48f1c2eb9e8911e34ba41caf50d5cbf600c3262e8581ed07c00ce1201fc7d547` |

## Unmeasured And Release Gates

Recorded candle prices and replay population are actual retained data;
provider response envelopes, source completeness/absence, clocks and callback
interleavings in tests are controlled. Real in-window live provider behavior,
historical execution/fills and deployment are UNMEASURED by this lane.
The unit baseline has inherited failures; neither local whole-suite GREEN nor
successful local Postgres/integration coverage is claimed. Fresh hosted CI,
independent review and exact-head pin are required before source can join a
parent-owned installation. No approval to merge, activate or install is inferred.
