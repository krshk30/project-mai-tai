# Option A snapshot-read audit (2026-10-02)

Scope: code and read-only Redis measurement, not an install or treatment result.

## Guard read bound

The earlier Lua/XINFO approach is removed. Redis 7.0 materializes the first and last
large snapshot entries even when Lua returns only an ID; reviewer measurements were
175-430 ms of blocking work. A small client reply did not bound server work.

The guard bootstraps with one `XREVRANGE + - COUNT 1`, then polls
`XRANGE (<last_id> + COUNT 1` at 1 Hz. Empty polls return no payload. A returned
payload is discarded immediately; only the stream-ID timestamp is retained for cadence.
At most three reads occur in a tick. Three nonempty reads trigger Blind because catching
up was not proven; the existing stop-and-page path stops only paper. No XINFO or EVAL
is used. Every command requests at most one entry. An entry over 20,000,000 field bytes
is rejected after receipt; this is a client acceptance bound, not a Redis-side byte cap.
Malformed/future/repeated/backwards IDs and missing/stale cadence also fail closed.

RedisSafety is unchanged: a rise from the startup `evicted_keys` baseline or memory
above 1,600,000,000 bytes stops paper; unreadable evidence is Blind.

## Read-only measurement

Raw output: `guard-snapshot-measurement-20261002.jsonl` in this directory.
Box measurement: **2026-10-02 14:21:22-14:22:03 UTC**. Twenty sequential nonempty
XRANGE COUNT 1 reads were spaced one second apart, followed by twenty live polls.
No Redis configuration, stream, hash, service, or slow-log reset was performed.

- Nonempty forward reads: n=20, p50 **23.456 ms**, nearest-rank p95 **33.448 ms**,
  range **16.029-35.788 ms**, including transfer and client decode.
- One bootstrap XREVRANGE COUNT 1: **30.284 ms**.
- Payload field sizes: **5,993,777-5,998,473 bytes** for the twenty reads; bootstrap
  **5,999,385 bytes**. At most one such payload was held for each read.
- Live polls: 6 nonempty, 14 empty; empty wall times **0.366-1.422 ms**.
- SLOWLOG settings: `slowlog-log-slower-than=10000` microseconds, max length 128.
  `SLOWLOG GET 128` covered IDs 11203-11330, timestamps 13:05:10-14:22:18 UTC.
  **Zero slow-log records of any command in the measurement window**. Exact Redis
  execution times below 10 ms are not observable from that log; they are not zero.
- `evicted_keys`: **23 -> 23**. `used_memory`: **1,182,212,232 -> 1,182,152,992** bytes.

Wall times in read order (ms):
`21.474,25.977,19.842,22.654,23.674,27.249,35.788,23.894,16.029,29.383,18.033,33.448,26.069,24.741,18.508,17.412,23.237,22.790,16.965,28.892`.

## Other snapshot readers and assigned follow-up

The stream retained 180 entries at the earlier census. The byte estimates below use
the earlier measured 5,516,896-byte payload for comparison with the reviewer report;
the new measurement is about 6 MB. These are size estimates, not hard upper bounds.

| Reader | Read shape | Estimated payload bytes per backlog call | Owner | Next action |
| --- | --- | ---: | --- | --- |
| Guard (#1083) | Bootstrap one reverse entry; forward COUNT 1; max 3 reads/tick | About 6 MB per nonempty call | codex-2 | Independent review, pin and exact-SHA install before Monday's guard session. |
| Strategy alert warm-up | XREVRANGE required_cycles=120 | 662,027,520 (about 720 MB at current size) | codex-2 | Separate bounded warm-up PR: sequential COUNT 1 replay preserving all 120 cycles; measure peak reply and tests for identical warm-up output. Tonight record Redis eviction/owners immediately before and after the restart and halt on loss. |
| Strategy live priority consumer | XREAD COUNT 500 over four streams; snapshot retention 180 | 993,041,280 | codex-2 | Separate consumer-read PR: bound snapshot reads independently from small-event streams and test backlog fairness/offset continuity. |
| Momentum-paper live consumer | XREAD COUNT 500 across snapshot and market-data streams | 993,041,280 | codex-2 | Include in the consumer-read PR; preserve tick progress with bounded snapshot reads and a backlog regression. |
| Control-plane summary | `_read_stream_events(..., limit=1)` | 5,516,896 | codex-2 | Keep one-entry bound; add current payload-byte observation in the consumer audit. |
| Offline throughput replay | XREAD COUNT 5,000; snapshot retention 180 | 993,041,280 | codex-2 | Bound replay snapshots before another replay; no production run is authorized here. |

All multi-entry readers listed above remain multi-entry in this PR. Their next actions
are assigned follow-ups, not claims that they are fixed or approved for installation.

## Additional regression caught during rework

The preceding guard patch accidentally placed `minute_values()` under RedisSafety.
It is restored to LiveSignals and a direct regression test exercises the minute
values used by `run_guard`. This defect was not deployed.

Verification: 49 focused guard/sampler tests pass. Full local unit run: 4,893
passed, 56 failed; the failed-name set matches the preceding local full run
(normalizing a warning appended to one summary line). Mutations requesting
multiple entries, removing the three-read bound, and removing the minute method
each produce a failing regression test. Ruff and diff whitespace checks pass.
