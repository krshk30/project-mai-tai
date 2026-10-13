# MOMLATCH1 Boyle Tests-Only Handoff

Base f9b71cc0. No runtime files changed. Main owns gateway/provider/watchdog/paper,
Tesla owns policy, Popper owns metrics/page/incidents. These tests do not implement
the production latch or change scanner/strategy/TPC inputs.

## D/E Contract

`test_momlatch1_transport_proof.py` expects the existing monitor entry point
`await service._warning_monitor_once()` and real provider `set_momentum_warning_aggregates`.
After a proved ready read, a controlled PermissionError must unsubscribe A only,
leave `_warning_revert_reason is None`, and allow resume only on the third subsequent
clean read. Published sequence remains increasing across the cycle, without repeated
A SUB/UNSUB on clean/noise polls. Watched T/Q owners remain intact.

The direct monitor test is intentionally RED on old f9 sticky behavior. It must
pass on main's composed runtime before this is called integration acceptance.
Policy component interface: `WarningPausePolicy(clock_ns=...)`, `trip(reason)->
pause|sticky`, `clean()->bool`, `snapshot()`. The test-only action adapter exercises
that API with the actual offline provider/publisher; it is NOT production mechanics.
`MOMLATCH_POLICY_SOURCE` can point to an independently owned policy file for component
controls before composition. Hash it before/after each run; mutable-source component
PASS is not a frozen-source integration PASS.

`MOMWARN_RETAINED_SNAPSHOT` selects the retained full event JSON. Functional equality
checks every snapshot/reference payload field,13269/3184 cardinality and stream
options, with no paced traffic, sustained run or stall measurement. Existing snapshot
offloop, T/Q owner/pipeline and scanner tests remain unedited. Main owns all timing.

## G LGHL Limits

`test_momlatch1_lghl_proof.py` buckets eligible SIP rows at each exclusive second END,
then invokes the actual warning candidate/consumer/engine, admits a bounded owner
at an explicitly CONTROLLED time, and feeds prints only after that owner application.
No future bucket low, fabricated ACK, fill on detection print or historical latency.
It records the complete model causal trace/reference bounds in JUnit properties.

Sparse reviewer points are CONTROLLED only. The3.70 admission point, row IDs, sizes
not supplied by the reviewer, prior-close3.00 and zero owner/aggregate delay are
synthetic. Result M60 detects on3.98 at16:02:18; M30 no detection; no subsequent
fillable print => no fill invented. A40s owner-delay control cannot backdate the
raw first signal. This does not prove the full recorded LGHL case.

`MOMLATCH_LGHL_RETAINED` enables the native-input model test ONLY when main supplies
a local packet with symbol LGHL, session_date2026-10-13, evidence_kind
RETAINED_NATIVE_TRADES, request_start_ns<=16:01ET, request_end_ns>=16:03ET,
pagination_complete=true, full `rows`, actual `condition_snapshot`, and verified
`prior_close`. Rows carry original SIP nanoseconds/price/size/conditions/trade IDs.
The test asserts first eligible>=3.96 is the M60 raw detection and M30 has none.
Packet acquisition/path/hash/catalog/reference provenance must also be reviewed;
an assertion in a supplied JSON is not independent completeness evidence. Derived
A buckets remain MODEL, even with full retained prints, not recorded native WS A.

Existing retained condition catalog from Oct12 names12 Form T/Extended Hours, with
consolidated high/low and open/close=false. Current `conditions.py` explicitly treats
12 as a neutral session marker, eligible after16 while unknown companions still
refuse. Oct13 current native condition snapshot has not been captured by this worker.

No live-key REST/socket/API, DB/write, install, benchmark or queue/status action.
T/P remain at their explicit preemption checkpoints; T1781/d276 not installed.
