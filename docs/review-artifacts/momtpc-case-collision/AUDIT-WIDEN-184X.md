# W3 18:4x Corrective Audit Build

The reviewer explicitly supersedes the old exact-two-TPC-only repair scope.
The legacy `case_collision_tool` remains unchanged and tested, but it cannot
represent this widened install. Use new file-only `case_collision_widen_tool`
only for local DRYRUN / private in-memory SQLite COPY, never a live repair.

## Exact Scope And Evidence

Only Oct13 TPC/BCPC ordinary momentum30/60 DETECTED, NO_FILL, FILLED, EXITED,
FINAL and UNANSWERABLE events are candidates. Reference or prior close must
be strictly below half a retained detect/fill price. Exact same logical ID,
strategy, symbol and date bind sibling evidence for sparse FILLED payloads;
the exact saved SESSION_READY is a fallback only when no trade-specific prior
close is retained. A good trade-specific cache is never replaced by a bad saved
cache during assessment. Missing evidence stays UNKNOWN, not automatically bad.
Raw SIP cause remains UNMEASURED; the approved ratio is the invalidation rule.

Manifest requires the full counted population, every proven exact target's
UUID/event key/logical ID/strategy/symbol/type/payload hash and symbol-specific
reason `case_collision_TpC` / `case_collision_BCpC`, reviewer `claude-1` and
review ID. No blanket ticker invalidation and no silently omitted eligible IDs.
CLI capture explicitly supplies row_count and truncated=false, at most20000
rows/16MiB per input; duplicate/capped/count-mismatched captures refuse.

Plan appends one INVALIDATED per bound target, never updates/deletes originals.
Read-side exclusions are bound to logical ID + strategy + symbol + session,
so a reused logical ID outside that scope remains visible. Warning strategies
and later ratio-good common trades remain intact. Original preimages and record
hashes guard idempotent COPY replay; no production DSN/execute option exists.

## Prior-Close Rebuild Is Separate

The old corrupted SESSION_READY TPC15.62/BCPC22.29 is not a source of corrected
daily closes. Intraday detect/fill/reference prices cannot stand in for Oct12
prior closes. No Massive request is authorized or performed.

An optional retained reference file must bind BOTH exact common keys to native
event `prior_close` fields, source `momentum_paper_events:prior_close`, dateOct12,
UUID/key/payload hash/value and the saved-session preimage. Wrong field, symbol,
date, hash, missing key, unchanged bad cache or still-subhalf value refuses.
Only then does the plan add SESSION_REFERENCE_CORRECTED and restore its audited
read overlay. Without that authority, an audit-only plan can be rehearsed but
reference_state explicitly remains BLOCKED_MISSING_EXACT_TPC_AND_BCPC_PRIOR_CLOSE;
this is NOT a full-install-ready packet or a repaired cache.

Main's complete native TPC/BCPC export and actual corrected Oct12 close authority
are still needed. The initial11-row export is an incomplete widened population.
Controlled test prices/UUIDs are not native repair bindings. Main's failed timing
packets stay retained; ambiguous comparison requires reviewer judgment, not an
install waiver. Gateway+strategy+paper+control is the approved W3 runner scope;
C's v2 guards compose into W2's one restart. All actions remain main-reviewed.
