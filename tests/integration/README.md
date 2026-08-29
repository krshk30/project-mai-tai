# Integration Tests

Cross-service tests for OMS, broker adapters, reconciliation, and restart recovery.

`test_acceptance_sql_windows.py` requires a real PostgreSQL database at
`MAI_TAI_DATABASE_URL` and the `psql` client. Validate provisions and migrates
that database. The test fails rather than skips when either dependency is
missing, because a silently absent database would make the runtime window proof
vacuous.

The relational fixtures prove the requested-window predicates for D6's `target_buy_legs`,
`target_mirror_symbols`, `target_matched_orders`, `target_refused`, and `target_exit_episodes`.
Asymmetric broker rows keep the mirror-symbol and matched-order bounds from masking each other;
separate BUY and SELL rows at exactly `until` prove both fill populations exclude that boundary.
Identity separately exercises `intent_rows` and `order_rows`: an inside intent linked to an
exact-`until` order makes deletion, loosening, or laundering of `bo.submitted_at` observable in the
database result rather than through SQL-string inspection.

D6 also has real `broker_order_events` and preceding sell fills for the refused-exit
numerator/episode denominator and reproduces every compiled `base_*` control from relational rows.
Tests pass the SQL's actual `CONTROL_*` output into `evaluate`, so a moved control makes the report
`COULD_NOT_TELL`. This remains a window/control harness, not an exhaustive test of every grading
branch or production population shape.
