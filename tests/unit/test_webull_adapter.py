"""Unit tests for the Webull live broker adapter.

The Webull SDK is imported lazily inside the adapter and is NOT a CI dependency, so we
register fake ``webull.*`` modules in ``sys.modules`` and inject a fake client. Tests cover
request construction + response mapping against the shapes the on-box probe confirmed (reads)
and the defensive parsing for the order shapes still to be confirmed by a funded test order.
"""
from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import (
    WebullAccountConfig,
    WebullBrokerAdapter,
)


# --------------------------------------------------------------------------- fakes
class _Resp:
    def __init__(self, body: object) -> None:
        self.body = body


class _JsonResp:
    """Mimics a live requests.Response: body via .json(), no .body attribute."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Req:
    """Generic setter-bag standing in for an SDK request object."""

    _kind = "?"

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            field = name[4:]
            return lambda value: self.values.__setitem__(field, value)
        raise AttributeError(name)


def _make_req(kind: str):
    return type(kind, (_Req,), {"_kind": kind})


class _FakeClient:
    """Dispatches get_response by request kind; records the last request per kind."""

    def __init__(self, bodies: dict[str, object]) -> None:
        self._bodies = bodies
        self.last: dict[str, _Req] = {}
        self.raises: dict[str, Exception] = {}

    def get_response(self, req: _Req) -> _Resp:
        self.last[req._kind] = req
        if req._kind in self.raises:
            raise self.raises[req._kind]
        return _Resp(self._bodies.get(req._kind))


class _ServerException(Exception):
    def __init__(self, code: str, msg: str, http: int) -> None:
        super().__init__(code)
        self.error_code = code
        self.error_msg = msg
        self.http_status = http


@pytest.fixture
def fake_sdk(monkeypatch):
    """Register fake webull.* modules so the adapter's lazy imports resolve."""

    def reg(path: str, **attrs):
        mod = types.ModuleType(path)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, path, mod)
        return mod

    for pkg in ("webull", "webull.trade", "webull.trade.request", "webull.data", "webull.data.quotes"):
        reg(pkg)
    reg("webull.trade.request.place_order_request", PlaceOrderRequest=_make_req("place"))
    reg("webull.trade.request.get_order_detail_request", OrderDetailRequest=_make_req("detail"))
    reg("webull.trade.request.get_account_positions_request", AccountPositionsRequest=_make_req("positions"))
    reg("webull.trade.request.cancel_order_request", CancelOrderRequest=_make_req("cancel"))

    class _Instrument:
        body = [{"symbol": "AAPL", "instrument_id": "913256135"}]

        def __init__(self, client) -> None:
            self._client = client

        def get_instrument(self, symbols=None, category=None):
            return _Resp(type(self).body)

    reg("webull.data.quotes.instrument", Instrument=_Instrument)
    return None


def _adapter(client, **overrides) -> WebullBrokerAdapter:
    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter.settings = None
    adapter.region_id = "us"
    adapter.host = "api.webull.com"
    adapter.app_key = "ak"
    adapter.app_secret = "as"
    adapter.accounts_by_name = {"live:orb": WebullAccountConfig(account_id="ACC1")}
    adapter._client = client
    import threading

    adapter._client_lock = threading.Lock()
    adapter._instrument_cache = {}
    adapter._instrument_lock = threading.Lock()
    for k, v in overrides.items():
        setattr(adapter, k, v)
    return adapter


def _order(**kw) -> OrderRequest:
    base = dict(
        client_order_id="orb-AAPL-open-1",
        broker_account_name="live:orb",
        strategy_code="orb",
        symbol="AAPL",
        side="buy",
        intent_type="open",
        quantity=Decimal("5"),
        reason="ORB_RECLAIM",
        metadata={"order_type": "limit", "limit_price": "2.83"},
        order_type="limit",
    )
    base.update(kw)
    return OrderRequest(**base)


# --------------------------------------------------------------------------- tests
@pytest.mark.asyncio
async def test_submit_limit_order_accepted(fake_sdk) -> None:
    client = _FakeClient({"place": {"order_id": "WB-77"}})
    adapter = _adapter(client)
    reports = await adapter.submit_order(_order())
    assert len(reports) == 1
    rep = reports[0]
    assert rep.event_type == "accepted"
    assert rep.broker_order_id == "WB-77"
    placed = client.last["place"].values
    assert placed["account_id"] == "ACC1"
    assert placed["instrument_id"] == "913256135"  # resolved from symbol
    assert placed["side"] == "BUY"
    assert placed["order_type"] == "LIMIT"
    assert placed["qty"] == "5"
    assert placed["limit_price"] == "2.83"
    assert placed["tif"] == "DAY"


def test_round_to_tick_grid() -> None:
    # px >= $1 -> 0.01 tick; ORB emits 4-decimal prices that Webull rejects (417) off-grid.
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("1.6500"))) == "1.65"
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("1.6549"))) == "1.65"
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("1.6550"))) == "1.66"
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("12.3456"))) == "12.35"
    # sub-dollar -> 0.0001 tick (preserved)
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("0.5432"))) == "0.5432"
    assert str(WebullBrokerAdapter._round_to_tick(Decimal("0.54325"))) == "0.5433"


@pytest.mark.asyncio
async def test_submit_limit_order_rounds_offgrid_price(fake_sdk) -> None:
    client = _FakeClient({"place": {"order_id": "WB-78"}})
    adapter = _adapter(client)
    # ORB-style 4-decimal price on a >$1 stock must be snapped to the 0.01 grid.
    reports = await adapter.submit_order(
        _order(metadata={"order_type": "limit", "limit_price": "1.6500"})
    )
    assert reports[0].event_type == "accepted"
    assert client.last["place"].values["limit_price"] == "1.65"


def _stop_order(**kw) -> OrderRequest:
    """A native-stop-guard sell, mirroring the OMS _arm_or_rearm_native_stop_guard intent."""
    base = dict(
        side="sell",
        intent_type="close",
        reason="HARD_STOP_NATIVE_BACKUP",
        metadata={"order_type": "STOP", "stop_price": "1.6774", "native_stop_guard": "true"},
        order_type="STOP",
    )
    base.update(kw)
    return _order(**base)


@pytest.mark.asyncio
async def test_native_stop_map_off_sends_raw_stop(fake_sdk) -> None:
    # Default (flag off) = byte-identical to today: sends the literal "STOP" (Webull 417s it),
    # stop_price still set + tick-rounded. This is the pre-fix behaviour we must preserve.
    client = _FakeClient({"place": {"order_id": "WB-S"}})
    adapter = _adapter(client)  # _native_stop_map_enabled unset -> False
    await adapter.submit_order(_stop_order())
    placed = client.last["place"].values
    assert placed["order_type"] == "STOP"
    assert placed["stop_price"] == "1.68"  # 1.6774 snapped to the 0.01 grid


@pytest.mark.asyncio
async def test_native_stop_map_on_maps_stop_to_stop_loss(fake_sdk) -> None:
    # Flag on: STOP -> STOP_LOSS (Webull's accepted market-on-trigger enum), stop_price
    # carried + rounded, no limit_price, and RTH-only (market orders cannot be extended).
    client = _FakeClient({"place": {"order_id": "WB-S"}})
    adapter = _adapter(client, _native_stop_map_enabled=True)
    await adapter.submit_order(_stop_order())
    placed = client.last["place"].values
    assert placed["order_type"] == "STOP_LOSS"
    assert placed["stop_price"] == "1.68"
    assert placed["extended_hours_trading"] is False
    assert "limit_price" not in placed


@pytest.mark.asyncio
async def test_native_stop_map_on_maps_stop_limit_to_stop_loss_limit(fake_sdk) -> None:
    client = _FakeClient({"place": {"order_id": "WB-S"}})
    adapter = _adapter(client, _native_stop_map_enabled=True)
    await adapter.submit_order(_stop_order(
        metadata={"order_type": "STOP_LIMIT", "stop_price": "1.65", "limit_price": "1.64"},
        order_type="STOP_LIMIT",
    ))
    placed = client.last["place"].values
    assert placed["order_type"] == "STOP_LOSS_LIMIT"
    assert placed["stop_price"] == "1.65"
    assert placed["limit_price"] == "1.64"


@pytest.mark.asyncio
async def test_native_stop_map_on_leaves_limit_unchanged(fake_sdk) -> None:
    # The mapping only touches STOP/STOP_LIMIT; ordinary entries/exits are unaffected.
    client = _FakeClient({"place": {"order_id": "WB-L"}})
    adapter = _adapter(client, _native_stop_map_enabled=True)
    await adapter.submit_order(_order())  # a LIMIT buy
    placed = client.last["place"].values
    assert placed["order_type"] == "LIMIT"
    assert placed["limit_price"] == "2.83"


@pytest.mark.asyncio
async def test_submit_rejected_when_account_unmapped(fake_sdk) -> None:
    adapter = _adapter(_FakeClient({}))
    reports = await adapter.submit_order(_order(broker_account_name="paper:orb"))
    assert reports[0].event_type == "rejected"
    assert "no Webull account id" in reports[0].reason


@pytest.mark.asyncio
async def test_submit_rejected_on_server_exception(fake_sdk) -> None:
    client = _FakeClient({"place": {}})
    client.raises["place"] = _ServerException("INVALID_TOKEN", "permission denied", 401)
    adapter = _adapter(client)
    reports = await adapter.submit_order(_order())
    assert reports[0].event_type == "rejected"
    assert "INVALID_TOKEN" in reports[0].reason and "401" in reports[0].reason


@pytest.mark.asyncio
async def test_fetch_order_update_filled(fake_sdk) -> None:
    # Confirmed live shape (real AZI fills): order_id top-level; status/fill in items[0]
    # as order_status / filled_qty / filled_price (the field the trail-arm depends on).
    client = _FakeClient(
        {"detail": {"order_id": "WB-77", "items": [
            {"order_status": "FILLED", "filled_qty": "5", "filled_price": "2.85"}]}}
    )
    adapter = _adapter(client)
    rep = await adapter.fetch_order_update(_order())
    assert rep is not None
    assert rep.event_type == "filled"
    assert rep.filled_quantity == Decimal("5")
    assert rep.fill_price == Decimal("2.85")     # parsed from filled_price -> trail can arm
    assert rep.broker_order_id == "WB-77"
    assert rep.broker_fill_id == "WB-77:5"


@pytest.mark.asyncio
async def test_fetch_order_update_partial_and_failed(fake_sdk) -> None:
    adapter = _adapter(_FakeClient({"detail": {"order_id": "X", "items": [
        {"order_status": "PARTIAL_FILLED", "filled_qty": "2", "filled_price": "2.80"}]}}))
    rep = await adapter.fetch_order_update(_order())
    assert rep.event_type == "partially_filled"
    adapter2 = _adapter(_FakeClient({"detail": {"order_id": "X", "items": [{"order_status": "FAILED"}]}}))
    rep2 = await adapter2.fetch_order_update(_order())
    assert rep2.event_type == "rejected"


@pytest.mark.asyncio
async def test_fetch_order_update_parses_requests_response(fake_sdk) -> None:
    # Live calls return a requests.Response (body via .json(), no .body) — the bug that
    # made NO live response parse. _body must fall back to .json().
    class _C:
        def get_response(self, req):
            return _JsonResp({"order_id": "WB-9", "items": [
                {"order_status": "FILLED", "filled_qty": "5", "filled_price": "3.10"}]})

    rep = await _adapter(_C()).fetch_order_update(_order())
    assert rep.event_type == "filled"
    assert rep.fill_price == Decimal("3.10")
    assert rep.broker_order_id == "WB-9"


@pytest.mark.asyncio
async def test_list_positions_maps_holdings(fake_sdk) -> None:
    client = _FakeClient(
        {
            "positions": {
                "has_next": False,
                "holdings": [
                    {"symbol": "AAPL", "quantity": "5", "cost_price": "2.80", "market_value": "14.25"},
                    {"symbol": "ZZZZ", "quantity": "0"},  # flat -> skipped
                ],
            }
        }
    )
    adapter = _adapter(client)
    snaps = await adapter.list_account_positions("live:orb")
    assert len(snaps) == 1
    assert snaps[0].symbol == "AAPL"
    assert snaps[0].quantity == Decimal("5")
    assert snaps[0].average_price == Decimal("2.80")
    assert snaps[0].market_value == Decimal("14.25")


@pytest.mark.asyncio
async def test_cancel_intent(fake_sdk) -> None:
    client = _FakeClient({"cancel": {}})
    adapter = _adapter(client)
    reports = await adapter.submit_order(_order(intent_type="cancel", side="sell"))
    assert reports[0].event_type == "cancelled"
    assert client.last["cancel"].values["client_order_id"] == "orb-AAPL-open-1"


@pytest.mark.asyncio
async def test_instrument_cache_resolves_once(fake_sdk) -> None:
    client = _FakeClient({"place": {"order_id": "1"}})
    adapter = _adapter(client)
    await adapter.submit_order(_order())
    assert adapter._instrument_cache == {"AAPL": "913256135"}


def test_real_constructor_normalizes_host() -> None:
    # Exercises __init__ (not __new__) so missing-method regressions are caught here.
    from project_mai_tai.settings import Settings

    adapter = WebullBrokerAdapter(Settings(webull_base_url="https://api.webull.com/"))
    assert adapter.host == "api.webull.com"
    assert WebullBrokerAdapter._normalize_host(None) == ""
    assert WebullBrokerAdapter._normalize_host("api.webull.com") == "api.webull.com"


def test_configured_webull_accounts_empty_without_account_id() -> None:
    from project_mai_tai.settings import Settings

    assert configured_empty(Settings(webull_account_id=None)) == {}


def configured_empty(settings):
    from project_mai_tai.broker_adapters.webull import configured_webull_accounts

    return configured_webull_accounts(settings)
