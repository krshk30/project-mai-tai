# Integration Tests

Cross-service tests for OMS, broker adapters, reconciliation, and restart recovery.

`test_acceptance_sql_windows.py` requires a real PostgreSQL database at
`MAI_TAI_DATABASE_URL` and the `psql` client. Validate provisions and migrates
that database. The test fails rather than skips when either dependency is
missing, because a silently absent database would make the runtime window proof
vacuous.

The fixture now exercises both sides of each report's acceptance signal rather than relying on
empty joins: D6 has real `broker_order_events` and preceding sell fills for the refused-exit
numerator/episode denominator, and identity has linked `broker_orders` for `order_rows`. The D6
fixture also reproduces every compiled `base_*` control from relational rows; tests pass the SQL's
actual `CONTROL_*` output into `evaluate`, so a moved control makes the report
`COULD_NOT_TELL`. Both modules carry rows exactly at `since` and `until`, pinning the declared
half-open interval (`since` included, `until` excluded). This remains a window/control harness,
not an exhaustive test of every grading branch or production population shape.
