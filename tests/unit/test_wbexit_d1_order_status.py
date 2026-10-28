"""UNRUN: existing fake SDK/primary reader fixture, no new runner or provider."""

from decimal import Decimal
from time import monotonic

import pytest

from project_mai_tai.broker_adapters.webull import WebullPositionsUnavailable
from project_mai_tai.settings import Settings
from tests.unit.test_webull_adapter import _FakeClient, _order
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk
from tests.unit.test_webull_list_primary import listed, primary, row


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True], ids=["exact-terminal-partial", "foreign-detail-no-cache-fallback"])
async def test_d1_status_is_exact_fresh_detail_not_cached_positions_or_list(fake_sdk, foreign):
    raw = row("foreign" if foreign else "our-sell", "CANCELLED", qty="7", price="10")
    raw["items"][0]["side"] = "SELL"
    client = _FakeClient({"detail": raw, "today": listed(row("our-sell", "FILLED", qty="27", price="10"))})
    adapter = primary(client)
    adapter.settings = Settings(_env_file=None, strategy_schwab_1m_v2_wbexit1_d1_enabled=True)
    request = _order(client_order_id="our-sell", strategy_code="schwab_1m_v2", symbol="AIFA",
                     side="sell", intent_type="close", quantity=Decimal(27))
    after = monotonic()
    if foreign:
        with pytest.raises(WebullPositionsUnavailable, match="status UNKNOWN"):
            await adapter.read_exit_order_status(request, after=after)
    else:
        report, started = await adapter.read_exit_order_status(request, after=after)
        assert after < started <= monotonic()
        assert report.origin == "broker" and report.client_order_id == "our-sell"
        assert report.event_type == "cancelled" and report.filled_quantity == Decimal(7)
        assert report.quantity == Decimal(27) and report.broker_order_id == "broker-our-sell"
    assert client.calls == {"detail": 1}


@pytest.mark.asyncio
async def test_d1_status_default_off_makes_no_native_query(fake_sdk):
    client = _FakeClient({})
    adapter = primary(client)
    adapter.settings = Settings(_env_file=None)
    with pytest.raises(WebullPositionsUnavailable, match="disabled"):
        await adapter.read_exit_order_status(_order(), after=monotonic())
    assert client.calls == {}
