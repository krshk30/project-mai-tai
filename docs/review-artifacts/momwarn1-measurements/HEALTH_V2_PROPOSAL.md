# G3 health v2 proposal, NOT READY, for MAIN/G1 schema review

Source: main's Oct12 read-only verified actual proc receipt:
MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS=15. No credential/proc-environ probe
by G3. b680 Settings default5 is NOT the live configuration. Source control
loop oms/service.py:1145 uses that configured interval; :11235 dynamically
shortens it when stop guards exist. This is a configured upper cadence, not
a claim that every pass occurs exactly15s apart. User approved grace15+5s.
Original completion timestamp and monotonic duration stay immutable.

Current health v1 prototype is NOT READY: requiring a completed duration <=5s
old would latch A off normally between15s passes. The green offline v1 tests
do NOT prove operational readiness. That rule is superseded by this proposal.
Monitor freshness <=5s remains distinct from completed-source age <=cadence+5.
A fresh monitor must actually read bounded log/proc evidence; reading an old
external JSON receipt and changing its clock is not acceptable.

V2 source: anchored_session_poll_open in schwab_v2_rest_client.py:49 starts07:00
ET on weekdays; main cites prior recorded card first same-day Schwab bars>=07:00
and measured empty histories before07. G3 has not recovered that card's exact
receipt reference; MAIN must supply it, not fabricate a provenance identifier.
That sourced expectation is stronger than observing a missing bar. The generic
rest-client bar loop also exists, so the predicate must explicitly bind the
active anchored/current-day lane, not apply to arbitrary data sources.
V2-MACD-PROBE is conditional on a probe set and does not prove all watched bars.
Heartbeat secs_since_last_bar includes warmup; isolated state watchlist can prove
an explicitly empty watchlist, but its payload has no full completed-bar due map.
Applicable post07 lateness remains unavailable without a source-proven adapter.
No OMS/v2/ORB trading source changes are authorized in G3.

## Proposed exact health envelope

schema_version=2; observed_wall_ns = fresh actual monitor sample time;
oms_sync_seq,oms_sync_seconds,oms_measured_wall_ns = original completed receipt;
oms_source_age_seconds = (observed_wall_ns - oms_measured_wall_ns)/1e9;
oms_sync_interval_seconds = verified live configured15;
oms_pass_allowance_seconds=5; oms_cadence_source = retained live-setting reference;
oms_monitor_coverage=complete; oms_source=OMS-BROKER-SYNC-PASS.phase=end;
oms_instance_id = PID+process-starttime binding, not a guessed reused PID.

v2_applicability=APPLICABLE/NOT_APPLICABLE/UNKNOWN;
v2_applicability_reason, v2_expectation_ref, v2_instance_id, v2_measured_wall_ns;
APPLICABLE requires complete_current_due measured coverage and actual
v2_due_wall_ns/v2_completed_wall_ns, with real v2_bar_lateness_seconds.
NOT_APPLICABLE requires an explicit sourced predicate: active anchored lane,
validated recorded expectation reference and ET<07:00, or a fresh complete
empty-watchlist snapshot. Lateness remains null, NOT a fake zero. UNKNOWN includes
unreadable/missing/partial sources and is never promoted from absent bars.
The empty-watchlist proof proposal allows <=10s source age, from existing v2
STATE_PUBLISH_INTERVAL_SECONDS=5 plus bounded5 allowance; monitor5s is separate.
This source bound, like the whole healthv2 schema, still requires G1 review.
Payload retains oms_instance_id/source/coverage and v2 applicability provenance
(recorded reference/lane/start time or original complete watchlist/snapshot stamp),
not only a reason string. APPLICABLE retains source/instance/coverage and original
due/completed timestamps; no scalar-only claim of provenance.

G1 owns version validation, monitor/source-age checks, all applicable late-bar
checks, skipped/regressed sequence checks and two slow completed passes. G3 will
not modify G1 reader or thresholds. Multiple completed IDs between1Hz samples
must not hide intermediate durations: initially fail closed on any unrepresented
gap; a bounded completion batch would need separate G1-reviewed contract.

## Narrow local build plan and readiness boundary

Add a real bounded off-loop OMS log reader with explicit verified source timezone,
PID/starttime, original end-line timestamp and measured duration. EOF coverage,
rotation/truncation/read/backlog failure must be explicit UNKNOWN. No DB/HTTP,
credentials, provider, quote path or new connection. The live cadence and receipt
reference are explicit reviewed configuration, not guessed defaults.
Test normal15 cadence at6/15/20s vs stale>20, unreadable source, completion stamp
unchanged while monitor advances, sequence/instance fences and sourced N/A vs
unknown. Keep the publisher inactive and schema2 NOT READY for G1v1 until review.
If no actual applicable v2 source is established, report that dependency rather
than calling an external JSON placeholder end-to-end complete.

MOMWARN1-G3-HEALTH status: operational BLOCKED, contract review pending.
MOMWARN1-W4 actual Oct13 receipt: NOT DONE; no live file/service/timer started.

Local candidate implements independent proposed_health_v2 and OmsLogMonitor.
Controlled normal15/cadence+5, stale monitor/source, sourced N/A vs unreadable,
applicable late-bar retention, actual log duration/original stamp and PID binding
controls pass. No G1 reader wiring/change. V1 health CLI deliberately refuses
launch so the rejected age rule cannot be installed by this entry point.
