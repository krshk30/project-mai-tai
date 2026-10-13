# MOMTPC Case Collision: Local T Component

Base: `126a949489bf7cb78b229e3aae7fc39952dd25af` (approved Q lineage).
Worktree: `/Users/velkris/.codex/worktrees/momtpc-case-t-boyle-20261013/project-mai-tai`.
Evidence: `/Users/velkris/.codex/task-queues/analysis/momtpc-case-t-boyle-20261013/`.

## Source Scope

Four paper runtime files only. Reject nonexact uppercase tickers before parsing,
history/path mutation, subscription routing or price/key normalization. Prior
closes retain exact keys, including restored session inputs. Snapshot and trade
reference pools require exact symbol equality. Snapshot metrics select the exact
common source. Warning raw inputs/windows no longer uppercase preferred markers.
The existing aggregate contract already rejects mixed case and remains unchanged.
Detection thresholds, daily windows, conditions, cap, fill/exit chronology,
subscription policy, ledgers and store/schema are unchanged. No Q/provider,
scanner, OMS, H/N/P, or page runtime file changed.

## Focused Proof

Original 29 new controls on base: 22 RED / 7 PASS (`before.xml`). Final suite:
187 PASS / 0 FAIL / 0 ERROR / 0 SKIP in 2.32s (`final-focused.xml`), comprising
33 new controls and 154 paper-engine/service/Option A/warning neighbors.
Initial after receipt: 29 PASS; intermediate neighbors: 183 PASS. These overlap,
not additive totals. Ruff and diff-check pass. No performance run or broad suite.

TPC fixture uses reviewer values TpC15.73/previous15.62 versus
TPC82.77/previous83.49. Alternating snapshots generate zero false detections;
mixed-case trades cannot enter the common reference pool or fill a common path.
Both prior-close insertion orders preserve common83.49. Common82.77 to99.324
(exact +20%) still detects both 30s/60s strategies. BCpC/BCPC identity controls
use explicitly synthetic16/80 and15/81 prices, not claimed captured market data.
Aggregate mixed-case rejection, malformed-price prefilter, exact metrics,
restored reentry identity, and misbound historical reference defenses are covered.

## Read-Only Sweep and Gaps

The source table and line provenance are in the external `CONSUMER-SCOPE.tsv`.
Snapshot provider/payload/gateway pass-through and reference cache preserve raw
case (not merging at those points). Scanner five-pillars/top-gainers and strategy
runner snapshot ingest uppercase raw tickers without a prior exact-case fence:
affected, no changes in this lane. Momentum alerts live snapshot history preserves
case, but its restored history uppercases: restore path affected.

Important: MassiveTradeStream._handle_messages uppercases raw A/T symbols before
warning/watched dispatch. Thus the gateway A wildcard may relabel TpC asTPC
before the paper aggregate contract sees it; subscribed TPC trades have the same
raw-stream risk. Downstream guards cannot reconstruct the lost original case.
Provider/gateway scope was read-only: no modification, live probe or silent fix.
This packet is NOT an end-to-end phantom-elimination claim for those routes.

Already-persisted uppercase prior-close keys may contain the earlier merged price;
their original ticker provenance cannot be recovered by this guard. Session
restore preserves the recorded value, filters still-mixed keys, and does not fetch
fresh reference data or rewrite durable state. Source guard is not data repair.

## Main Gates

Main owns queue/status/review/composition/install. Q does not wait for this new T
component unless green and reviewed. After a reviewed install, main alone audits
and invalidates exactly two phantom TPC paper trades via the approved path,
retaining records rather than deleting them. No event retirement was implemented
or executed here; identities/current counts were not freshly queried by this worker.
Upstream source risks require separate main scope/ownership decision. No provider
API/socket/production action, database write, benchmark, or queue/status edit.
