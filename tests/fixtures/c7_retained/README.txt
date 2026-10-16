C7 retained inputs: offline, bounded, no broker/provider connection.

manifest.json pins compressed and uncompressed SHA256/length plus original
source SHA256/length and exact selection. Loader never skips absent inputs.
Every gzip member is <=2MiB compressed and <=8MiB uncompressed; total stored
input bundle is <2MiB. No original account/envelope context is published.

The four timestamp-filtered v2 bursts are unchanged retained bytes, not a
contiguous log. Single-second GRML is partial;20-21 is the full rolling burst.

The cursor fixture keeps the exact last1MiB of the original19,022,548-byte
log PLUS the preceding256-byte cursor anchor. Only unscanned prefix bytes
are controlled space padding in the test; this is NOT a full-prefix replay.
Every byte inspected by that reset cursor and initial EOF anchor is retained.
The original whole-log hash remains provenance, not a hash of padded bytes.

The market-payload projection keeps all13269 snapshot rows and all original
reference_data values. Only unrelated envelope fields are excluded. Original
retained source digest and13269/12870/399 denominators remain pinned; no row
or eligible-print/volume/price data is synthesized or inferred.

The single timing exclusion is limited to shared-runner Validate:
tests/unit/test_momlatch1_gateway_integration.py::test_gateway_A_240_per_second_60s_original_budget_and_TQ_progress
This node is intact and remains REQUIRED in the approved quiet-host/VPS
pre-install speed gate with its original240/s60s workload and50ms threshold.
The timing marker alone does not waive any other test or pre-install gate.
