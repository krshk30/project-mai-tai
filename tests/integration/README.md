# Integration Tests

Cross-service tests for OMS, broker adapters, reconciliation, and restart recovery.

`test_acceptance_sql_windows.py` requires a real PostgreSQL database at
`MAI_TAI_DATABASE_URL` and the `psql` client. Validate provisions and migrates
that database. The test fails rather than skips when either dependency is
missing, because a silently absent database would make the runtime window proof
vacuous.

Scope limit: the current seed proves the exercised target denominator and identity-intent
windows only. D6's `target_refused`, `target_mirror_symbols`, and `target_matched_orders`
populations are empty; the seed has no `broker_order_events`, and the identity seed has no
`broker_orders`, so identity's `order_rows` is also empty. Those paths are UNEXERCISED, not
passing: removing their window predicates can still leave this suite green until dedicated rows
are added.
