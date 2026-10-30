"""CONTROLLED DB-only scan load beside 240Hz heartbeats; no native/fleet claim."""
import asyncio
from decimal import Decimal
import logging
from time import perf_counter

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition
from project_mai_tai.oms import wbexit2
from tests.unit.wbexit_stale1_episode_port import seed_episode, hydrate, snapshot, WB2_ID, WB3_ID


@pytest.mark.asyncio
async def test_existing_DB_scan_60s_held_owner_with_240Hz_foreground(tmp_path, record_property):
    case = seed_episode(tmp_path)
    logger = case.service.logger
    previous = (logger.handlers[:], logger.propagate, logger.level)
    formatted = []
    class FormatOnly(logging.Handler):
        def emit(self, record):
            # Format the real scan/checkpoint records without a disk sink.
            message = record.getMessage()
            assert message.startswith('[WBEXIT2-')
            formatted.append(len(message))
    logger.handlers = [FormatOnly()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        with case.factory.begin() as db:
            # CONTROLLED held-native operand forces full validation to refuse
            # and retain responsibility on every existing refresh invocation.
            db.scalar(select(AccountPosition)).quantity = Decimal(1)
        before = snapshot(case.factory)
        await hydrate(case)
        callbacks, lateness, scans = [], [], []
        started = perf_counter()
        for index in range(14401):
            deadline = started + index / 240
            await asyncio.sleep(max(0, deadline - perf_counter()))
            entered = perf_counter()
            lateness.append(max(0, entered - deadline))
            if index % 240 == 0:
                scan_started = perf_counter()
                await wbexit2.refresh_pending(case.service)
                scans.append(perf_counter() - scan_started)
            await asyncio.sleep(0)
            callbacks.append(perf_counter() - entered)
        elapsed = perf_counter() - started
        after = snapshot(case.factory)
        assert len(callbacks) == 14401 and len(scans) == 61
        assert elapsed >= 60 and len(callbacks) / elapsed >= 200
        assert max(callbacks) < .050 and max(lateness) < .050 and max(scans) < .050
        assert all(after[table] == before[table] for table in before if table != 'system_incidents')
        assert after['system_incidents'][str(WB3_ID)] == before['system_incidents'][str(WB3_ID)]
        state = after['system_incidents'][str(WB2_ID)]['payload']
        assert state['phase'] == 'HOLD' and state['remaining'] == '69'
        assert 'durable_accounting_retirement' not in state and case.attempts == []
        assert len(formatted) >= 122
        ordered = sorted(callbacks)
        for key, value in {
            'actual_seconds': elapsed, 'foreground_callbacks': len(callbacks),
            'foreground_events_per_second': len(callbacks) / elapsed,
            'callback_p99_ms': ordered[int(.99 * (len(ordered) - 1))] * 1000,
            'callback_max_ms': max(callbacks) * 1000, 'wake_lateness_max_ms': max(lateness) * 1000,
            'actual_DB_scans': len(scans), 'DB_scan_max_ms': max(scans) * 1000,
            'formatted_APP_log_records': len(formatted),
            'scope': '61actual_DBscans_at_CONTROLLED1Hz_plus14401heartbeatcallbacks_NOT200DBscans_per_second_NOTv2OMS_native_loop',
        }.items():
            record_property(key, value)
    finally:
        logger.handlers, logger.propagate, logger.level = previous
        case.engine.dispose()
