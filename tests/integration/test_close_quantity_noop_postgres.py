"""Same actual SELL callers on isolated real PG; venue replies are controlled."""

import pytest

from tests.integration.test_cancel_terminal_runtime import sessions as controlled_database
from tests.unit import test_close_quantity_noop as control

database = controlled_database


@pytest.mark.asyncio
@pytest.mark.parametrize("quantities", control.EQUAL)
async def test_pg_equal_sell_quantity_does_not_dirty_intent(database, monkeypatch, quantities):
    await control.test_equal_sell_quantity_does_not_dirty_intent_before_lookup(database, monkeypatch, quantities)


@pytest.mark.asyncio
@pytest.mark.parametrize("quantities", control.CLAMP)
async def test_pg_real_clamp_autoflushes_before_order_lookup(database, monkeypatch, quantities):
    await control.test_real_clamp_remains_dirty_and_autoflushes_before_order_lookup(database, monkeypatch, quantities)


@pytest.mark.asyncio
async def test_pg_postwire_parent_failure_rolls_back(database, monkeypatch):
    await control.test_postwire_parent_failure_rolls_back_clamped_intent_and_order(database, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "dirty"])
async def test_pg_unrelated_rows_still_require_original_autoflush(database, monkeypatch, kind):
    await control.test_unrelated_rows_still_require_original_autoflush(database, monkeypatch, kind)
