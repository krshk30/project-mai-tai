# PRICE1 Census V3 Clock / Existing-Capture Contract

SOURCE ONLY; parent cafbc5d9db21c9e37e6ec119581dba10a65bf284 at15:28:33 ET
preserved. Main quiet window obeyed; resumed only after15:33:42 release. No
actual census data parsed, no tests/import/AST/compile/replay/provider/DB/SSH.
Production src diff versus529 is EMPTY. Source schemas/producer calls were read.

## Why V3

V2 correctly pinned inputs/config/minutes, but its fixed result scope said
retained-delivery even if an exporter supplied historical event-time projections.
Missing historic local-arrival data must not force abandoning a useful DECLARED
counterfactual study; neither can such a study be called native/as-known evidence.
V3 requires explicit clock provenance and does not generate any clock/window.
No data-dependent tuning occurred; actual10-12 census results remain UNKNOWN.

Header contract PRICE1-CANDIDATES-v3 / result PRICE1-CENSUS-RESULT-v3:

- clock_basis.kind=retained_consumer_delivery requires receipt_provenance and
  observation_provenance, and initial_state=empty_before_session_prepare from
  actual prefix evidence. These are DECLARED until main verifies original inputs.
- clock_basis.kind=declared_counterfactual requires contract_id, assignment_rule,
  delivery_scope; permits explicitly declared_cold_start. Output says NOT
  native/local-arrival/as-known. Complete-for-study input is labelled
  DECLARED_COUNTERFACTUAL_INPUT_COMPLETE, never native delivery completeness.
- Every row still supplies its explicit observed_ns and original/projected A/T
  clocks under that declared contract. No SIP-to-receipt substitution, new API
  capture, invented ARM/fill/ACK, assumed observation delay or chosen window.
- Compare requires identical clock contract, initial state, source config, study
  bounds and input SHA. UNKNOWN/missing-minute/cap pressure reporting remains.

Existingcaf census40 source controls preserved; 10 clock classification controls,
2 comparison drift variants and2 manifest-capacity controls added =54 intended
census controls, ALL UNRUN. The manifest has a separate2MiB cap for all prior
closes; ordinary event lines stay <=256KiB. The original event-oversize assertion
is retained with its fixture explicitly in the event position, not the manifest.
Original61 entry controls remain, total115 intended across4 own modules,
collection unverified. This does not borrow main's L13 functional receipt.

## Finite Existing Export Alternatives / Exact Missing Questions

Main intake still records actual10-12 warning prefix/config MISSING. Bounded
filename inventory found no specifically named PRICE1/MOMNEG census packet;
it does not prove absence of every differently named archive. No raw tape read.

1. Existing original delivery: provide the A/T prefix/config/receive checkpoints
   described in the primary handoff, including session/restart boundaries and
   persisted reentry boundaries if a restart occurred. Current native-prefix mode
   supports a fresh empty session; it does NOT silently simulate consumer restarts
   or import live RAM. Intermediate restart replay remains unsupported/UNKNOWN.
2. If only historical A/T exists: main/reviewer must declare the bounded study
   window, observation/receipt assignment, same cold-start/prefix rule and T
   delivery/legacy-active scope BEFORE comparison. V3 can consume that normalized
   study, labelled counterfactual. Worker has not selected a delay/order/seed.
   Incomplete counterfactual T coverage remains UNKNOWN, not assumed no-fill.
3. Existing baseline samples can independently provide observed owner counts,
   even without the whole delivery tape. Exact inspected producer is
   services/momentum_paper_app.py::_warning_metrics and its consumer_sample call:
   stream momentum-warning-metrics, JSON version1/event_type=consumer_sample,
   source=momentum-paper, observed_ns, owned_warning_symbols,
   shared_owned_symbols, active_warning_paths, gateway_instance_id/generation,
   coverage/transport_start_ms/transport_through_ms/status. Samples are scheduled
   >=10 seconds apart; no completeness proof from a missing sample.

Important: owned_warning_symbols is len(desired_symbols(set())), NOT simply
len(candidates). It includes active warning symbols. active_warning_paths counts
logical events, not distinct names; do not subtract it to invent candidate count.
Baseline sample maxima must be called OBSERVED OWNER SAMPLE MAXIMA. They cannot
provide the new counterfactual, exact full-minute maxima, historic arrivals, or
unobserved zeroes. No such extractor was executed or baseline count claimed.

Exact request to main/Claude: identify any retained warning-metrics sample prefix
and/or normalized A/T archive/config for10-12, with producer hashes and coverage.
If no receive clocks survive, explicitly choose a DECLARED historical study
contract, not a fabricated native replay. If no data survives, record UNKNOWN.
This is a finite existing-input request, not permission for a worker connection.

## Readiness / Impact

Only tools/price1_candidate_counts.py, tools/price1_candidate_compare.py,
tests/unit/test_price1_candidate_census.py and own handoff/docs change. CLI flags
and pinned baseline/current roots remain the V2 proposal; input JSONL header now
requiresV3 clock_basis. Main's exact head/root/sha preflight remains mandatory.
Imported source path/hash is not a Git attestation or proof against concurrent
source mutation; use frozen roots. No install/GO/actual census claim.

Main-only module tests/unit/test_price1_candidate_census.py and original3 PRICE1
modules remain UNRUN by author. Cost stays two bounded streaming input passes,
<=256KiB lines/<=1440 minute rows plus configured model bounds. Actual cost,
P1 original inputs, P2, ALL-ON/native transport and next-day boot remain owed.
