# Option A snapshot-read audit (2026-10-02)

This is a code and read-only Redis audit, not an install or a live treatment pass. At the read,
`mai_tai:snapshot-batches` held 180 entries and used 1,132,558,076 Redis bytes. The latest
serialized `data` field was 5,516,896 bytes. The estimates below multiply that one measured
entry size by each reader's possible snapshot count; entry sizes vary, so they are not hard
upper bounds. The Redis server had `maxmemory=2,147,483,648`, `used_memory=1,180,936,272`,
and `evicted_keys=23` before the new ID-only probe. The probe returned the 15-byte ID
`1790947923632-0`; `evicted_keys` remained 23 afterward.

| Reader | Code read | Snapshot payload bytes at measured size | Status |
| --- | --- | ---: | --- |
| Option A guard | Prior `XREVRANGE count=180` | 993,041,280 | Replaced with one server-side `XINFO STREAM` extraction per second. Only the ID is returned to the client; the reply is rejected above 64 bytes. Redis can still inspect its first/last entries internally. |
| Strategy alert warm-up | `strategy_engine_app.py` `XREVRANGE count=required_cycles`; 10 minutes / configured 5-second interval = 120 cycles | 662,027,520 | **Unchanged risk.** Restart warm-up can read hundreds of MB into the client. This PR does not make that read safe. |
| Strategy live priority consumer | `strategy_engine_app.py` `XREAD count=500` across four streams; snapshot stream retains 180 | Up to 993,041,280 for the snapshot portion of one backlog read | Unchanged. Normal polling usually receives one new batch; a backlog is different. |
| Momentum-paper live consumer | `momentum_paper_app.py` `XREAD count=500` across snapshot and market-data streams; snapshot stream retains 180 | Up to 993,041,280 for the snapshot portion of one backlog read | Unchanged. |
| Control-plane snapshot summary | `control_plane.py` `_read_stream_events(..., limit=1)` | 5,516,896 | Unchanged one-entry read. |
| Offline throughput replay | `backtest/momentum_gateway_throughput.py` `XREAD count=5,000`; snapshot stream retains 180 | Up to 993,041,280 for the snapshot portion | Not a live service, but unchanged. |

The guard's new cadence source is Redis stream-ID milliseconds, not the event payload's
`produced_at`. It polls at 1 Hz only from 5 minutes 30 seconds before treatment start
through treatment end, records each new ID, and computes five-minute inter-batch intervals
from those IDs. A malformed, oversized, future, backwards, missing, or stale ID is blind
evidence and stops paper. Redis `evicted_keys` increasing from the guard-start baseline or
`used_memory` exceeding 1,600,000,000 bytes directly stops paper and pages through the
existing guard stop path. Missing Redis memory evidence is blind and also stops paper.
