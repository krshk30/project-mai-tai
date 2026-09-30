# Declared fleet changes in restart evidence

The 2026-09-30 pre-open report used a Sunday snapshot but listed only `oms`,
`strategy`, and `schwab-1m-v2` as restarted. The approved 2026-09-29 install
also restarted `orb` and first installed `orb-schwab`. The checker therefore
called ORB's changed PID a failure even though it matched the install record.
This was a check-input defect, not evidence that trading violated its design.

Before running `report`, derive the declared service changes from the verified
deployment journal, not from the old wrapper's hard-coded list. For the
2026-09-29 install, that means four `--restarted` arguments (`oms`, `strategy`,
`schwab-1m-v2`, `orb`) and `--new-service orb-schwab`. Check the observer's
running flags with `--expect-flag` values for
`MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=true` and
`MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=false`. The actual source is
`/home/trader/fleet_health/deployments-20260929.md`; a future wrapper must
not silently infer approval from a changed PID. Re-pin the wrapper only under
a separate install authorization.

An installed observer absent from the snapshot and not declared new is
UNKNOWN. A previously captured service whose PID changed without a declared
restart is UNKNOWN, not a proven deployment failure. Both outcomes remain
nonzero and block readiness. A deliberately inactive service starting, a
declared restart that does not return healthy on a new PID, or a declared new
observer that does not start healthy after the snapshot is a REAL FAILURE.
Snapshots taken after the observer is installed include it, so subsequent
restarts compare its PID normally rather than treating it as new again.
