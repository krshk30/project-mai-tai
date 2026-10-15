# MOMWARN1 G3 NOT READY assessment; metrics v1, health v2 PROPOSED

Owner: Euler, G3 only. Branch codex/momwarn1-measurements-20261012.
Base: b680c2aff50150ea6130b19ccf0db9e05dfb680d. UI attachment failed
at the app's 100-identity limit; the checkout exists and is clean.
Source: human G3 assignment and v1 contract Oct12; later human W4 answers.
Main owns the durable queue; this worker does not edit it.

## Intake before implementation

| Stable ID | Priority/owner | Status | Next action / completion criterion |
| --- | --- | --- | --- |
| MOMWARN1-G3-CONTRACT | 2/G3 via MAIN | assessment reported, acceptance BLOCKED | G1 v1 read; final G2 producer mapping still needs coordination and exact-head review. |
| MOMWARN1-G3-COLLECTOR | 2/G3 | done local, not install-ready | Bounded injected-client read-only intake, offline replay, ten-second CSV and per-stock comparison; no new connection or live launch. |
| MOMWARN1-G3-CONTROLS | 2/G3 | done local, reported | 84 new controls +120 unchanged neighbors PASS; operational sources/future receipts remain separate. |
| MOMWARN1-G3-HEALTH | 2/G3 | v1 prototype rejected by main; v2 policy/source local, operational BLOCKED | Coordinate health v2 with G1; actual applicable v2 source and source expectation reference still missing; CLI launch refused. |
| MOMWARN1-W4-ACTUAL | 2/G3/main | NOT DONE, future | Actual Oct13 04:11-09:30 ET receipts, independently reviewed; no automatic execution scheduled. |
| MOMWARN1-G3-HEALTH-ATTRIBUTION | 2/G3 | docs corrected, reported | Latest human correction: main proposed15+5 for assessment, no explicit human approval; docs only, NOT READY/source gaps retained. |

B a8444946 remains untouched; original B 68.4ms and MNDR 20s causes UNKNOWN.
No authority here over G1 gateway/settings/watchdog, G2 consumer/engine, F mirror,
paper display, broker actions, queue, production or GitHub.

## Source-backed assessment

G1 draft publisher uses `<prefix>:momentum-warning-bars` and
`<prefix>:momentum-warning-metrics`, each Redis entry `data` containing JSON.
Gateway metrics expose A_received/A_published/A_rate_bins, queue_drops, lag_ms,
owned_warning_symbols, warning_trade_coverage, A_subscription_requested_ns,
A_first_provider_event_ns, A_subscription_confirmed (currently null),
coverage_status, watchdog_revert_reason and watchdog_revert_ns.
Rate bins contain observed seconds only: omitted seconds are NOT zero bins.
Mean rate uses a same-epoch counter delta over the actual observation interval;
p95/peak are labelled observed-bin statistics with explicit bin coverage.

Separate A queues alone do not establish callback fairness. Bounded normalization
chunks/yields and a separate A writer belong to G1; G3 cannot prove them from a
fake collector. G1 draft health reader expects <=5s freshness, whereas G3 samples
once/10s starting 04:11, after the proposed 04:00 A start. Main/G1 must identify
the independent health producer. G3 neither writes that file nor implements a
second watchdog. W3 proof is G1's actual `revert` receipt, not a G3 threshold call.

## Exact proposed worker contract (coordinate via MAIN)

Bars v1 (G1 owns): version=1,event_type=aggregate,symbol,start_ms,end_ms,
open,high,low,close,volume,received_utc_ns,published_utc_ns,
gateway_instance_id,generation,sequence,dropped_total. No invented SIP_low_at.
G3 does not consume this per-quote/bar stream in its sampler.

Metrics v1 common fields: version=1,source,event_type,observed_ns (UTC epoch ns),
gateway_instance_id,generation; coverage_status/coverage and dropped_total are
preserved, absent evidence stays UNKNOWN. Types: gateway_sample,consumer_sample,
decision,snapshot_entry,revert. G3's offline envelope is
`{stream_id,read_ns,data:<JSON object or JSON string>}`; `read_ns` is collector UTC.
Live intake accepts an already-owned injected client and reads ONLY the metrics
stream with bounded, nonblocking XREAD. No client factory, provider access,
subscription ACK, control write, DB, service loop or automation is introduced.

G1 gateway_sample fields are those above. G2 consumer_sample proposed fields:
warning_count,subscribed_count,coverage_status,dropped_total. Subscriber counts
must describe their actual status, not an inferred provider ACK. Samples join
only same gateway instance + generation, age 0..20s; future/skewed/stale times
are rejected. Missing fields are not substituted with zero.

G2 decision/snapshot_entry proposed fields (forthcoming producer contract):
symbol,window_seconds (30/60),ledger,lineage_id,status,cap_status,
observeddecision_ns,warning_obs_ns,signal_basis (raw_exact/bucket_sufficient),
signal_sip_ns (only a real trade), or signal_sip_start_ns/signal_sip_end_ns for
whole-bucket evidence; entry_ns,entry_price,outcome,exit_ns,exit_price,pnl.
Updates retain their real observed_ns; they do not backdate a decision/fill.
Optional comparison_id explicitly links warning and Snapshot episodes. Without
it, both actual rows remain visible but unpaired; symbol/time proximity is not
enough. Warning ledgers: momentum_30s_warn/momentum_60s_warn. Snapshot ledgers:
momentum_30s/momentum_60s, pending G2 confirmation. Never sum their totals.
Paper fills remain G2's next eligible trade +5/-15/600; G3 never simulates fills.

Clock extension proposed for latency interpretation: clock_status=verified and
clock_skew_bound_ns (measured nonnegative bound). Raw observed UTC differences
and same-clock warning lead are separated from clock-qualified SIP percentiles.
Absent clock evidence leaves qualified latency UNKNOWN. Bucket evidence reports
latency lower/upper bounds, never an exact fabricated SIP instant.

Local probe extension is collector-only: gateway_cpu_pct from /proc PID stat
deltas bound to PID starttime; system_per_core_cpu_pct from /proc/stat deltas
(NOT falsely labelled gateway per-core). New ERROR count begins at a bounded
log baseline; rotation, truncation, missing/oversize/read failures are UNKNOWN.
Probes run off-loop when explicitly called, no timer or live run today.

## Plan and acceptance boundary

Implement new module/probe module, offline-only script and dedicated tests.
Bound payload bytes, read count, dedup history, decisions and comparison records;
surface lost coverage/counter resets/generation changes. Stable ten-second rows
are anchored to 04:11 ET; missing samples still emit UNKNOWN. Companion CSV keeps
per-stock BOTH detector entry time/price/outcome, lineage, signal bounds and
decision time, even when one detector has no recorded counterpart.
Run focused tests and existing paper/gateway neighbors locally, then commit with
one recognized OpenAI Codex trailer. Main must review actual G1/G2 field matching.
All generated receipts today are CONTROLLED/OFFLINE, not tomorrow's observed data.

## Oct12 producer compatibility checkpoint (not final acceptance)

G1 current head b5c0125b295e04c6cc6758e3b7a3d4819aa5d0c5 uses the proposed
gateway names; queue_drops and dropped_total are separate counters. It additionally
exposes max_publish_lag_ms, which should be retained as a peak, not conflated with
the instantaneous lag_ms. G2 current uncommitted draft uses observed_decision_ns,
strategy_code,logical_id,lineage and aggregate signal_start_ms/signal_end_ms. It
does not yet provide the full proposed ledger/window/lineage_id/entry/outcome
records or Snapshot counterpart. Its consumer_sample uses owned_warning_symbols,
shared_owned_symbols,active_warning_paths,coverage/status, not the proposed
warning_count/subscribed_count. These are NOT silently reinterpreted by G3.
One controlled incompatibility test explicitly proves missing entry records do
not become fictitious comparisons. Contract acceptance remains BLOCKED until
MAIN coordinates the final producer fields; no assertion that this draft can
produce the full actual W4 receipt. Main's ledger/cold-seed human answers are
settled: separate labelled warning ledgers and conservative whole-bucket evidence.
The pending item is producer observability, not the human policy choice.

Reported: initial 36 controlled tests PASS, producer gap surfaced in commentary.
F worker's store.py coordination note is outside G3; no edit or ownership approval
was made. B/service.py remains unchanged. No direct worker-to-worker message was
sent; app messaging to MAIN (native ancestor) is unavailable, so this assessment
and commentary are the handoff for the integrator to read/relay.

## Additive health-publisher assessment before implementation

The human explicitly assigned the separate 1Hz health INPUT producer to G3.
This supersedes the earlier no-health-writer assumption, NOT the read-only W4
collector or G1 watchdog ownership. No source edit outside G3 new modules/scripts.
Read G1 final PROTOCOL.txt at b5c0125b: health schema_version=1,observed_wall_ns,
oms_sync_seq,oms_sync_seconds,v2_bar_lateness_seconds; missing/future/>5s age or
skipped/regressed OMS sequence fails closed. It must exist before A enable04:00;
W4 reporting alone starts04:11. Runtime setup/install stays MAIN after review.

Actual OMS source: oms/service.py::sync_broker_state logs monotonic measured
duration_ms at [OMS-BROKER-SYNC-PASS] id=N phase=end outcome=... . A start line
is NOT a completed duration. Existing v2 heartbeat's secs_since_last_bar is
time since ANY bar, including warmup; NOT completed-bar lateness beyond due.
V2-GAP-DETECT is conditional, cannot establish full fresh watched-set coverage.
No general measured current-due v2 receipt source found; operational dependency
remains BLOCKED. Do not refresh a cached completion/heartbeat as fresh evidence.

Proposed explicit local receipt input (G3-owned reader, source producer pending):
schema_version=1,receipt_kind=measured,observed_wall_ns (original measurement time),
oms_instance_id,oms_sync_seq,oms_sync_seconds,oms_measured_wall_ns,oms_source
(OMS-BROKER-SYNC-PASS.phase=end),oms_coverage=complete;
v2_instance_id,v2_bar_lateness_seconds,v2_measured_wall_ns,v2_source
(completed_bar_due_monitor),v2_coverage=complete_current_due,
v2_due_wall_ns,v2_completed_wall_ns (null when overdue pending).
Source clocks must explicitly be UTC: clock_status=verified. These are actual
receipt fields, not a G3 guess from log mtime, read time or generic heartbeat.
G3 validates receipt freshness/finite values/sequence/identity and retains the
minimum original measurement timestamp in the output. Reading unchanged input
once/second cannot make it fresh. Unknown input atomically publishes an explicit
UNKNOWN record missing required measurements, so G1 fails closed immediately.
No G3 thresholds for load/slow sync/lag, no A control call or second watchdog.

Implementation: bounded input <=8192B, fresh receipt <=5s, sequence/identity
fence, one off-loop read/write per explicitly launched second, no startup task.
Atomic replacement in a pre-existing G3 runtime directory; reject symlink/unsafe
directory, create file0600 owned by publisher. MAIN must install publisher and
gateway as trader/same UID with directory0750 and verify access. Do not create,
chmod, launch or write /run now. Tests only use temporary controlled files.
Operational acceptance requires the real source producer and actual provenance
receipt, not merely green publisher controls; still NOT DONE today.

## Final local candidate checkpoint

This initial checkpoint is SUPERSEDED for health by the subsequent main review
and HEALTH_V2_PROPOSAL.md. The v1 freshness rule is not operationally valid.

190 tests PASS in2.45s: 70 G3 controls and120 unchanged paper/gateway neighbors.
Ruff PASS. Duplicate-bin control was RED then GREEN unchanged; lifecycle-update
control was RED (2 latency decisions vs1) then GREEN unchanged. Outcome updates
remain visible while one causal decision is counted once. Source observation can
precede actual decision capture; decision is checked against original read time,
not forcibly backdated to observation. Clock-qualified latency still requires
explicit clock evidence; unqualified raw arithmetic is labelled separately.

Six retained TESTONLY JSONL records are CONTROLLED fixtures, not recorded fills.
1915 stable ten-second rows are tested, including UNKNOWN missing intervals;
large generated CSVs were not retained. Companion rows retain both real-shaped
entry time/price/outcome/lineage without adding warning and Snapshot totals.
No observed future market data, no provider ACK, no actual A peak/burst proof.

Latest G2 draft checkpoint: Snapshot DETECTED/FILLED/EXITED metrics now exist,
with nested payloads; aggregate metrics carry a signals array. This supersedes
the earlier checkpoint's absent-counterpart description. Still incompatible with
the flat proposed G3 contract: observed_decision_ns vs observeddecision_ns;
strategy_code/logical_id/lineage plus payload vs explicit ledger/window/lineage_id
and full entry/outcome fields. Raw Snapshot lifecycle packets currently omit
gateway_instance_id/generation. MAIN must coordinate a final producer shape or
assessed bounded adapter; no alias/epoch/comparison identity is guessed here.
Subscription ACK remains null. Missing clock provenance remains UNKNOWN.

Health input setup: the opt-in scripts/momwarn1_health.py accepts explicit local
input/output paths; it was NOT launched. No /run file or directory was created.
Its producer must establish complete OMS sequence coverage and actual current-due
v2 coverage, not infer either from generic heartbeat/log age. Under this strict
receipt design, sparse old completions cannot sustain a fresh <=5s health input;
1Hz rereads do NOT solve that dependency. MAIN must review the measured-source
plan before W3 setup. G1 remains fail-closed if that evidence is unavailable.

Reported in commentary: initial36, expanded184, final190 local tests; producer
and health-provenance blockers surfaced before commit. B independently checked
clean at a844494652381153f7e63e49bb6c596a1f46a580; no B source/writer transition.
Original B68.4ms and MNDR20s causes remain UNKNOWN, no A/D waiver transfer.
Next: MAIN exact-head review and producer/source contract coordination, then
authorized setup/install, then actual Oct13 W4 receipt. No execution scheduled.

## Main review: material readiness blockers, superseding v1 health

Main verified actual proc setting MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS=15.
G3 read source control-loop scheduling/dynamic stop-guard shortening. The five-
second completed-source age prototype falsely fails between normal15s passes.
Main proposed configured15 plus bounded5 allowance for assessment, distinct from
monitor5s. No explicit human approval; controlled tests do not confer approval.
The first-current-day>=07:00 predicate must be sourced to the recorded card/main
verified eligibility, not missing data. Actual anchored REST source opens07:00;
unconditional pre07 lateness requirements would wrongly block04:11-07 trial.

HEALTH_V2_PROPOSAL.md names the exact proposed fields before further build.
New momwarn_health_contract.py implements ONLY independent v2 policy + real bounded
OMS log/proc reader, no G1 edits/wiring. OMS original completion stamp is unchanged
while a successful actual EOF/proc monitor operation gets a separate fresh stamp.
Normal6/15/20s source age is accepted; >20, stale/unreadable monitor is UNKNOWN.
Applicable v2 real lateness remains unchanged; sourced pre07/empty-watchlist N/A
keeps lateness null. Blank/unreadable sources cannot become N/A or fake zeros.
The v1 prototype remains only for controlled component receipts; its CLI now
refuses launch. G1v1 cannot consume healthv2 until the schema owner reviews it.

Actual OMS reader implemented, tested with controlled original log lines/proc
files, NOT launched on box. Applicable v2 source still missing: generic heartbeat,
conditional MACD probe and isolated watchlist state cannot prove every current
completed-bar due/completion. External JSON is a placeholder contract, NOT an
end-to-end implemented receipt producer. MAIN must supply actual prior-card
expectation reference and coordinate a reviewed source adapter. No trading-source
hook or DB/provider probe added. NOT READY for install; tomorrow W4 NOT DONE.

Final review-candidate receipt:204 PASS in2.54s,84 G3 +120 unchanged neighbors.
Ruff/diffcheck PASS; evidence plus controlled fixture remains <=500 lines.
Reported latest result/blockers in commentary. The actual-source reader is NOT
wired to the v1 publisher; code integration waits for G1's schema review.
Source file/proc reads were controlled temporary fixtures only, no box launch.
