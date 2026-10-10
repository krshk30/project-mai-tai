"""Real PG parent-commit and RESERVE1 proof controls; broker answers are controlled."""

import pytest

from tests.integration.test_cancel_terminal_runtime import sessions as controlled_database
from tests.unit import test_reserve1_fifo_proof as control

database = controlled_database


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_pg_actual_recovery_reads_committed_rejection_while_fifo_blocked(database, monkeypatch, strategy):
    await control.test_actual_recovery_reads_committed_rejection_while_fifo_blocked(database, monkeypatch, strategy)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["client", "unknown_origin", "partial", "unknown_read", "filled_read"])
async def test_pg_fake_uncertain_or_filled_rejection_never_admits_retry(database, monkeypatch, case):
    await control.test_fake_uncertain_or_filled_rejection_never_admits_retry(database, monkeypatch, case)


@pytest.mark.asyncio
async def test_pg_ordinary_accepted_hard_stop_sell_remains_fifo(database, monkeypatch):
    await control.test_ordinary_accepted_hard_stop_sell_remains_fifo(database, monkeypatch)
