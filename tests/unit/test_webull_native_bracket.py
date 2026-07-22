"""Webull native OCO combo bracket (v3 MASTER + STOP_PROFIT + STOP_LOSS) — Phase 1 write side.

The shape asserted here is from Webull's OWN published SDK sample
(webull-inc/webull-openapi-python-sdk `samples/trade/trade_client_v3.py`): a flat `new_orders`
list of legs tagged by `combo_type`, symbol+market+instrument_type per leg (wire value "EQUITY"),
numeric fields as strings, `support_trading_session:"CORE"` for RTH. These tests pin the exact
leg VALUES — a bracket whose legs are structurally plausible but priced/tagged wrong is a
real-money loss, and the account-level ACCEPTANCE + one-cancels-other behaviour are what Webull
STEP-1 validates live (this proves we build the documented shape, flag-gated + byte-identical off).
"""
from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter


# --------------------------------------------------------------------------- fakes
class _Resp:
    def __init__(self, body: object) -> None:
        self.body = body


class _Req:
    _kind = "?"

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            return lambda value: self.values.__setitem__(name[4:], value)
        raise AttributeError(name)


def _make_req(kind: str):
    return type(kind, (_Req,), {"_kind": kind})


class _FakeClient:
    def __init__(self, bodies: dict[str, object]) -> None:
        self._bodies = bodies
        self.last: dict[str, _Req] = {}

    def get_response(self, req: _Req) -> _Resp:
        self.last[req._kind] = req
        return _Resp(self._bodies.get(req._kind))


class _FakeOrderOpV3:
    """Records the combo calls made against the v3 operations façade."""

    calls: list[tuple[str, str, list, object]] = []

    def __init__(self, client: object) -> None:
        self._client = client

    def preview_order(self, account_id, new_orders, client_combo_order_id=None):
        type(self).calls.append(("preview", account_id, new_orders, client_combo_order_id))
        return _Resp({"ok": True})

    def place_order(self, account_id, new_orders, client_combo_order_id=None):
        type(self).calls.append(("place", account_id, new_orders, client_combo_order_id))
        return _Resp({"client_combo_order_id": client_combo_order_id})


@pytest.fixture
def fake_sdk(monkeypatch):
    """Register the fake webull.* modules the adapter lazily imports (v1 single-leg + v3 combo)."""

    def reg(path: str, **attrs):
        mod = types.ModuleType(path)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, path, mod)

    for pkg in (
        "webull", "webull.trade", "webull.trade.request", "webull.trade.trade",
        "webull.trade.trade.v3", "webull.data", "webull.data.quotes",
    ):
        reg(pkg)
    reg("webull.trade.request.place_order_request", PlaceOrderRequest=_make_req("place"))
    reg("webull.trade.trade.v3.order_opration_v3", OrderOperationV3=_FakeOrderOpV3)

    class _Instrument:
        def __init__(self, client) -> None: ...
        def get_instrument(self, symbols=None, category=None):
            return _Resp([{"symbol": "F", "instrument_id": "913256135"}])

    reg("webull.data.quotes.instrument", Instrument=_Instrument)
    _FakeOrderOpV3.calls = []
    return None


def _adapter(*, bracket_enabled: bool = True, client: object | None = None) -> WebullBrokerAdapter:
    adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    adapter.settings = types.SimpleNamespace(webull_native_bracket_enabled=bracket_enabled)
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
    adapter._native_stop_map_enabled = False
    return adapter


def _bracket_req(**meta) -> OrderRequest:
    metadata = {
        "bracket": "true",
        "bracket_entry_type": "LIMIT",
        "limit_price": "10.50",           # MASTER entry (marketable limit)
        "bracket_target_price": "11.50",  # STOP_PROFIT (+target)
        "bracket_stop_price": "10.00",    # STOP_LOSS (-protect)
    }
    metadata.update(meta)
    return OrderRequest(
        client_order_id="v2_webull-F-open-abc123",
        broker_account_name="live:orb",
        strategy_code="schwab_1m_v2",
        symbol="F",
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        reason="ENTRY_CW",
        order_type="limit",
        metadata=metadata,
    )


# --------------------------------------------------------------------------- _build_combo_payload
def test_combo_payload_matches_the_sdk_sample_shape() -> None:
    """Pin the exact 3-leg MASTER/STOP_PROFIT/STOP_LOSS shape from Webull's own SDK sample."""
    master, target, protect = _adapter()._build_combo_payload(_bracket_req())

    # every leg carries the common equity envelope
    for leg in (master, target, protect):
        assert leg["symbol"] == "F"
        assert leg["instrument_type"] == "EQUITY"   # wire value, NOT "STOCK"
        assert leg["market"] == "US"
        assert leg["quantity"] == "1"               # string, per the sample
        assert leg["entrust_type"] == "QTY"
        assert leg["time_in_force"] == "DAY"
        assert leg["support_trading_session"] == "CORE"   # RTH

    assert master["combo_type"] == "MASTER"
    assert master["side"] == "BUY"
    assert master["order_type"] == "LIMIT"
    assert master["limit_price"] == "10.50"

    assert target["combo_type"] == "STOP_PROFIT"
    assert target["side"] == "SELL"
    assert target["order_type"] == "LIMIT"
    assert target["limit_price"] == "11.50"

    assert protect["combo_type"] == "STOP_LOSS"
    assert protect["side"] == "SELL"
    assert protect["order_type"] == "STOP_LOSS"     # Webull stop enum, not literal "STOP"
    assert protect["stop_price"] == "10.00"
    assert "stop_price" not in target and "limit_price" not in protect


def test_market_master_carries_no_limit_price() -> None:
    """The live v2 CW entry is a MARKET order -> MASTER is MARKET with no price; exits unchanged."""
    master, target, protect = _adapter()._build_combo_payload(
        _bracket_req(bracket_entry_type="MARKET")
    )
    assert master["order_type"] == "MARKET"
    assert "limit_price" not in master
    assert target["combo_type"] == "STOP_PROFIT" and protect["combo_type"] == "STOP_LOSS"


def test_buy_stop_master_is_rejected() -> None:
    """Fork A: a buy-STOP master rejects on Webull -> the builder must refuse to emit that shape."""
    with pytest.raises(RuntimeError, match="LIMIT or MARKET"):
        _adapter()._build_combo_payload(_bracket_req(bracket_entry_type="STOP"))


def test_missing_exit_metadata_is_never_a_half_built_bracket() -> None:
    """An entry with no attached exits is the naked-position shape this structure exists to kill."""
    req = _bracket_req()
    req.metadata.pop("bracket_stop_price")
    with pytest.raises(RuntimeError, match="bracket_stop_price"):
        _adapter()._build_combo_payload(req)


def test_combo_prices_snap_to_the_webull_tick_grid() -> None:
    """Off-grid prices 417 on Webull. >=$1 -> 0.01 tick; sub-dollar -> 0.0001 (a sub-$1 name)."""
    over = _adapter()._build_combo_payload(
        _bracket_req(limit_price="10.505", bracket_target_price="11.994", bracket_stop_price="10.006")
    )
    assert over[0]["limit_price"] == "10.51"   # 10.505 -> 10.51 (half-up)
    assert over[1]["limit_price"] == "11.99"
    assert over[2]["stop_price"] == "10.01"
    sub = _adapter()._build_combo_payload(
        _bracket_req(limit_price="0.7250", bracket_target_price="0.7400", bracket_stop_price="0.6800")
    )
    assert sub[0]["limit_price"] == "0.7250"   # sub-$1 keeps 4 decimals
    assert sub[2]["stop_price"] == "0.6800"


def test_leg_client_order_ids_are_unique_and_within_the_40_char_cap() -> None:
    """Webull caps coid at 40 and 417s a reused id -> 3 distinct legs, each <=40."""
    coids = [leg["client_order_id"] for leg in _adapter()._build_combo_payload(_bracket_req())]
    assert len(set(coids)) == 3
    assert all(len(c) <= 40 for c in coids)
    # even a base already longer than the cap stays <= 40 for both the group id and each leg
    long_base = "v2_webull-SOMEVERYLONGSYMBOL-open-2026-07-22-xyz"  # > 40 chars
    assert len(long_base) > 40
    assert len(WebullBrokerAdapter._combo_leg_coid(long_base, "")) <= 40
    assert len(WebullBrokerAdapter._combo_leg_coid(long_base, "M")) <= 40


# --------------------------------------------------------------------------- _is_bracket_request
def test_is_bracket_request_requires_both_flag_and_metadata() -> None:
    assert _adapter(bracket_enabled=True)._is_bracket_request(_bracket_req()) is True
    # flag off -> never a bracket, even with the metadata (single-leg path stays byte-identical)
    assert _adapter(bracket_enabled=False)._is_bracket_request(_bracket_req()) is False
    # flag on but no bracket metadata -> single-leg
    req = _bracket_req()
    req.metadata.pop("bracket")
    assert _adapter(bracket_enabled=True)._is_bracket_request(req) is False


# --------------------------------------------------------------------------- dispatch + preview
@pytest.mark.asyncio
async def test_submit_places_a_v3_combo_when_flag_on(fake_sdk) -> None:
    """Flag on + bracket metadata -> one v3 place_order of the 3-leg combo; returns accepted."""
    adapter = _adapter(bracket_enabled=True, client=_FakeClient({}))
    reports = await adapter.submit_order(_bracket_req())
    assert len(reports) == 1 and reports[0].event_type == "accepted"
    assert [c[0] for c in _FakeOrderOpV3.calls] == ["place"]
    _kind, account_id, new_orders, combo_id = _FakeOrderOpV3.calls[0]
    assert account_id == "ACC1"
    assert [leg["combo_type"] for leg in new_orders] == ["MASTER", "STOP_PROFIT", "STOP_LOSS"]
    assert combo_id and len(combo_id) <= 40
    assert reports[0].broker_order_id == combo_id   # place echoes the group id back


@pytest.mark.asyncio
async def test_submit_stays_single_leg_when_flag_off(fake_sdk) -> None:
    """Flag off + bracket metadata -> the v1 single-leg path (NO v3 combo call) = byte-identical."""
    adapter = _adapter(bracket_enabled=False, client=_FakeClient({"place": {"order_id": "WB-1"}}))
    reports = await adapter.submit_order(_bracket_req())
    assert len(reports) == 1 and reports[0].event_type == "accepted"
    assert _FakeOrderOpV3.calls == []                    # no combo path taken
    assert adapter._client.last["place"].values["order_type"] == "LIMIT"  # single v1 leg


@pytest.mark.asyncio
async def test_preview_bracket_validates_without_placing(fake_sdk) -> None:
    """STEP-1 item 0: preview_order is the only call, and no place_order fires."""
    adapter = _adapter(bracket_enabled=True, client=_FakeClient({}))
    status, body = await adapter.preview_bracket_order(_bracket_req())
    assert status == 200
    assert [c[0] for c in _FakeOrderOpV3.calls] == ["preview"]   # preview ONLY, never place
    _kind, account_id, new_orders, _combo = _FakeOrderOpV3.calls[0]
    assert [leg["combo_type"] for leg in new_orders] == ["MASTER", "STOP_PROFIT", "STOP_LOSS"]
