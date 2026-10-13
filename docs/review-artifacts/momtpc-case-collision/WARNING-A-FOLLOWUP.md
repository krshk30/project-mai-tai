# Warning-A Followup

Additive human authorization after paper component `cb6f84ba`: guard warning A
before upstream ticker normalization, no live T/Q behavior changes, no overlap
with main's publisher/gateway snapshot-offload files.

Only `market_data/massive_provider.py` changes in this followup: nine added lines.
`_handle_messages` discards mixed-case A only when the warning wildcard route has
been used, before `str(symbol).upper()`. `_normalize_warning_aggregate` also
requires the raw SDK ticker to be exact uppercase and equal to its symbol argument.
Excluded preferred frames do not cause an invalid-aggregate safety revert. Common
malformed A still causes the existing revert. No 2s threshold/window/1008/CAL
policy modification. T/Q and non-warning explicit A preserve existing behavior.

New file has 11 controls. Corrected baseline: 7 RED / 4 PASS; first harness receipt
has two additional AttributeErrors from `ask` vs actual `ask_price`, corrected
before source edits and preserved separately. Full focused selection: 318 PASS,
0 FAIL/ERROR/SKIP in3.15s, including the prior187 paper controls. One existing JUnit
record_property warning from a bounded SDK-yield fairness unit control, not a
60s benchmark, real peak measurement, live-provider run or soak qualification.
Ruff/diff-check PASS. No standalone benchmark or production/provider action.

Evidence: `/Users/velkris/.codex/task-queues/analysis/momtpc-warning-a-t-boyle-20261013/`.
Original sealed paper packet and read-only consumer sweep remain preserved.
Warning-A case-erasure gap in the old table is now locally fixed. Scanner/strategy
normalization remains read-only/affected; live T/Q normalization remains an
explicitly unchanged source risk per human no-live-TQ ruling. Already-merged
uppercase durable prior-close values still cannot be recovered by this guard.

This is source/control completion, not review/install/re-arm/real-warning soak.
Main retains strict2s gate and all publisher/gateway source/composition authority.
Main alone retires exactly2 audited phantom TPC trades after reviewed install.
