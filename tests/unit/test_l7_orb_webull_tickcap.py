"""L7 author-only controls: synthetic incident inputs, never native/live proof."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from project_mai_tai.strategy_core.orb_pastlevel import assert_same_selection, round_buy, select_entry
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
from tests.unit.test_orbroute1_oms_chain import setup as setup, WEBULL, SCHWAB
from tests.unit.test_webull_adapter import _FakeClient, _adapter, _instrument_stub, fake_sdk as fake_sdk


INCIDENT = datetime(2026, 10, 22, 13, 28, 3, 452000, tzinfo=UTC)


def quote(ask, *, at=INCIDENT):
    return None if ask is None else {"ask": ask, "received_at": at}


def selection(level="1.65", ask=None, *, now=INCIDENT):
    return select_entry(Decimal(level), quote(ask, at=now), now,
                        max_age_ms=1000, provider="webull")


def incident_event(setup, monkeypatch, ask):
    service, factory, _broker = setup
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: INCIDENT)
    store = CountryStore(factory)
    store.put(CountryRecord("STAK", "0000000001", "A6", "A6", False,
        INCIDENT - timedelta(days=1), "READY", INCIDENT.date()))
    router = CountryRouter(store)
    router._records = store.load()
    routed = select_orb_country_route(router, "STAK", INCIDENT,
        schwab_account=SCHWAB, webull_account=WEBULL)
    event = build_orb_schwab_open_intent(service.settings, "STAK", Decimal("1.65"), selection=routed)
    service._latest_quotes_by_symbol = {} if ask is None else {"STAK": quote(ask)}
    return event


@pytest.mark.parametrize("ask", ["1.65", "1.64", None])
def test_actual_stak_oms_path_repairs_only_wire_cap(setup, monkeypatch, ask):
    service, _factory, broker = setup
    event = incident_event(setup, monkeypatch, ask)
    original = dict(event.payload.metadata)
    assert original["stop_price"] == original["limit_price"] == "1.65"
    result = asyncio.run(service.process_trade_intent(event))
    assert result[-1].payload.status == "accepted"
    assert len(broker.sent) == 1 and broker.previews == []
    request = broker.sent[0]
    assert (request.symbol, request.broker_account_name, request.quantity) == ("STAK", WEBULL, Decimal(10))
    assert request.order_type == request.metadata["order_type"] == "STOP_LIMIT"
    assert Decimal(request.metadata["stop_price"]) == Decimal("1.65")
    assert Decimal(request.metadata["limit_price"]) == Decimal("1.66")
    assert request.metadata["orb_target_price"] == original["orb_target_price"]
    assert request.metadata["orb_protect_price"] == original["orb_protect_price"]
    assert request.metadata["orb_exit_anchor"] == "broker_trigger_not_actual_fill"
    assert not any(key.startswith("bracket") for key in request.metadata)
    assert "native_oco_bracket" not in request.metadata
    assert event.payload.metadata == original
    request.prewire_check()


@pytest.mark.parametrize("ask,wire", [("1.6501", "1.66"), ("1.66", "1.66")])
def test_real_oms_fresh_past_trigger_uses_bounded_limit(setup, monkeypatch, ask, wire):
    service, _factory, broker = setup
    event = incident_event(setup, monkeypatch, ask)
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1 and broker.previews == []
    request = broker.sent[0]
    assert request.order_type == "LIMIT" and request.metadata["limit_price"] == wire
    assert request.metadata["orb_target_price"] == event.payload.metadata["orb_target_price"]
    assert request.metadata["orb_protect_price"] == event.payload.metadata["orb_protect_price"]


@pytest.mark.parametrize("ask", ["1.66001", "1.67", "NaN", "Infinity", "0", "-1"])
def test_real_oms_refuses_unbounded_or_invalid_ask_without_submit(setup, monkeypatch, ask):
    service, _factory, broker = setup
    event = incident_event(setup, monkeypatch, ask)
    result = asyncio.run(service.process_trade_intent(event))
    assert result[-1].payload.status == "rejected"
    assert broker.sent == [] and broker.previews == []
    assert "orb_pastlevel_" in result[-1].payload.reason


@pytest.mark.parametrize("mutation", ["stale", "missing", "shape", "above_cap", "window"])
def test_real_oms_rechecks_after_last_preparation_await(setup, monkeypatch, mutation):
    service, _factory, broker = setup
    event = incident_event(setup, monkeypatch, "1.6501")
    original = service._prepare_bot_control_entry

    async def prepare(*args, **kwargs):
        await original(*args, **kwargs)
        if mutation == "missing":
            service._latest_quotes_by_symbol.clear()
        elif mutation == "window":
            monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: INCIDENT + timedelta(minutes=5))
        else:
            service._latest_quotes_by_symbol["STAK"] = quote(
                "1.65" if mutation == "shape" else "1.67" if mutation == "above_cap" else "1.6501",
                at=INCIDENT - timedelta(minutes=1) if mutation == "stale" else INCIDENT)

    monkeypatch.setattr(service, "_prepare_bot_control_entry", prepare)
    result = asyncio.run(service.process_trade_intent(event))
    assert result[-1].payload.status == "rejected"
    assert broker.sent == [] and broker.previews == []


@pytest.mark.parametrize("session", ["NORMAL", "PRE", "POST"])
@pytest.mark.parametrize("ask,kind", [(None, "STOP_LOSS_LIMIT"), ("1.64", "STOP_LOSS_LIMIT"),
                                      ("1.65", "STOP_LOSS_LIMIT"), ("1.6501", "LIMIT")])
def test_selected_stak_prices_reach_actual_webull_fake_sdk(fake_sdk, session, ask, kind):
    # Synthetic listing, not STAK native evidence; exercise the real symbol mapper.
    _instrument_stub(None, [{"symbol": "STAK", "instrument_id": "synthetic-STAK-instrument", "status": "OC"}])
    selected = selection(ask=ask)
    # The actual ORB producer retains the RAW breakout, not the rounded cap.
    metadata = {**selected, "orb_intended_break_level": "1.65", "session": session,
                "extended_hours": str(session != "NORMAL").lower()}
    request = OrderRequest("synthetic-l7-STAK", WEBULL, "orb_schwab", "STAK", "buy", "open",
                           Decimal(10), "SYNTHETIC_INCIDENT_CONTROL", metadata,
                           order_type=selected["order_type"])
    client = _FakeClient({"place": {"order_id": "synthetic-parent"}})
    adapter = _adapter(client, _native_stop_map_enabled=True)
    reports = asyncio.run(adapter.submit_order(request))
    assert reports[0].event_type == "accepted"
    placed = client.last["place"].values
    assert placed["instrument_id"] == "synthetic-STAK-instrument"
    assert placed["order_type"] == kind
    assert placed["limit_price"] == "1.66"
    assert placed["extended_hours_trading"] is (session != "NORMAL")
    if kind == "STOP_LOSS_LIMIT":
        assert placed["stop_price"] == "1.65"
        assert Decimal(placed["limit_price"]) > Decimal(placed["stop_price"])
        assert reports[0].metadata["webull_buy_stop_limit_tick_adjusted"] == "false"
    else:
        assert "stop_price" not in placed
    assert client.calls["place"] == 1


@pytest.mark.parametrize("level,trigger,cap", [
    (".50123", ".5013", ".5037"), (".99999", "1.00", "1.01"),
    ("1", "1.00", "1.01"), ("1.00001", "1.01", "1.02"),
    ("1.65", "1.65", "1.66"), ("10", "10.00", "10.05"),
    ("10.01", "10.01", "10.06"), ("25.3456", "25.35", "25.47"),
])
def test_provider_legal_stop_and_cap_boundaries(level, trigger, cap):
    if Decimal(level) < 1:
        with pytest.raises(ValueError, match="^PRICE1-REFUSED$"):
            selection(level)
        # Preserve the independent low-price grid check, not BUY admission.
        assert round_buy(Decimal(level), provider="webull") == Decimal(trigger)
        legacy = build_orb_schwab_bracket_metadata(Decimal(level))
        assert Decimal(legacy["limit_price"]) == Decimal(cap)
        return
    result = selection(level)
    assert Decimal(result["stop_price"]) == Decimal(trigger)
    assert Decimal(result["limit_price"]) == Decimal(cap)
    assert Decimal(result["limit_price"]) > Decimal(result["stop_price"])
    assert result["orb_buy_cap_nominal_pct"] == result["orb_buy_cap_pct"] == "0.5"
    assert Decimal(result["orb_buy_cap_effective_price"]) == Decimal(cap)


@pytest.mark.parametrize("level", ["10", "10.01", "25.3456", "100"])
def test_higher_price_legacy_geometry_and_exit_pair_anchors_unchanged(level):
    original = build_orb_schwab_bracket_metadata(Decimal(level))
    selected = selection(level)
    for key in ("stop_price", "limit_price", "reference_price", "orb_exit_anchor"):
        assert selected[key] == original[key]
    assert selected["orb_target_price"] == original["bracket_target_price"]
    assert selected["orb_protect_price"] == original["bracket_stop_price"]


@pytest.mark.parametrize("level,ask,expected", [(".50123", ".50131", ".5014"),
                                               (".99999", "1.00001", "1.01"),
                                               ("10.01", "10.01001", "10.02")])
def test_past_trigger_ceiling_ask_uses_provider_tick(level, ask, expected):
    if Decimal(level) < 1:
        with pytest.raises(ValueError, match="^PRICE1-REFUSED$"):
            selection(level, ask)
        assert round_buy(Decimal(ask), provider="webull") == Decimal(expected)
        return
    selected = selection(level, ask)
    assert selected["order_type"] == "LIMIT"
    assert Decimal(selected["limit_price"]) == Decimal(expected)
    assert Decimal(selected["limit_price"]) <= Decimal(selected["orb_buy_cap_effective_price"])


def test_schwab_default_equality_and_cap_are_unchanged():
    default = select_entry(Decimal("1.65"), quote("1.65"), INCIDENT, max_age_ms=1000)
    explicit = select_entry(Decimal("1.65"), quote("1.65"), INCIDENT, max_age_ms=1000, provider="schwab")
    assert default == explicit
    assert default["order_type"] == default["bracket_entry_type"] == "LIMIT"
    assert default["limit_price"] == "1.65" and default["native_oco_bracket"] == "true"
    assert round_buy(Decimal(1)) == Decimal("1.0000")
    assert str(round_buy(Decimal(1))) == "1.0000"
    assert str(round_buy(Decimal(1), provider="webull")) == "1.00"


@pytest.mark.parametrize("ask", ["1.65", "1.64", "1.67"])
def test_stale_quote_retains_strict_tick_cap_not_stale_limit(ask):
    selected = select_entry(Decimal("1.65"), quote(ask, at=INCIDENT - timedelta(seconds=2)),
                            INCIDENT, max_age_ms=1000, provider="webull")
    assert selected["order_type"] == "STOP_LIMIT" and selected["limit_price"] == "1.66"
    assert selected["orb_quote_unavailable_reason"] == "orb_pastlevel_quote_stale"


@pytest.mark.parametrize("at", [INCIDENT + timedelta(milliseconds=1), INCIDENT.replace(tzinfo=None)])
def test_invalid_quote_clocks_remain_fail_closed(at):
    with pytest.raises(ValueError, match="orb_pastlevel_quote_"):
        select_entry(Decimal("1.65"), quote("1.65", at=at), INCIDENT,
                     max_age_ms=1000, provider="webull")


def test_webull_fence_accepts_same_wire_evidence_but_not_changed_shape():
    selected = selection(ask="1.6501")
    assert_same_selection(Decimal("1.65"), quote("1.6599"), INCIDENT, selected,
                          max_age_ms=1000, provider="webull")
    with pytest.raises(ValueError, match="quote_changed_wait_next_plan"):
        assert_same_selection(Decimal("1.65"), quote("1.65"), INCIDENT, selected,
                              max_age_ms=1000, provider="webull")


def test_unknown_provider_is_not_silently_schwab():
    with pytest.raises(ValueError, match="provider_invalid"):
        select_entry(Decimal("1.65"), None, INCIDENT, max_age_ms=1000, provider="unknown")
