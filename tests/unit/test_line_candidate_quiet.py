"""Joined seed path must stay quiet across overnight and weekend boundaries."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms
from tests.unit.test_line_repair_preopen_seed import _bot, BOUNDARY


@pytest.mark.asyncio
async def test_friday_close_to_monday_0400_has_no_seed_requests_or_tracebacks(caplog):
    start = int(datetime.fromisoformat("2026-10-09T16:00:00-04:00").timestamp() * 1000)
    end = int(datetime.fromisoformat("2026-10-12T04:00:00-04:00").timestamp() * 1000)
    now = [start]
    bot = _bot("MI", BOUNDARY)
    bot.strategy._now_ms = lambda: now[0]
    for stamp in range(start, end + 1, 60_000):
        now[0] = stamp
        bar = ChartBar("MI", 2, 2, 2, 2, 10, stamp - 60_000)
        proof = SimpleNamespace(closed_ids=(bar.timestamp_ms,), end_ms=stamp, complete=True)
        assert await bot._join_line_preopen("MI", session_start_ts_ms(stamp), [bar], proof) == ([bar], proof)
    bot._atr_massive_seed_client.fetch_preopen.assert_not_called()
    assert not any(record.exc_info for record in caplog.records)
