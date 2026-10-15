# BKHA Added To Proof G

Tests-only followup to add1a2e8/f9. Original LGHL tests, packet and manifest preserved.
Main owns runtime/composition/install/performance; no source gates changed here.

## Observed Versus Controlled

Reviewer reports native normal-paper M60 detection16:19:13.591 with reference14.01,
fill18.15 at16:19:20.202, STOP15.17 at16:21:13. That report is not a newly captured
native row in this worker packet. Full SIP tape, event IDs, conditions, participant
timestamps, observed receipt/decision seals, owner ACK and A frames are UNRETAINED.

Existing /tmp/claude1-bkha.py was inspected/captured as text only. It requests live
provider trades and prints a simplified any-print/round-lot path but saves no full
response. NEVER executed here. Its naive round-lot scan compares SIP nanoseconds;
it does not implement the engine's millisecond fill fence, condition catalog,
same-second target/STOP priority, receipt/decision seal or candidate admission.
Thus that calculation is not the production A-on paper replay.

## Rules Verified On Current F9 Source

| Proposed Path | Rule / Result |
| --- | --- |
|Detect16.87 at13.591ms, next17.71 later NS in SAME millisecond|NO FILL: sip_ts_ms must exceed max(detection_ms, observed_decision_ns//1e6)|
|17.71 at a later millisecond with zero controlled receive delay|Can fill if condition-eligible; alternate timestamp is synthetic, not native|
|17.71 fill TARGET|Exact threshold18.5955; display18.60 is rounded, not the engine predicate|
|18.15 fill TARGET|Exact threshold19.0575;18.87 cannot reach it even if eligible|
|1-share18.87/19.50 with conditions12+37|Not exit-eligible:37 Odd Lot Trade does not update consolidated OHLC|
|1-share18.87/19.50 with conditions12 only|Exit-eligible; size1 itself has NO additional fill/exit minimum|
|Pending TARGET|Deferred through its second; an eligible STOP in that same second can win|
|Native reported18.15 ->15.17|STOP threshold15.4275; price change -16.4187328%, consistent with reported -16.4%|

Exact native conditions on the one-share prints are UNKNOWN. Do not infer37 solely
from size1, or claim TARGET from the high19.50 alone. Eligibility, order, observed
decision time, later eligible callbacks and same-second STOP precedence all matter.
The same-ms17.71 fill must not be asserted without verifying native precision and
the real decision seal. No runtime fix/waiver to that fence is authorized or made.

## Local Controls / Limits

`test_momlatch1_bkha_proof.py`:16 controls plus a native-input blocker test. Actual
warning consumer/whole-bucket reference -> bounded candidate -> controlled owner
application -> raw M60 detection -> fill/exit engine. M30 does not detect the burst.
The pre-admission15.43 bucket, prior-close10, owner application/zero receive delay,
IDs, missing sizes/conditions and sub-millisecond ordering are SYNTHETIC, explicitly
not a reconstruction of missing native tape. Causal traces retained in JUnit.

Controls cover same-ms fence; eight fill/exit/condition combinations; six actual
raw-normalizer size/condition cases; reported price-path STOP with declared odd-lot
conditions. No fill on detection print and no imaginary later eligible target print.
Native-input test skips when no full tape is supplied; supplying one demands source
provenance/full replay review rather than silently treating file presence as PASS.

LGHL remains: sparse CONTROLLED M60 raw3.98 detection/M30none, native full tape
SKIPPED. G native acceptance is BLOCKED for BOTH cases. Need retained raw responses
with full interval/pagination, original SIP NS/conditions/IDs, current native session
catalog/prior close, A/subscription and observed-receipt provenance. With those,
replay exact source and report detection/fill/exit IDs or the first violated gate.

No provider/API/socket/live DB writes, production action, benchmarks or sustained
timing tests. New tests and documentation only; existing source/controls preserved.
