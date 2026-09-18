# Momentum Gateway Throughput Protocol

Status: **PRE-REGISTERED / UNMEASURED**  
Date frozen: 2026-09-18  
Scope: Momentum Step 2 only. This document changes no runtime behavior and authorizes no Step 3 code.

## Question

Can the existing market-data gateway forward the whole-market `T.*` tape from 04:00 through
09:30 ET to Momentum over a local, non-Redis side channel without slowing the gateway's live work?

The target architecture remains one Massive websocket owned by the gateway. This measurement must
not open another Massive websocket during market hours, write whole-market ticks to Redis, stop the
Momentum proving unit, or change any trading rule.

## Required population

Measure at least three complete recent sessions, including 2026-09-17. For each session, count all
whole-market prints, eligible and ineligible, from 04:00:00 through 09:29:59 ET. Report:

- total prints and seconds observed;
- maximum prints in one second;
- p99 prints per second;
- busiest rolling 60-second total and its ET window;
- the source of the tape and any population it cannot see.

Fewer than three complete sessions, omission of 2026-09-17, or a source that cannot establish the
whole-market population is **UNMEASURED**, never PASS. No partial-session result may be promoted.

## Replay

After 16:00 ET, identify the busiest measured minute and replay it on the box through the same
parse-and-forward function intended for Step 3. Run the replay at 1x and 3x the measured arrival
rate. The consumer may be synthetic, but it must exercise the proposed bounded hand-off rather
than a shortcut around it.

The live-box replay has these fail-closed preconditions and limits:

- Start only when both live accounts have zero open managed rows and zero non-zero account
  positions, or after 20:00 ET.
- Run as a separate `nice -n 10` process. Never import it into, attach it to, or restart the
  running gateway.
- Before each run, record the gateway heartbeat status, the current count of
  `1008 (policy violation)` lines in `market-data.log`, and the one-minute load average.
- Abort immediately if the gateway heartbeat is not `healthy`, the policy-violation count rises,
  or the one-minute load average exceeds 3.5 on the four-CPU box.

An aborted run is **UNMEASURED**, not FAIL. It may be retried only from a new ten-minute baseline;
partial measurements from the aborted run are diagnostic and do not enter the verdict.

For each run report:

- input frames, forwarded frames, dropped frames, and parse failures;
- p50, p95, p99, and maximum hand-off lag;
- process CPU as a percentage of one CPU and peak RSS;
- the gateway's existing snapshot cadence and active-symbol quote latency before and during replay.

The measurement must state how CPU, lag, snapshot cadence, and active-symbol quote latency were
sampled. A silent or dead Momentum consumer must also be exercised: the gateway must continue its
own work while the bounded paper queue drops frames and increments a counter.

### Existing-work sampling

Each 1x and 3x replay requires its own uninterrupted ten-minute baseline immediately before the
run. A read-only sampler starts Redis `XREAD` cursors at `$` on `snapshot-batches` and
`market-data`; it must not publish, trim, acknowledge, or otherwise mutate either stream.

- Snapshot cadence is the interval between consecutive `SnapshotBatchEvent.produced_at` values.
  Report its p99 for the baseline and replay. A missed five-second cycle is counted for every full
  additional five-second slot with no completion between consecutive batches; the replay permits
  zero missed cycles.
- Active-symbol quote latency is measured only for `QuoteTickEvent` rows whose symbol is in the
  latest `market-data-subscriptions` state at baseline start; its count must reconcile to the
  gateway heartbeat's `active_symbols` value. It is the sampler's UTC receipt time
  minus `EventEnvelope.produced_at`, reported as p99. This is explicitly publisher-to-observer
  latency; the current quote payload has no SIP timestamp, so it does not claim exchange-to-host
  latency. The same sampler, host clock, symbol set, and calculation are used for baseline and
  replay.
- CPU is sampled once per second for the separate replay process and expressed as a percentage of
  one CPU. The running gateway PID is sampled separately so a replay cannot hide work shifted into
  the live process.

For each metric, the replay p99 must be no greater than:

`baseline p99 + max(10% of baseline p99, 50 ms)`.

The raw timestamps and one-second CPU samples are retained with the report so the p99 values are
reproducible rather than copied from a summary line.

## Frozen verdict

PASS requires all of the following at **3x the measured peak**:

1. p99 hand-off lag is below 250 ms.
2. The forward path uses less than 50% of one CPU.
3. Snapshot-cadence p99 and active-symbol quote-latency p99 each stay within
   `baseline + max(10% of baseline, 50 ms)`, with zero missed five-second snapshot cycles.
4. The gateway read loop never blocks on the Momentum consumer; a stalled consumer raises the
   dropped-frame count instead.

Any failed condition is **FAIL**. Report the measurements and stop; the operator decides whether
the architecture proceeds. The thresholds and consequences above must not change after results are
read.

## Result template

| Session / replay | Population | Peak 1 s | p99 1 s | Busiest 60 s | p99 hand-off | CPU | Existing-work lag | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Session 1 | pending | pending | pending | pending | N/A | N/A | N/A | UNMEASURED |
| Session 2 | pending | pending | pending | pending | N/A | N/A | N/A | UNMEASURED |
| 2026-09-17 | pending | pending | pending | pending | N/A | N/A | N/A | UNMEASURED |
| 1x replay | pending | N/A | N/A | N/A | pending | pending | pending | UNMEASURED |
| 3x replay | pending | N/A | N/A | N/A | pending | pending | pending | UNMEASURED |

Step 3 stays blocked until this table is complete, the verdict is PASS, and independent review has
accepted the evidence.
