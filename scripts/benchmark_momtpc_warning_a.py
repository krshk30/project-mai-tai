"""PREPARED, NOT RUN by worker. Explicit main-only serial 60s local synthetic A gate."""

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


async def run_gate(expected_head):
    # Import offline test doubles only after the explicit run gate; never start a provider.
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests/unit'), str(ROOT)]
    from tests.unit.test_momwarn1_gateway import aggregate, service
    from project_mai_tai.market_data.models import SnapshotRecord

    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if head != expected_head:
        raise ValueError('exact composed head required')
    svc, provider, _, warning_redis = service()
    await svc._warning_monitor_once()
    if not provider._warning_aggregates_enabled:
        raise RuntimeError('controlled gateway A route unavailable; no clock/gate override')
    stop = asyncio.Event()
    writer = asyncio.create_task(svc._warning_publish_loop(stop))
    snapshots = [SnapshotRecord(symbol=f'S{i:05}', last_trade_price=10.0) for i in range(13_269)]
    gaps, snapshot_counts = [], []
    rate, duration = 125, 60  # Two valid common A frames per iteration = 250/s.
    started = time.monotonic()

    async def ticker():
        prior = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.001)
            now = time.monotonic()
            gaps.append(now - prior - 0.001)
            prior = now

    async def batches():
        while not stop.is_set():
            snapshot_counts.append(await svc.publish_snapshot_batch_once(snapshots))
            await asyncio.sleep(5)

    tick_task, snapshot_task = asyncio.create_task(ticker()), asyncio.create_task(batches())
    sent = 0
    try:
        while time.monotonic() - started < duration:
            due = min(rate * duration, int((time.monotonic() - started) * rate))
            while sent < due:
                sip = int(datetime.now(UTC).timestamp() * 1000) // 1000 * 1000
                provider._handle_messages([
                    aggregate(sym='TpC', o=15.73, h=15.73, l=15.73, c=15.73, s=sip, e=sip + 1000),
                    aggregate(sym='TPC', o=82.77, h=82.77, l=82.77, c=82.77, s=sip, e=sip + 1000),
                    aggregate(sym='BCpC', o=16, h=16, l=16, c=16, s=sip, e=sip + 1000),
                    aggregate(sym='BCPC', o=80, h=80, l=80, c=80, s=sip, e=sip + 1000),
                ])
                sent += 1
                if sent % 32 == 0:
                    await asyncio.sleep(0)
            await asyncio.sleep(0.001)
        elapsed = time.monotonic() - started
        async def drain():
            while not svc._warning_queue.empty() or svc._warning_inflight_records:
                if writer.done():
                    return False
                await asyncio.sleep(0.001)
            return True
        try:
            drained = await asyncio.wait_for(drain(), timeout=3)
        except TimeoutError:
            drained = False
    finally:
        stop.set()
        for task in (writer, tick_task, snapshot_task):
            task.cancel()
        await asyncio.gather(writer, tick_task, snapshot_task, return_exceptions=True)
    bars = [data for name, data, _ in warning_redis.entries if name.endswith('momentum-warning-bars')]
    max_stall_ms = max(gaps, default=0) * 1000
    valid_rate = len(bars) / elapsed
    expected_price = {'TPC': 82.77, 'BCPC': 80.0}
    collisions = sum(row.get('symbol') not in expected_price or any(
        row.get(key) != expected_price.get(row.get('symbol')) for key in ('open', 'high', 'low', 'close')
    ) for row in bars)
    qualified = (drained and elapsed >= 60 and valid_rate >= 200 and max_stall_ms < 50
                 and svc._warning_revert_reason is None and svc._warning_drop_count == 0
                 and len(bars) == sent * 2 and collisions == 0
                 and snapshot_counts and all(n == 13_269 for n in snapshot_counts))
    files = ['src/project_mai_tai/market_data/gateway.py', 'src/project_mai_tai/market_data/publisher.py',
             'src/project_mai_tai/market_data/massive_provider.py']
    return dict(head=head, elapsed_seconds=elapsed, valid_A_published=len(bars), valid_A_per_second=valid_rate,
                raw_A_frames=sent * 4, expected_common_frames=sent * 2, collision_frames=collisions,
                drained=drained, max_stall_ms=max_stall_ms, snapshot_batch_counts=snapshot_counts,
                reverts=svc._warning_revert_reason, dropped=svc._warning_drop_count, qualified=bool(qualified),
                provider_calls=0, evidence_kind='LOCAL_SYNTHETIC_SOURCE_GATE_NOT_REAL_WS_PEAK_OR_SOAK',
                source_sha256={path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in files})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-approved', action='store_true')
    parser.add_argument('--expected-head')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.run_approved:
        print('NOT RUN. Main serial approval + exact composed head required; valid A >=200/s,60s,stall<50ms.')
        return
    if not args.expected_head or args.output is None:
        parser.error('run requires --expected-head and a fresh --output')
    if args.output.exists():
        raise FileExistsError(args.output)
    result = asyncio.run(run_gate(args.expected_head))
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['qualified'] else 1)


if __name__ == '__main__':
    main()
