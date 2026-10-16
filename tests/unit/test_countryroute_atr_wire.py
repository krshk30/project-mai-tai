"""Selected ATR wire shapes through the existing adapter, with no live SDK calls."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from project_mai_tai.atr_country_route import AtrCountrySelection
from project_mai_tai.broker_adapters import webull as webull_module
from tests.unit.test_webull_adapter import (
    _FakeClient, _adapter, _mirror_market_metadata, _order, fake_sdk as fake_sdk,
)

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_sdk")
@pytest.mark.parametrize("ask,shape", [
    ("4.30", "STOP_LOSS_LIMIT"), ("4.31", "LIMIT"), ("4.34", None),
])
async def test_selected_webull_keeps_real_adapter_shape_and_band(
    monkeypatch, ask, shape,
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(webull_module, "datetime", Clock)
    selected = AtrCountrySelection(
        "BMGL", 1789565403195, "webull", "live:orb", "sec_unknown", "UNKNOWN",
        "", NOW.isoformat(),
    )
    metadata = {**_mirror_market_metadata(ask, observed_at=NOW), **selected.metadata()}
    client = _FakeClient({"place": {"order_id": "CONTROLLED-ATR-WIRE"}})
    adapter = _adapter(client, _native_stop_map_enabled=True)
    reports = await adapter.submit_order(_order(
        strategy_code="schwab_1m_v2", symbol="BMGL", quantity=Decimal(1),
        metadata=metadata, order_type="STOP_LIMIT",
    ))
    if shape is None:
        assert reports[0].event_type == "rejected"
        assert reports[0].reason.startswith("ASK_PAST_BAND:")
        assert client.calls == {}
    else:
        assert client.calls["place"] == 1
        wire = client.last["place"].values
        assert wire["order_type"] == shape
        assert wire["limit_price"] == "4.33"
        if shape == "STOP_LOSS_LIMIT":
            assert wire["stop_price"] == "4.31"
        else:
            assert "stop_price" not in wire
