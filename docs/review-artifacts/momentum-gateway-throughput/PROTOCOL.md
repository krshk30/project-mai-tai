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

- From 16:05 through 19:59:59 ET, start only after the existing OMS restart preflight proves zero
  open managed rows and fresh, zero non-zero `account_positions` on both live accounts in strict
  mode (manual/protected symbols are not excluded). Re-run that proof immediately before each of
  the three replays. A position appearing between runs aborts the suite as UNMEASURED. At or after
  20:00 ET, record the after-hours branch instead; it does not claim that account state was read.
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
sampled. A silent or dead Momentum consumer must also be exercised. The producer and consumer are
separate processes connected by a local Unix datagram socket: parse and non-awaiting queue offer
run in the producer, a writer task drains to the non-blocking socket, and the consumer reads in its
own process. Hand-off lag is consumer receipt time minus producer `received_ns` on the same host
clock. A connected-but-non-reading consumer must fill the socket, increment a dedicated
would-block drop counter, and leave producer offer-loop duration within the same +10% or +50 ms
allowance as the active-consumer 3x run. Queue drops and socket drops are reported separately.

Known Step 2 limit: the replay tape emits one trade per frame. A live Massive websocket frame may
batch many trades, and a Unix datagram larger than the socket buffer raises `EMSGSIZE`; Step 2 does
not exercise that batched-frame shape. Before Step 3 is wired, oversized batches must be split or
counted and dropped without terminating the writer task.

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
- CPU is sampled once per second for the producer process and expressed as a percentage of one
  CPU. The separate consumer process and running gateway PID are sampled separately so a replay
  cannot hide work shifted into either one.

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
4. The gateway stand-in never blocks on the Momentum consumer; a stalled separate consumer raises
   the Unix-socket would-block drop count while producer offer-loop timing remains within its
   active-consumer allowance.

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

## Step 2 executable (not wired)

The measurement implementation is intentionally standalone:

- `project_mai_tai.momentum_gateway_handoff` is the proposed parse-and-forward path: an
  `asyncio.Queue` with non-awaiting `put_nowait`, drop-oldest behavior, and explicit input,
  forwarded, dropped, and parse-failure counters. Its writer uses a non-blocking local Unix
  datagram socket and its measurement consumer is a separate spawned process. No gateway or
  service imports it.
- `project_mai_tai.backtest.momentum_gateway_throughput population` reads complete Massive
  `us_stocks_sip/trades_v1/YYYY/MM/YYYY-MM-DD.csv.gz` files. Massive describes these as daily,
  whole-market SIP files containing tick-level trades from exchanges and dark pools:
  <https://massive.com/docs/flat-files/stocks/trades>. This is the least-load source because it is
  one bulk file per session rather than per-symbol REST pagination. Files publish the next day;
  prices are unadjusted; SIP timestamps are UTC epoch values converted explicitly to ET; and the
  files cannot reproduce websocket packet batching, host-arrival jitter, upstream omissions, or
  later provider corrections. Those limits are printed in every population result. SIP
  timestamps, not file row order, define the measured seconds and replay pace. The optional
  `fetch-population` path uses the path-style `flatfiles` S3 endpoint with SigV4, reads the existing
  `MAI_TAI_MASSIVE_API_KEY` only from the measurement process environment, checks free disk before
  downloading, and removes each raw day only after its count record and peak-minute tape exist.
- `project_mai_tai.backtest.momentum_gateway_throughput replay-suite` runs from 16:05 ET when the
  strict flatness proof passes, or after 20:00 ET under the explicit after-hours branch. It
  requires process niceness of at least 10 and performs the two-minute quote precheck, a fresh
  ten-minute read-only baseline before each 1x/3x/dead-consumer replay, the frozen abort checks,
  and one-second process sampling. It writes raw stream receipt timestamps and CPU/RSS samples.
  If the quote precheck has zero `QuoteTickEvent` rows, the quote-latency row and overall verdict
  are written `UNMEASURED`; the tool does not substitute another metric. Exit codes are `0` PASS,
  `1` FAIL, and `2` UNMEASURED, so the detached runner cannot page an unmeasured suite as a
  successful completion.

The Redis observer starts `XREAD` offsets at `$` and only calls `XREAD`/`XREVRANGE`. It never
publishes, trims, acknowledges, joins a consumer group, or changes either observed stream.
