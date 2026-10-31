"""Real adapter SELL paths / fake HTTP and SDK only. Author UNRUN, not native proof."""

from decimal import Decimal as D

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.settings import Settings
from tests.unit.test_webull_adapter import _adapter, _FakeClient, fake_sdk as fake_sdk


STRATEGIES = ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"]


def sell(strategy, session, *, account):
    return OrderRequest("price1-OLOX-exit-control", account, strategy, "OLOX", "sell", "close",
        D(2), "HARD_STOP", {"stop_guard": "true", "session": session,
            "order_type": "limit", "limit_price": ".97", "reference_price": ".97",
            "extended_hours": "true" if session == "AM" else "false"}, "limit")


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("session", ["AM", "NORMAL"])
async def test_schwab_subdollar_sell_reaches_exactly_one_unchanged_transport(strategy, session, monkeypatch, caplog):
    adapter = SchwabBrokerAdapter(Settings(_env_file=None, schwab_access_token="synthetic-token",
        schwab_token_store_path=None), accounts_by_name={
            "live:schwab_1m_v2": SchwabAccountConfig(account_hash="synthetic-hash")})
    sent = []

    async def transport(method, path, *, body=None):
        # This is deliberately NOT **kwargs: an entry capability must not leak
        # onto a SELL, and PRICE1 must not reject the SELL before this seam.
        assert method == "POST" and path == "/trader/v1/accounts/synthetic-hash/orders"
        sent.append(body)
        assert body["session"] == session and body["orderType"] == "LIMIT"
        assert D(str(body["price"])) == D(".97")
        assert body["orderLegCollection"] == [{"instruction": "SELL", "quantity": 2.0,
            "instrument": {"symbol": "OLOX", "assetType": "EQUITY"}}]
        return 201, {"Location": "https://api.schwabapi.com/trader/v1/accounts/synthetic-hash/orders/TEST-EXIT"}, {}

    async def no_poll(*args, **kwargs):
        pytest.fail("stop-guard SELL unexpectedly waited for a fill")

    monkeypatch.setattr(adapter, "_authorized_request_json", transport)
    monkeypatch.setattr(adapter, "_wait_for_terminal_order", no_poll)
    reports = await adapter.submit_order(sell(strategy, session, account="live:schwab_1m_v2"))
    assert len(sent) == len(reports) == 1
    assert reports[0].event_type == "accepted" and reports[0].broker_order_id == "TEST-EXIT"
    assert reports[0].side == "sell" and reports[0].filled_quantity == 0
    assert "[PRICE1-REFUSED]" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("session", ["AM", "NORMAL"])
async def test_webull_subdollar_sell_reaches_exactly_one_unchanged_sdk_request(strategy, session, fake_sdk, caplog):
    client = _FakeClient({"place": {"order_id": "TEST-EXIT"}})
    adapter = _adapter(client)
    adapter._instrument_cache["OLOX"] = "synthetic-instrument"
    reports = await adapter.submit_order(sell(strategy, session, account="live:orb"))
    assert client.calls == {"place": 1}
    values = client.last["place"].values
    assert values["side"] == "SELL" and values["order_type"] == "LIMIT"
    assert D(values["limit_price"]) == D(".97") and values["qty"] == "2"
    assert values["account_id"] == "ACC1" and values["instrument_id"] == "synthetic-instrument"
    assert values["extended_hours_trading"] is (session == "AM")
    assert len(reports) == 1 and reports[0].event_type == "accepted"
    assert reports[0].broker_order_id == "TEST-EXIT" and reports[0].filled_quantity == 0
    assert "[PRICE1-REFUSED]" not in caplog.text
