# Integration Tests

Cross-service tests for OMS, broker adapters, reconciliation, and restart recovery.

`test_acceptance_sql_windows.py` requires a real PostgreSQL database at
`MAI_TAI_DATABASE_URL` and the `psql` client. Validate provisions and migrates
that database. The test fails rather than skips when either dependency is
missing, because a silently absent database would make the runtime window proof
vacuous.
