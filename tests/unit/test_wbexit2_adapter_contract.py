"""SOURCE UNRUN: new H2 API proposal through the surviving fake SDK seam.

Main must compose the separately reviewed external backend proposal first.
No native capability or historical counterfactual is inferred from these bodies.
"""
from decimal import Decimal
from time import monotonic

import pytest

from project_mai_tai.broker_adapters import webull_exit_census as census_module
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, WebullPositionsUnavailable
from project_mai_tai.oms.wbexit2 import original_geometry
from project_mai_tai.settings import Settings
from tests.unit.test_webull_adapter import _FakeClient, fake_sdk as fake_sdk
from tests.unit.test_webull_list_primary import primary, Clock


def adapter_with(client, switch=True):
    adapter = primary(client)
    settings = Settings(_env_file=None)
    object.__setattr__(settings, "strategy_schwab_1m_v2_wbexit2_enabled", switch)
    # This WB2-specific negative profile must also exclude the WB3 geometry exporter.
    object.__setattr__(settings, "oms_webull_exit3_enabled", False)
    adapter.settings = settings
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True], ids=["OFF-no-request", "ON-fresh-request"])
async def test_H2_new_positions_capability_own_flag_and_actual_start(fake_sdk, switch):
    client = _FakeClient({"positions": {"has_next": False, "holdings": [
        {"symbol": "AIFA", "quantity": "10"}]}})
    adapter = adapter_with(client, switch)
    after = monotonic()
    if not switch:
        with pytest.raises(WebullPositionsUnavailable, match="disabled"):
            await adapter.read_wbexit2_positions("live:orb", after=after)
        assert client.calls == {}
    else:
        rows, started, account_id = await adapter.read_wbexit2_positions("live:orb", after=after)
        assert after < started <= monotonic()
        assert account_id == adapter.accounts_by_name["live:orb"].account_id
        assert [(r.broker_account_name, r.symbol, r.quantity) for r in rows] == [("live:orb", "AIFA", Decimal(10))]
        assert client.calls == {"positions": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"holdings": []},
    {"holdings": [], "has_next": "false"},
    {"holdings": [], "has_next": False, "hasNext": True},
    {"holdings": [], "positions": [{"symbol": "AIFA", "quantity": "10"}], "has_next": False},
    {"holdings": [{"symbol": "AIFA", "ticker": "FOREIGN", "quantity": "10"}], "has_next": False},
    {"holdings": [{"symbol": "AIFA", "quantity": "10", "qty": "9"}], "has_next": False},
    {"holdings": [{"symbol": "AIFA", "quantity": True}], "has_next": False},
    {"holdings": [{"symbol": "AIFA", "quantity": "NaN"}], "has_next": False},
    {"holdings": [], "has_next": False, "account_id": "FOREIGN"},
    {"holdings": [], "has_next": True},
    {"holdings": [{"symbol": "AIFA", "quantity": "10"}] * 2, "has_next": False},
])
async def test_partial_missing_or_conflicting_positions_never_imply_flat(fake_sdk, body):
    client = _FakeClient({"positions": body})
    with pytest.raises(WebullPositionsUnavailable, match="UNKNOWN"):
        await adapter_with(client).read_wbexit2_positions("live:orb", after=monotonic())
    assert client.calls == {"positions": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [False, True], ids=["OFF-old-metadata", "ON-own-geometry"])
async def test_H2_existing_LIST_exports_own_geometry_without_new_request(fake_sdk, monkeypatch, switch):
    base = "CONTROLLED-WBEXIT2-original"
    rows = []
    for suffix, field, price in (("T", "limit_price", "10.20"), ("S", "stop_price", "9.50")):
        coid = WebullBrokerAdapter._combo_leg_coid(base, suffix)
        rows.append({"client_order_id": coid, "order_id": "native-" + coid, "account_id": "ACC1",
            "items": [{"symbol": "AIFA", "side": "SELL", "qty": "10", "filled_qty": "0",
                       "order_status": "SUBMITTED", field: price}]})
    client = _FakeClient({"today": {"orders": rows, "has_next": False}})
    adapter = adapter_with(client, switch)
    reader = census_module.ExitCensusReader(adapter._query_budget, 2., clock=Clock())
    monkeypatch.setattr(census_module, "shared_exit_census", lambda *_args: reader)
    result = await adapter.confirm_exit_pair_from_today_list(broker_account_name="live:orb", symbol="AIFA",
        base_client_order_id=base, quantity=Decimal(10), original_attempts=[{"base": base, "quantity": "10"}])
    assert result.outcome == "reserved" and len(result.reports) == 2
    assert client.calls == {"today": 1}
    if switch:
        assert original_geometry(result.reports, base, Decimal(10), "AIFA") == (Decimal("10.20"), Decimal("9.50"))
    else:
        assert all("wbexit2_geometry" not in report.metadata for report in result.reports)
