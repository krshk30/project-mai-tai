# WBQUIET-READER contract, Lane D v1

Consume actual `[WBQUIET-READER] <JSON>` lines, not the immediate SHADOW
`covered_readers` list alone. One SHADOW receipt is emitted per observed pass;
later notes neither re-emit nor mutate it. All IDs below are opaque strings.

## Root fields

```json
{
  "process_pid": 123,
  "reader_sequence": 1,
  "window_seconds": 60,
  "dropped_reader_observations": 0,
  "other_oms_and_external_readers": "PARTIAL",
  "wire_calls": "UNMEASURED",
  "wire_calls_saved": "UNMEASURED",
  "policy_applied": false,
  "readers": []
}
```

`readers` has at most 16 account records. `reader_sequence` is local to the
service observer; use process/account/reader/adapter-read-ID together, not the
sequence alone. Counts are instrumented adapter method invocations, not HTTP pulls.
Canceled/uninstrumented reads, missing lines and observation loss are not zero.

## Reader fields

Every record contains these fields:

```json
{
  "account": "live:webull_1m_v2",
  "reader": "reconcile_tri_state",
  "provider": "webull",
  "reader_provider_scope": "UNMEASURED",
  "webull_cadence_eligible": true,
  "source": "adapter_positions",
  "outcome": "returned_not_wire_proof",
  "generation": "2026-10-08T14:00:00+00:00",
  "generation_basis": "caller_observed_metadata_not_wire_id",
  "adapter_calls": 1,
  "adapter_read_id": "123:1:live:webull_1m_v2",
  "reader_acquisition_generation": "adapter_cache:100.0",
  "same_source_generation": "observed_adapter_cache_identity",
  "periodic_evidence": "committed_generation_observed",
  "overlapping_periodic_passes": []
}
```

`provider` is already-constructed account routing or UNMEASURED, not inferred from
an account-name prefix. ORB admission/close has `reader_provider_scope=schwab` and
`webull_cadence_eligible=false` even if routing is wrong. Exclude it from Webull
cadence analysis; retain its actual provider/account as a separate diagnostic.

The five reader names are `reconcile_tri_state`, `exit_snapshot_refresh`,
`reserve1_bracket_note_renewal`, `orb_admission_positions`, `orb_close_positions`.
Position readers use source `adapter_positions`, outcome `returned_not_wire_proof`
or `unreadable`, and `adapter_calls=1`. Their `generation` is the matching account/
symbol snapshot's observed `as_of`, or UNMEASURED. Renewal uses source
`exact_entry_bracket_confirmation`, outcome `renewed`, `adapter_calls=0`, null
`adapter_read_id`, and an opaque managed-row/parent/renewal-timestamp generation.
Renewal is not counted as a position pull; its existing broker calls are not
counted or claimed as saved by these position-reader records.

## Committed-generation join

Each element of `overlapping_periodic_passes` contains:

```json
{
  "process_pid": 123,
  "pass_id": 71,
  "account": "live:webull_1m_v2",
  "local_read_id": "123:71:live:webull_1m_v2:1",
  "acquisition_generation": "adapter_cache:100.0",
  "generation_basis": "matched_immutable_cache_objects_not_wire_or_broker_id",
  "adapter_calls": 1,
  "acquired_elapsed_seconds": 0.01,
  "committed_elapsed_seconds": 0.02,
  "elapsed_seconds": 59.0,
  "shadow_receipt_emitted": true
}
```

Join to SHADOW with exact process PID, pass ID, account, and the SHADOW account's
`committed_generation.acquisition_generation`/`local_read_id`. The local read ID
identifies a returned invocation; it is not a source-acquisition ID. The acquisition
stamp is observed only under a nonblocking cache lock when the nonempty returned
immutable snapshot objects match that account's stored cache objects by identity.
Empty/equal-valued copies, missing cache and contested locks remain UNMEASURED.
No extra SQL/HTTP, adapter instantiation, cache mutation or wire-ID inference occurs.

`elapsed_seconds` is from the observed persistence transaction return, not pass
start or snapshot `as_of`. Include 0 through 60 seconds, expire >60. Registry is
bounded to 16 accounts x 16 generations. Multiple overlapping passes are retained;
do not silently pick a nearest pass or call overlap proof of reuse.

`same_source_generation=observed_adapter_cache_identity` requires a measured
reader acquisition matching a committed acquisition, successful SHADOW publication,
and no reported reader loss. Otherwise it is UNMEASURED. `periodic_evidence` is
UNMEASURED when anchors are missing, unpublished, lost, or acquisition is unknown.
Missing downstream/external readers remain PARTIAL; these fields establish neither
trading-decision equivalence nor measured wire savings nor install permission.

## Controls and evidence limits

The isolated reader tests exercise all five real methods, transaction failure,
ContextVar reset, inclusive 60-second expiry, account/provider mismatch, same-object
vs empty/equal-cache data, one immutable SHADOW receipt, bounded memory, lock loss,
logger failure and unchanged adapter call counts. Nineteen mutation controls remove
reader/commit guards or falsify those guarantees. Use final `mutations/summary.json`
and raw logs, not an earlier run with a surviving loss-control mutant.

`REPLAY_2026-10-08.json` is a synthetic real-method/mock trace; it is NOT production
or historical broker data. Actual Friday lines must be collected separately under
the parent's read-only data authorization. Agent E owns that data report/parser.
