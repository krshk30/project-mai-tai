# WEBULL429 released build source assessment

[codex] Sole writer of `codex/webull429-list-primary-plan`. KEEPREST remains
stopped at its own evidence commit; no shared handoff edits.

## Accepted Evidence and Scope

Read the full STEP0 and all nine records in its raw receipt companion before
implementation. Companion SHA256 independently matches
`ddfdb153714a02798dc14811ae1532e688ea3d94700254c950e5f304c68955ce`.
These are preserved historical captures, not new production reads or a claim
that I re-executed their production queries in this build.

Own source assessment at branch publication base
`2db8eb1f08d2bdce871010820e635a22571bdd08` confirms ordinary entry and OCO-child
reads call detail first and only fall back to list-today on429. The old list
cache permits up to20 HTTP pages within one logical scan and stamps the whole
result with its completion time. Strict RPG BUY readback bypasses that fallback;
EOD reads both children, and ordinary OCO execution returns on the first valid
FILLED child. Position reads have a separate10s throttle and5s/60s backoff.

AGREE with the accepted read-pressure cause and list-primary direction.
The withdrawn cancel429 diagnosis stays withdrawn: retained cancellation errors
are417, not429. Cancellation requests, retries, acknowledgements, and interval
settings are outside this build. The legacy query quota remains UNMEASURED.

## Implementation Contract

- `MAI_TAI_WEBULL_LIST_PRIMARY_READS_ENABLED` defaults ON in code; deploy ON is
  the expected configuration. OFF selects the previous detail-first/cache
  behavior for rollback. No production flag or environment was changed.
- Ordinary reads use exact real-account/client identities. Existing durable
  owned base IDs derive M/T/S; no symbol/prefix/position adoption. Missing or
  malformed matches remain UNKNOWN; OCO missing-child reads raise UNKNOWN.
- Each page consumes a shared app-key/endpoint rolling permit. The conservative
  ceiling is2 HTTP attempts per2s, including failed attempts. Ordinary optional
  detail gets at most1 of those permits, reserving the other for strict proof.
  Strict reads never wait for permits and never consume ordinary cached proof.
  This policy is process-local, not a distributed app-key quota guarantee, and
  is not a measured Webull legacy quota.
- Real-account scans coalesce across aliases, threads, and adapter instances.
  At most2 list page attempts per account/scheduler cycle, with fair continuation
  instead of restarting on2s cache expiry.20 pages remain the logical cap.
  Every row retains its own page acquisition timestamp;2s freshness applies
  to positive reads. Incomplete scans never certify absence.
- Page20 still advertising continuation is truncated. Empty continuation,
  repeated cursor, malformed rows, foreign account, conflicts, and HTTP errors
  fail closed. Known positive rows do not acquire new timestamps when served.
- Terminal evidence versions bind normalized execution status, real account,
  client ID, broker ID, cumulative fill quantity/price, and broker fill time.
  One concurrent optional confirmation per version;429 or budget denial leaves
  a valid fresh list execution usable and does not complete a forever flag.
- Successful exact detail proof is committed as an immutable deterministic-ID
  `webull_terminal_read_proof` row in existing `dashboard_snapshots`. No migration
  is required. Runtime OMS construction wires the durable repository. A crash
  before proof commit permits re-read; after commit reuses that proof. Existing
  broker fill IDs and OMS fill attribution remain idempotent.
- A partial execution stays nonterminal, preserves cumulative fill accounting,
  and does not spend a terminal-confirmation detail call. Conflicting terminal
  detail or unusable quantity/price/time remains UNKNOWN. Conflicting aliases
  for quantity, price, status, and fill time are rejected; equivalent formats
  remain usable. List-endpoint NOT_FOUND never proves readable/absent children,
  and an unusable native FILLED decoder result remains UNKNOWN.
- EOD still freshly confirms both children; strict RPG still uses its exact
  fresh BUY-only detail decoder. AIFA's ordinary failure and both recorded OLOX
  strict failures are represented with their actual IDs and synthetic controlled
  response variants. No claim of live repair follows from these replay controls.

## Detection-Latency Limit

With the captured live15s sync, one account and ideal instantaneous pages, a
20-page scan spread two pages per cycle reaches page20 at135s after its start.
Other active accounts and network time can extend this. This is a deterministic
test model, not production after latency. A full logical scan is not one HTTP
request and is not promised complete in one15s cycle. Missing prior-day GTC
orders stay UNKNOWN. Scheduler15s, native minimum30s, and position10s/backoff
settings remain unchanged; this lane does not slow those timers.

## Verification

Focused adapter, RPG, configuration catalog, PM composition, and protection
guards:411 passed. Local SQLite roundtrip plus replay/backtest:71 passed,
one expected failure. Ruff and marker isolation pass. Fourteen process-local source
mutations are killed. Full Mac unit pair against authorized current main4805:
main6469 passed/56 failed; head6521 passed/56 failed. Failure names match exactly,
zero new failures. The prior c21 pair is superseded by this post-rebase pair.
The measured baseline is56, not an assumed47. `VERIFICATION.md` records commands,
hashes, the corrected inventory assertion, and the pinned main's green Ubuntu
CI receipt. Final exact-head push and PR Validate results must both be green
before a ready claim; their URLs and exact SHA will be published on the PR.
Controlled ALL-ON nine-flag composition plus this lane's read flag passes the
effective-zero audit:152/152 checked,0 mismatches,0 unknown. Public WBPOWER1
budget/cache/detail boundaries are released in `INTEGRATION_CONTRACT.md`.
No production DB/Redis writes, broker
order calls, service actions, installation, or merge was performed.
