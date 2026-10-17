# SYNC2X Hygiene: Required Database Setting

Source proposal only. Not installed or qualified by this artifact.

## Required Setting

`jit=off` is required for the application database's effective service sessions.
Claude reports setting the database default and restarting OMS on October 16,
2026 after measuring 743.8 ms of JIT compilation in the ORB work query. This note
does not independently certify that operational action or every pooled session.

The authorized installer must retain the previous database/role setting, verify
the database identity and effective `current_setting('jit')` on approved service
connections, and record which services have recycled their connections. A database
default does not retroactively reconfigure an existing pooled connection. Never
automatically change role settings or restart unrelated services to satisfy this
note. Claude retains rollback and production-change authority.

## WEEKMAINT Check Plan

Proposed seam: the existing read-only connection in
`ops/weekmaint/backend.py::validate_runtime_inputs`, before claim or writer stop.
Read effective `current_setting('jit')` along with existing database identity,
without a new connection, broker read, `SET jit`, or `ALTER DATABASE` action.

TOP20:0x approves a report-only performance observation, not a safety gate. The
source now reads `current_setting('jit')` in the existing validation connection,
isolated by a savepoint after original identity queries. `on` or unknown yields a
CRITICAL report line and maintenance CONTINUES; `off` yields INFO. The observation
is persisted in existing operation inventory and carried into the maintenance
report across reboot. It does not change health evidence or release a failed
safety fence. Original identity query failures/refusals remain load-bearing.
Controls are authored for actual validation/inventory/report methods with fake
DB/OS leaves. No real DB, connection, maintenance result or tests were executed.
The earlier proposed refusal-if-ON policy is superseded and is NOT implemented.

## Query Acceptance

Run only in main's authorized serial slot against an explicitly approved fresh
local qualification database. No production or application default DSN.

The set-based selector must match the retained db73 predicate, including unrouted
newer BUY supersession, NULL submitted times, explicit-zero versus missing VP,
older held state and unresolved older native pairs. The conservative guard skips
only when no routed BUY has missing/nonzero VP or an unresolved protection phase.
Missing VP is conservatively UNKNOWN until the full selector validates its Fill
fallback. There is no today-only return: older owned/UNKNOWN work and newer
unrouted BUY supersession retain the old semantics.

Required actual evidence: representative box-size cardinality/index/statistics
manifest; old/new truth-set equality; raw PostgreSQL JSON plans with Total Cost
below 100000 and execution at most 40 ms for idle and populated/unknown-state
controls. The retained box file reports estimates of 84932 orders, 20657 fills,
1018 VP, 12 accounts and 15 strategies, with 16145 scanned orders on `live:orb`
and zero routed candidates. Its retained run has cost 1081922.99, JIT 613.875 ms
and execution 665.006 ms; this differs from the earlier reported 743.8 ms run.
Exact representative distributions remain UNKNOWN. No source test,
SQLite result, generated SQL, or arbitrary small fixture establishes those limits.

`ops/sync2x_plan.py` is an UNRUN proof-control script for a main-prepared synthetic
fresh qualification database, not a seeder or production collector. It has no
default DSN; refuses application/remote/default-port databases; uses a local Unix
socket, exact name acknowledgement and read-only transaction; checks effective
JIT off, cardinality and index manifest before raw old/guard/new JSON plans and
independent expected-ID equality. No setting change, schema write, cleanup or
retry is implemented. Each measured plan runs once. A private exclusive JSON
receipt records failures rather than silently replacing a previous attempt.

Main must approve a synthetic dataset and index manifest for each idle/mixed
case. Required manifest fields are `database`, `source_evidence_sha256`,
`dataset_kind=SYNTHETIC_NOT_PRODUCTION_COPY`, `case`, `account=live:orb`,
`distribution_reviewed_by_main=true`, `expected_ids`, `expected_pg_major`, exact
`row_counts`, and sorted `indexes` (`tablename`, `indexname`, `indexdef`). The
reviewed row-count floors come from the estimates, not an exact live copy.
Preserve retained indexes; do not add favorable indexes only for this proof.
Seed old unresolved/held, missing-VP fill, unrouted supersession and NULL timestamp
controls in mixed fixtures, calculating expected IDs separately. The script and
its pure guard tests are authored only, not run or qualified.

## Traffic Diagnostics Plan

Main owns the before/after alias stub count. Physical HTTP attempts must be
counted at a verified transport-send boundary, including failed attempts and
internal retries, not at `list_account_positions` or SDK `get_response` entry.
Cache hits and rejected local query permits count zero HTTP. Do not log account
hashes, native IDs, request URLs, tokens, payloads, or app keys.

TOP20:0x approves the source format: after the original sync account lookup,
one start INFO lists actual logical account names with `physical_http_reads=PENDING`.
No additional DB or provider read obtains that list. The existing end INFO adds
`physical_http_reads=UNKNOWN sdk_invocations=N sdk_scope=webull_delegate_until_end`.
SDK counts are per-context Webull `_EndpointCountingClient.get_response` entries,
including failures, not verified wire sends/internal retries. Thread context is
propagated by existing `asyncio.to_thread`; late detached calls cannot contaminate
another pass because inherited closed frames ignore them. A counter failure
reports SDK count UNKNOWN without replacing the delegate result/exception.

The existing Webull endpoint recorder counts SDK calls, not verified transport
attempts. Local SDK transport source is unavailable in K's environment; a physical
wire hook still needs verification. No physical zero/count is fabricated by the
approved SDK-only diagnostic. Query permits share a host/app-key budget, whereas positions cache
and backoff are keyed by logical account name. Shared vendor account quota does
not imply shared local positions backoff across aliases.
