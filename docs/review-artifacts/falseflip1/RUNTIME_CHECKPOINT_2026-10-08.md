# FALSEFLIP1 runtime checkpoint (not review ready)

Sole writer: Codex, branch `codex/falseflip1-entry-close-budget-1008`, PR #1135.
No production action, merge, pin, activation, or migration application.

## Scope implemented in this checkpoint

- Exact entry-bound completed-live-bar classification, including already-closed
  managed rows. Managed proof and owning entry-order display proof share a transaction;
  native execution mechanism, fills, exit timing, and original timestamps remain intact.
- Additive nullable JSON migration `20261008_0023`, based on `20261005_0022`.
  Old projections work with the nullable column present. Deployment requires migration
  before new OMS/v2 code and a control restart for display; rollback leaves the column.
- Default-OFF setting; catalog expected true, checked on both OMS and v2. This candidate
  adds two process checks (153 total on this isolated base), not a live audit receipt.
- Durable per-segment episode/compensation/skip/cancel/draft budget with CAS; siblings
  count once. No destructive decrement of the old close ledger. REAL/UNKNOWN/held
  siblings do not authorize a refund. Existing RETRY_ONE=true, max_retries=0 remains.
- Ordinary placement after an exact cancellation receipt. After the second false
  episode the logical wait is off-wire for the skipped cross; persisted skip plus
  below-level observation prevents duplicate deliveries/restart becoming another cross.
- Classification runs on an owned bounded OMS worker, not the serial intent consumer.
  Explicit OFF creates no worker. Cancellation retains ownership of an in-flight DB
  thread until its transaction finishes. Bar capture/cross checks are memory-only.

Lane F integration dependency: the `false_flip_restore` purpose must compose with
F's exact cancel-publication CAS/inventory marker. No F cherry-pick or side patch was
made; parent owns the reviewed integration hunks.

## Measured receipts

Fresh runtime focus after worker lifecycle controls: 58 passed, 1.55 seconds.
Ruff: clean for the changed runtime, new tests, migration, and data scripts.
The earlier 76 new classifier tests plus 68 existing controls were 144 total, not
144 new tests. Historical population remains 90 legs / 61 slots, 24 FALSE legs /
17 FALSE slots, 64 REAL legs, 2 UNKNOWN; 16 historical FALSE legs lack durable binding
and cannot be refunded in production. Controlled binding permutations are not fills.

## Interrupted full run: not parity

Own PID 13322 was verified by command and exact worktree cwd, then interrupted with
SIGINT after more than 26 minutes. No other lane process was touched.
`/tmp/falseflip1-runtime-full.xml` is PARTIAL: 6,232 cases, 72 failures, 6,105 passes,
55 skips. XML's 508.733 seconds is not the observed process wall time. Source/tests
changed after process imports; inspect.getsource/linecache failures are not valid
frozen-source evidence. These tests/thresholds were not weakened to match that run.
Parent stack: `/tmp/falseflip1-parent-full-process-sample.txt`.

Last completed node was `test_trade_tick_can_emit_intrabar_floor_breach_close`;
next collected node `test_trade_tick_uses_monotonic_bar_count_after_history_trim`
passed independently in 8.71 seconds (`/tmp/falseflip1-stalled-node.txt`). An intrinsic
hang in that node is not established. Three literal hot-stall failures remain in the
partial receipt; they are not waived. Mirror claim-expiry IndexError needs a fresh
reproduction. Next source-inspection checks run only after this checkpoint is frozen.

## Remaining review blockers

Three-false lifecycle/manual-stop flow controls; full recorded FLYE bar/book/emitter
replay (current 17-slot tests are labelled controlled runtime permutations); runtime
assertion-red mutations; full frozen-source pair against the shared main baseline.
Historical min/max are bars, not counterfactual broker fills. Controlled page row is
not a live screenshot. No completed full-suite parity or H1-H7 readiness is claimed.
