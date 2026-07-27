"""Unit tests for the Webull live broker adapter.

The Webull SDK is imported lazily inside the adapter and is NOT a CI dependency, so we
register fake ``webull.*`` modules in ``sys.modules`` and inject a fake client. Tests cover
request construction + response mapping against the shapes the on-box probe confirmed (reads)
and the defensive parsing for the order shapes still to be confirmed by a funded test order.
"""
from __future__ import annotations

import asyncio
import sys
import types
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import (
    WebullAccountConfig,
    WebullBrokerAdapter,
    WebullPositionsUnavailable,
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
        self.calls: dict[str, int] = {}  # per-kind get_response count (throttle/backoff proof)

    def get_response(self, req: _Req) -> _Resp:
        self.last[req._kind] = req
        self.calls[req._kind] = self.calls.get(req._kind, 0) + 1
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
    # Position-sync throttle/backoff state (set by __init__ in production; here for __new__).
    adapter._positions_throttle_secs = 10.0
    adapter._positions_backoff_base_secs = 5.0
    adapter._positions_backoff_max_secs = 60.0
    adapter._positions_lock = threading.Lock()
    adapter._positions_cache = {}
    adapter._positions_backoff_until = {}
    adapter._positions_backoff_secs = {}
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


@pytest.mark.parametrize(
    "session,otype,expected",
    [
        ("am", "LIMIT", (True, True)),           # pre-market limit -> EH
        ("pm", "LIMIT", (True, True)),           # post-market limit -> EH
        ("pre", "LIMIT", (True, True)),
        ("post", "LIMIT", (True, True)),
        ("premarket", "LIMIT", (True, True)),
        ("aftermarket", "LIMIT", (True, True)),
        ("STOP_LOSS_LIMIT", "STOP_LOSS_LIMIT", (False, False)),  # session token unknown -> RTH
        ("am", "MARKET", (False, True)),         # EH requested but MARKET -> RTH-only + warning
        ("pm", "STOP_LOSS", (False, True)),      # EH requested but STOP -> RTH-only + warning
        ("", "LIMIT", (False, False)),           # no session -> RTH
        ("rth", "LIMIT", (False, False)),        # unknown token -> RTH
    ],
)
def test_extended_hours_flag(session, otype, expected) -> None:
    req = OrderRequest(
        client_order_id="c", broker_account_name="live:orb", strategy_code="s", symbol="F",
        side="buy", intent_type="open", quantity=Decimal("1"), reason="t",
        metadata={"session": session}, order_type="limit", time_in_force="day",
    )
    assert WebullBrokerAdapter._extended_hours_flag(req, otype) == expected


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
async def test_fetch_order_update_reported_at_is_broker_fill_time(fake_sdk) -> None:
    # Real broker fill payload carries `last_filled_time` (broker-stamped, ms). reported_at
    # must be THAT time (Schwab closeTime equivalent), not our poll/receive time, so the
    # Webull-vs-Schwab fill-latency A/B is a real measurement. Both broker times land in metadata.
    from datetime import UTC, datetime
    client = _FakeClient({"detail": {"order_id": "WB-9", "items": [{
        "order_status": "FILLED", "filled_qty": "5", "filled_price": "5.2674",
        "last_filled_time": "2026-07-10 13:31:00.394+0000",
        "place_time": "2026-07-10 13:31:00.352+0000",
    }]}})
    rep = await _adapter(client).fetch_order_update(_order())
    assert rep is not None and rep.event_type == "filled"
    assert rep.reported_at == datetime(2026, 7, 10, 13, 31, 0, 394000, tzinfo=UTC)
    assert rep.metadata["webull_broker_filled_time"] == "2026-07-10 13:31:00.394+0000"
    assert rep.metadata["webull_broker_place_time"] == "2026-07-10 13:31:00.352+0000"


@pytest.mark.asyncio
async def test_fetch_order_update_missing_fill_time_falls_back(fake_sdk) -> None:
    # No last_filled_time -> reported_at falls back to now() (recent), report still valid
    # (and the adapter logs an UPPER-BOUND warning).
    from datetime import UTC, datetime
    before = datetime.now(UTC)
    client = _FakeClient({"detail": {"order_id": "WB-10", "items": [
        {"order_status": "FILLED", "filled_qty": "5", "filled_price": "2.85"}]}})
    rep = await _adapter(client).fetch_order_update(_order())
    assert rep is not None and rep.event_type == "filled"
    assert rep.reported_at is not None and rep.reported_at >= before
    assert "webull_broker_filled_time" not in rep.metadata


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


def _positions_body(qty: str = "5"):
    return {
        "positions": {
            "has_next": False,
            "holdings": [{"symbol": "AAPL", "quantity": qty, "cost_price": "2.80"}],
        }
    }


@pytest.mark.asyncio
async def test_positions_throttle_coalesces_rapid_calls(fake_sdk) -> None:
    # N rapid calls within THROTTLE_SECS -> exactly ONE Webull get_response; the rest are cached.
    client = _FakeClient(_positions_body())
    adapter = _adapter(client)  # default throttle = 10s
    for _ in range(6):
        snaps = await adapter.list_account_positions("live:orb")
        assert len(snaps) == 1 and snaps[0].symbol == "AAPL"
    assert client.calls.get("positions") == 1


@pytest.mark.asyncio
async def test_positions_throttle_disabled_hits_webull_each_call(fake_sdk) -> None:
    # MUTATION: disable the throttle (secs=0) -> every call hits Webull (the rapid-call test
    # above goes red without the coalescing). Pins that the throttle VALUE is what caps the rate.
    client = _FakeClient(_positions_body())
    adapter = _adapter(client, _positions_throttle_secs=0.0)
    for _ in range(6):
        await adapter.list_account_positions("live:orb")
    assert client.calls.get("positions") == 6


@pytest.mark.asyncio
async def test_positions_backoff_on_429_serves_cache_then_resumes(fake_sdk) -> None:
    # Throttle disabled to isolate the 429 backoff; tiny backoff window for a deterministic resume.
    client = _FakeClient(_positions_body())
    adapter = _adapter(
        client,
        _positions_throttle_secs=0.0,
        _positions_backoff_base_secs=0.05,
        _positions_backoff_max_secs=0.05,
    )
    # 1) Prime the cache with a successful (HELD) read.
    assert len(await adapter.list_account_positions("live:orb")) == 1
    assert client.calls["positions"] == 1
    # 2) A 429 -> back off AND serve the last known-good snapshot (HELD, NOT flat).
    client.raises["positions"] = _ServerException("TOO_MANY_REQUESTS", "rate", 429)
    snaps = await adapter.list_account_positions("live:orb")
    assert len(snaps) == 1 and snaps[0].symbol == "AAPL"  # unknown != flat: still HELD
    assert client.calls["positions"] == 2  # this call hit Webull and got the 429
    # 3) Subsequent calls WITHIN the backoff window do NOT hit Webull.
    assert len(await adapter.list_account_positions("live:orb")) == 1
    assert client.calls["positions"] == 2  # unchanged -> no Webull hit during backoff
    # 4) After the window expires, one call resumes (and the recovered read refreshes the cache).
    client.raises.pop("positions", None)
    await asyncio.sleep(0.12)
    assert len(await adapter.list_account_positions("live:orb")) == 1
    assert client.calls["positions"] == 3


@pytest.mark.asyncio
async def test_positions_429_without_cache_raises_never_flat(fake_sdk) -> None:
    # ⚠ "unknown != flat": a 429 with NO prior snapshot RAISES (callers -> UNKNOWN, keep
    # protection) instead of returning [] (which classifies as FLAT_INFERRED and could clear a
    # live stop / drop a collision guard).
    client = _FakeClient({})
    client.raises["positions"] = _ServerException("TOO_MANY_REQUESTS", "rate", 429)
    adapter = _adapter(client, _positions_throttle_secs=0.0)
    with pytest.raises(WebullPositionsUnavailable):
        await adapter.list_account_positions("live:orb")


@pytest.mark.asyncio
async def test_positions_non_ratelimit_error_returns_empty(fake_sdk) -> None:
    # A NON-429 error preserves the prior behaviour (log + empty list) so flat-vs-unknown
    # semantics for other failures are unchanged by this fix.
    client = _FakeClient({})
    client.raises["positions"] = _ServerException("ILLEGAL_PARAMETER", "bad", 417)
    adapter = _adapter(client, _positions_throttle_secs=0.0)
    assert await adapter.list_account_positions("live:orb") == []


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


# ------------------------------------------------- combo-bracket status polling (2026-07-27)
# LIVE DEFECT: `_place_combo_bracket` places each leg under a SUFFIXED coid
# (`_combo_leg_coid(base, "M"/"T"/"S")`), but the status poll asked for the BARE base. Webull
# answered 417 ORDER_NOT_FOUND on every poll (236 times on LGHL alone), so the order never left
# `accepted`: no fill recorded, no managed position, and four REAL filled Webull positions
# (LGHL/QBTX/BIYA/ENTX) were invisible to v2, which reported positions=[] and daily_pnl=0.0.

def _bracket_adapter(client):
    import types as _types
    adapter = _adapter(client)
    adapter.settings = _types.SimpleNamespace(webull_native_bracket_enabled=True)
    return adapter


_MASTER_FILL = {"order_id": "WB-COMBO-1", "combo_type": "MASTER", "items": [
    {"order_status": "FILLED", "filled_qty": "1", "filled_price": "8.94"}]}


@pytest.mark.asyncio
async def test_combo_bracket_status_is_polled_under_the_master_coid(fake_sdk) -> None:
    """THE REGRESSION: a bracket order must be looked up as `<base>M`, not `<base>`."""
    client = _FakeClient({"detail": _MASTER_FILL})
    rep = await _bracket_adapter(client).fetch_order_update(
        _order(client_order_id="schwab_1m_v2-QBTX-open-d730d4d11afd",
               metadata={"order_type": "market", "bracket": "true"})
    )
    assert client.last["detail"].values["client_order_id"] == "schwab_1m_v2-QBTX-open-d730d4d11afdM"
    assert client.calls["detail"] == 1          # no wasted probe on the happy path
    assert rep is not None and rep.event_type == "filled"
    assert rep.fill_price == Decimal("8.94")    # the fill the OMS needs to arm/close
    # the report must carry the BARE id -- that is the row the OMS reconciles against
    assert rep.client_order_id == "schwab_1m_v2-QBTX-open-d730d4d11afd"


@pytest.mark.asyncio
async def test_single_leg_status_still_polls_the_bare_coid(fake_sdk) -> None:
    """No-regression: with the bracket flag off the lookup is byte-identical to before."""
    client = _FakeClient({"detail": _MASTER_FILL})
    rep = await _adapter(client).fetch_order_update(_order())     # settings=None -> flag off
    assert client.last["detail"].values["client_order_id"] == "orb-AAPL-open-1"
    assert client.calls["detail"] == 1
    assert rep is not None and rep.event_type == "filled"


class _FlakyDetailClient(_FakeClient):
    """ORDER_NOT_FOUND on the first coid, real payload on the second."""

    def __init__(self, body, fail_first_n: int = 1, exc: Exception | None = None) -> None:
        super().__init__({"detail": body})
        self._left = fail_first_n
        self._exc = exc or _ServerException("ORDER_NOT_FOUND", "ORDER_NOT_FOUND", 417)
        self.seen: list[str] = []

    def get_response(self, req):
        if req._kind == "detail":
            self.seen.append(req.values.get("client_order_id"))
            self.calls["detail"] = self.calls.get("detail", 0) + 1
            self.last["detail"] = req
            if self._left > 0:
                self._left -= 1
                raise self._exc
            return _Resp(self._bodies.get("detail"))
        return super().get_response(req)


@pytest.mark.asyncio
async def test_order_placed_before_the_flag_flipped_still_resolves(fake_sdk) -> None:
    """A combo placed while the flag was ON, polled after it was turned OFF: the bare lookup
    404s and the MASTER fallback finds it. Without this the order goes dark forever."""
    client = _FlakyDetailClient(_MASTER_FILL)
    rep = await _adapter(client).fetch_order_update(          # flag OFF -> bare first
        _order(client_order_id="schwab_1m_v2-QBTX-open-d730d4d11afd")
    )
    assert client.seen == ["schwab_1m_v2-QBTX-open-d730d4d11afd",
                           "schwab_1m_v2-QBTX-open-d730d4d11afdM"]
    assert rep is not None and rep.event_type == "filled"


@pytest.mark.asyncio
async def test_a_non_not_found_error_is_never_retried(fake_sdk) -> None:
    """An auth/transport failure must propagate on the FIRST coid -- retrying it under the other
    shape would double every failing call and mask the real error."""
    client = _FlakyDetailClient(_MASTER_FILL, fail_first_n=99,
                                exc=_ServerException("INVALID_TOKEN", "permission denied", 401))
    rep = await _bracket_adapter(client).fetch_order_update(
        _order(metadata={"order_type": "market", "bracket": "true"})
    )
    assert rep is None                 # fetch_order_update logs + returns None
    assert client.calls["detail"] == 1  # exactly one attempt, no fallback probe


# --------------------------------------------- fill-anchored bracket realign (2026-07-27)
# The combo is placed atomically, so BOTH exit legs are priced off the pre-trade REFERENCE before
# the master has filled. The Webull leg enters at MARKET on the ATR cross -- exactly where slippage
# lives -- so the realised bracket drifts off spec. Measured live across 8 fan-out trades: the "-5%"
# stop actually ranged -3.85%..-5.83%, the "+2%" target +1.67%..+3.18%. BIYA 12:51 filled 0.37%
# worse than reference, putting the stop at -5.34% of the fill; it slipped to -5.83% realised.

def _realign_adapter(client, *, enabled=True):
    import types as _types
    adapter = _adapter(client)
    adapter.settings = _types.SimpleNamespace(
        webull_native_bracket_enabled=True,
        webull_bracket_realign_on_fill_enabled=enabled,
    )
    adapter._bracket_realigned = set()
    return adapter


def _biya_order(**kw):
    """The real BIYA 12:51 trade: ref 4.1050, +2%/-5% -> 4.1871/3.8998, master filled 4.120."""
    md = {
        "order_type": "market", "bracket": "true", "bracket_entry_type": "MARKET",
        "reference_price": "4.1050", "bracket_target_price": "4.1871",
        "bracket_stop_price": "3.8998",
    }
    md.update(kw.pop("metadata", {}))
    return _order(client_order_id="schwab_1m_v2-BIYA-open-aaaa", symbol="BIYA",
                  quantity=Decimal("1"), metadata=md, **kw)


class _ReplaceCapturingClient(_FakeClient):
    """Records replace_order calls made through OrderOperationV3."""

    def __init__(self, detail_body):
        super().__init__({"detail": detail_body})
        self.replaced = []


def _reg_op(monkeypatch, op_cls):
    """Register the v3 order-operation module the adapter lazily imports (the fake_sdk fixture
    stubs webull.trade.* but not this one)."""
    for pkg in ("webull.trade.trade", "webull.trade.trade.v3"):
        monkeypatch.setitem(sys.modules, pkg, types.ModuleType(pkg))
    mod = types.ModuleType("webull.trade.trade.v3.order_opration_v3")
    mod.OrderOperationV3 = op_cls
    monkeypatch.setitem(sys.modules, "webull.trade.trade.v3.order_opration_v3", mod)


def _patch_replace(monkeypatch, client):
    class _Op:
        def __init__(self, _c):
            pass

        def replace_order(self, account_id, modify_orders, client_combo_order_id=None):
            client.replaced.append((account_id, modify_orders, client_combo_order_id))
            return _Resp({})

    _reg_op(monkeypatch, _Op)


_FILL_412 = {"order_id": "WB-B1", "combo_type": "MASTER", "items": [
    {"order_status": "FILLED", "filled_qty": "1", "filled_price": "4.120"}]}


@pytest.mark.asyncio
async def test_bracket_is_repriced_off_the_actual_fill(fake_sdk, monkeypatch) -> None:
    """THE FIX: ratios are preserved against the FILL, not the reference."""
    client = _ReplaceCapturingClient(_FILL_412)
    _patch_replace(monkeypatch, client)
    rep = await _realign_adapter(client).fetch_order_update(_biya_order())

    assert rep is not None and rep.event_type == "filled"   # the status read still works
    assert len(client.replaced) == 1
    _acct, legs, combo_id = client.replaced[0]
    by_type = {leg["combo_type"]: leg for leg in legs}
    # 4.120 * (4.1871/4.1050) = 4.2024 -> tick 4.20 ; 4.120 * (3.8998/4.1050) = 3.9140 -> 3.91
    assert by_type["STOP_PROFIT"]["limit_price"] == "4.20"   # was 4.19 = only +1.70% of the fill
    assert by_type["STOP_LOSS"]["stop_price"] == "3.91"      # was 3.90 = -5.34% of the fill
    assert combo_id == "schwab_1m_v2-BIYA-open-aaaa"
    # and the corrected legs really are +2% / -5% OF THE FILL
    assert float(by_type["STOP_PROFIT"]["limit_price"]) / 4.120 == pytest.approx(1.02, abs=0.002)
    assert float(by_type["STOP_LOSS"]["stop_price"]) / 4.120 == pytest.approx(0.95, abs=0.002)


@pytest.mark.asyncio
async def test_no_realign_when_the_fill_matches_the_reference(fake_sdk, monkeypatch) -> None:
    """LGHL 10:23 filled exactly at reference — spending a broker write there is pure noise."""
    client = _ReplaceCapturingClient({"order_id": "WB-L", "items": [
        {"order_status": "FILLED", "filled_qty": "1", "filled_price": "4.1050"}]})
    _patch_replace(monkeypatch, client)
    await _realign_adapter(client).fetch_order_update(_biya_order())
    assert client.replaced == []


@pytest.mark.asyncio
async def test_realign_is_idempotent_across_polls(fake_sdk, monkeypatch) -> None:
    """The OMS re-polls a filled order; the bracket must be re-priced ONCE."""
    client = _ReplaceCapturingClient(_FILL_412)
    _patch_replace(monkeypatch, client)
    adapter = _realign_adapter(client)
    for _ in range(4):
        await adapter.fetch_order_update(_biya_order())
    assert len(client.replaced) == 1


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical(fake_sdk, monkeypatch) -> None:
    """Deploys inert: no broker write until the attended check enables it."""
    client = _ReplaceCapturingClient(_FILL_412)
    _patch_replace(monkeypatch, client)
    rep = await _realign_adapter(client, enabled=False).fetch_order_update(_biya_order())
    assert rep is not None and rep.event_type == "filled"
    assert client.replaced == []


@pytest.mark.asyncio
async def test_a_failed_realign_never_breaks_the_fill_report(fake_sdk, monkeypatch) -> None:
    """PROTECTION > PRECISION. If replace_order fails the ORIGINAL bracket still guards the
    position, and the fill must still reach the OMS — losing the fill report would be far worse
    than an imperfectly-priced stop."""
    client = _ReplaceCapturingClient(_FILL_412)

    class _BoomOp:
        def __init__(self, _c):
            pass

        def replace_order(self, *a, **k):
            raise _ServerException("ORDER_NOT_FOUND", "already filled", 417)

    _reg_op(monkeypatch, _BoomOp)
    rep = await _realign_adapter(client).fetch_order_update(_biya_order())
    assert rep is not None
    assert rep.event_type == "filled"
    assert rep.fill_price == Decimal("4.120")


@pytest.mark.asyncio
async def test_non_bracket_orders_are_untouched(fake_sdk, monkeypatch) -> None:
    client = _ReplaceCapturingClient(_FILL_412)
    _patch_replace(monkeypatch, client)
    await _realign_adapter(client).fetch_order_update(_order())   # no bracket metadata
    assert client.replaced == []


# ------------------------------------------- OCO exit-fill capture (2026-07-27)
# The exit executes on a combo child leg the OMS never placed, so nothing books a fill for it.
# Since the native bracket went live, NO exit fill has been recorded and the operator's
# completed-trades table and P&L have been blank.

_BASE = "schwab_1m_v2-BIYA-open-d364cebd2145"


class _LegClient(_FakeClient):
    """Answers order-detail per SUFFIXED coid; records the exact call order."""

    def __init__(self, by_coid: dict):
        super().__init__({})
        self.by_coid = by_coid
        self.seen: list[str] = []

    def get_response(self, req):
        if req._kind == "detail":
            coid = req.values.get("client_order_id")
            self.seen.append(coid)
            self.last["detail"] = req
            if coid not in self.by_coid:
                raise _ServerException("ORDER_NOT_FOUND", "ORDER_NOT_FOUND", 417)
            return _Resp(self.by_coid[coid])
        return super().get_response(req)


def _leg(status, price, qty="1", when="2026-07-27 15:36:30.000+0000", oid="WB-X"):
    return {"order_id": oid, "items": [{
        "order_status": status, "filled_qty": qty, "filled_price": price,
        "last_filled_time": when,
    }]}


@pytest.mark.asyncio
async def test_exit_fill_reads_the_take_profit_leg(fake_sdk) -> None:
    """THE FIX: the real BIYA exit — STOP_PROFIT filled @3.9300."""
    client = _LegClient({_BASE + "T": _leg("FILLED", "3.9300", oid="WB-T1")})
    got = await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", _BASE)
    assert got is not None
    assert got["price"] == Decimal("3.9300")
    assert got["quantity"] == Decimal("1")
    assert got["broker_order_id"] == "WB-T1"


@pytest.mark.asyncio
async def test_only_one_detail_call_when_the_target_filled(fake_sdk) -> None:
    """⛔ RATE LIMIT. In an OCO exactly one leg can fill, so the second lookup is waste. Probing
    4 symbols x 2 legs back-to-back live returned 1 result then three 417/TOO_MANY_REQUESTS."""
    client = _LegClient({_BASE + "T": _leg("FILLED", "3.9300")})
    await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", _BASE)
    assert client.seen == [_BASE + "T"]          # S is never queried


@pytest.mark.asyncio
async def test_falls_back_to_the_stop_leg(fake_sdk) -> None:
    """The real LGHL exit — target cancelled, STOP_LOSS filled @1.360."""
    client = _LegClient({
        _BASE + "T": _leg("CANCELLED", None, qty="0"),
        _BASE + "S": _leg("FILLED", "1.3600", oid="WB-S1"),
    })
    got = await _adapter(client).fetch_oco_exit_fill("live:orb", "LGHL", _BASE)
    assert got["price"] == Decimal("1.3600")
    assert got["broker_order_id"] == "WB-S1"
    assert client.seen == [_BASE + "T", _BASE + "S"]


@pytest.mark.asyncio
async def test_both_legs_cancelled_yields_nothing(fake_sdk) -> None:
    """The real QBTX case: hand-closed, so both legs cancelled. Returning an exit here would
    invent a fill that never happened."""
    client = _LegClient({
        _BASE + "T": _leg("CANCELLED", None, qty="0"),
        _BASE + "S": _leg("CANCELLED", None, qty="0"),
    })
    assert await _adapter(client).fetch_oco_exit_fill("live:orb", "QBTX", _BASE) is None


@pytest.mark.asyncio
async def test_a_filled_leg_priced_zero_is_never_booked(fake_sdk) -> None:
    """Same trap as Schwab: a $0 exit would report the trade as -100%."""
    client = _LegClient({_BASE + "T": _leg("FILLED", "0")})
    assert await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", _BASE) is None


@pytest.mark.asyncio
async def test_missing_base_coid_makes_no_broker_call(fake_sdk) -> None:
    """Without the entry's coid there is nothing to address; must not fire a blind request."""
    client = _LegClient({})
    assert await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", "") is None
    assert client.seen == []


@pytest.mark.asyncio
async def test_order_not_found_on_a_leg_is_skipped_not_raised(fake_sdk) -> None:
    """A single-leg (non-combo) order has no T/S legs at all — that is absence, not failure."""
    client = _LegClient({_BASE + "S": _leg("FILLED", "2.5000")})   # T raises ORDER_NOT_FOUND
    got = await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", _BASE)
    assert got is not None and got["price"] == Decimal("2.5000")


@pytest.mark.asyncio
async def test_status_filter_alone_rejects_a_cancelled_leg_carrying_a_real_price(fake_sdk) -> None:
    """ISOLATES the status filter. A leg that PARTIALLY filled and was then cancelled reports
    CANCELLED with a real qty and price. Status must exclude it on its own -- treating a partial
    as the exit would book the wrong size and close a position that is still partly HELD.
    (Without this the price/qty filter masks the status filter and a regression goes unseen.)"""
    client = _LegClient({
        _BASE + "T": _leg("CANCELLED", "3.9300", qty="1"),   # real price, real qty, NOT filled
        _BASE + "S": _leg("CANCELLED", None, qty="0"),
    })
    assert await _adapter(client).fetch_oco_exit_fill("live:orb", "BIYA", _BASE) is None
