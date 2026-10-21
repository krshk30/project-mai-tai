"""Author-only ORBPASTLEVEL1 cases. Execution belongs to the main integrator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.oms.orb_pastlevel import receipt, unsent_plan
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_pastlevel import assert_same_selection, select_entry
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata

NOW = datetime(2026, 10, 21, 13, 28, tzinfo=UTC)


def choose(level="1.20", ask="1.21", *, at=NOW, now=NOW):
    return select_entry(Decimal(level), {"ask": ask, "received_at": at}, now, max_age_ms=2000)


@pytest.mark.parametrize("ask,kind,limit", [
    ("1.19", "STOP_LIMIT", "1.21"), ("1.20", "LIMIT", "1.20"),
    ("1.2001", "LIMIT", "1.21"), ("1.21", "LIMIT", "1.21"),
])
def test_tick_floor_shape_and_trigger_anchored_exits(ask, kind, limit):
    md = choose(ask=ask)
    assert md["order_type"] == md["bracket_entry_type"] == kind
    assert md["limit_price"] == limit
    assert md["stop_price"] == "1.20"
    assert md["bracket_target_price"] == "1.26"
    assert md["bracket_stop_price"] == "1.10"
    assert md["orb_buy_cap_nominal_pct"] == "0.5"
    assert Decimal(md["orb_buy_cap_effective_pct"]) > Decimal(".8333")


@pytest.mark.parametrize("ask", ["1.21001", "1.22", "NaN", "Infinity", "0", "-1"])
def test_above_rounded_cap_or_bad_ask_is_never_a_market_order(ask):
    with pytest.raises(ValueError):
        choose(ask=ask)


@pytest.mark.parametrize("delta,allowed", [(0, True), (-2000, True), (-2001, False), (1, False)])
def test_quote_age_inclusive_and_future_clock_rejected(delta, allowed):
    if allowed:
        choose(at=NOW + timedelta(milliseconds=delta))
    else:
        with pytest.raises(ValueError, match="stale"):
            choose(at=NOW + timedelta(milliseconds=delta))


@pytest.mark.parametrize("quote", [None, {}, {"ask": "1.2"}, {"ask": "1.2", "received_at": NOW.replace(tzinfo=None)}])
def test_no_missing_quote_authority(quote):
    with pytest.raises(ValueError):
        select_entry(Decimal("1.2"), quote, NOW, max_age_ms=2000)


@pytest.mark.parametrize("level,trigger,cap", [
    (".50123", ".5013", ".5038"), (".99999", "1.0000", "1.01"),
    ("1", "1.0000", "1.01"), ("1.00001", "1.01", "1.02"),
    ("3.42", "3.42", "3.44"),
])
def test_tick_boundary_and_nominal_upward_rounding(level, trigger, cap):
    md = choose(level=level, ask=level)
    assert Decimal(md["stop_price"]) == Decimal(trigger)
    assert Decimal(md["orb_buy_cap_effective_price"]) == Decimal(cap)
    assert Decimal(md["limit_price"]) <= Decimal(cap)
    legacy = build_orb_schwab_bracket_metadata(Decimal("1.20"))
    assert legacy["limit_price"] == "1.20"  # Webull/default semantics stay unchanged.


def test_selection_fence_rejects_await_age_and_new_quote_without_repreview():
    md = choose()
    assert_same_selection(Decimal("1.2"), {"ask": "1.21", "received_at": NOW + timedelta(seconds=1)},
                          NOW + timedelta(seconds=1), md, max_age_ms=2000)
    with pytest.raises(ValueError, match="stale"):
        assert_same_selection(Decimal("1.2"), {"ask": "1.21", "received_at": NOW},
                              NOW + timedelta(seconds=3), md, max_age_ms=2000)
    with pytest.raises(ValueError, match="changed"):
        assert_same_selection(Decimal("1.2"), {"ask": "1.20", "received_at": NOW},
                              NOW, md, max_age_ms=2000)


def test_limit_native_bracket_wire_has_no_stop_parent_and_preserves_exit_anchor():
    adapter = SchwabBrokerAdapter(Settings())
    request = OrderRequest(client_order_id="fixture", broker_account_name="fixture", strategy_code="orb_schwab",
        symbol="OLOX", side="buy", intent_type="open", quantity=Decimal("10"), reason="fixture",
        metadata=choose(), prewire_check=lambda: None)
    body = adapter._build_bracket_payload(request)
    assert body["orderType"] == "LIMIT" and body["price"] == 1.21
    assert "stopPrice" not in body and body["session"] == "NORMAL"
    target, protective = body["childOrderStrategies"][0]["childOrderStrategies"]
    assert target["price"] == 1.26 and protective["stopPrice"] == 1.10
    assert target["orderLegCollection"][0]["quantity"] == 10


def test_country_webull_cap_and_route_metadata_are_unchanged():
    from project_mai_tai.orb_country_route import routed_orb_metadata
    selection = SimpleNamespace(country=SimpleNamespace(broker="webull", reason="sec_non_us",
                                                        status="resolved", as_of=None))
    md = routed_orb_metadata(selection, Decimal("1.20"))
    assert md["limit_price"] == "1.20" and md["order_type"] == "STOP_LIMIT"
    assert md["orb_target_price"] == "1.26" and md["orb_protect_price"] == "1.10"
    assert md["extended_hours"] == "false" and md["session"] == "NORMAL"
    assert "native_oco_bracket" not in md and "orb_pastlevel_policy" not in md


class ProofSession:
    def __init__(self, rows, order=None):
        self.rows, self.order = rows, order

    def scalars(self, _query):
        return self.rows

    def scalar(self, _query):
        return self.order


def proof_row():
    row = SimpleNamespace(id="root", strategy_id="strategy", broker_account_id="account",
                          symbol="OLOX", side="buy", intent_type="open", status="rejected",
                          created_at=NOW,
                          payload={"refusal_origin": "client_abort"})
    receipt(row, "root", "local_zero")
    return row


def test_only_exact_local_zero_receipt_can_continue():
    root = proof_row()
    incoming = SimpleNamespace(id="next", strategy_id="strategy", broker_account_id="account", symbol="OLOX")
    assert unsent_plan(ProofSession([root]), incoming, NOW) is root
    with pytest.raises(ValueError, match="budget"):
        unsent_plan(ProofSession([root], order="any-native-order-even-cancelled"), incoming, NOW)
    with pytest.raises(ValueError, match="original"):
        unsent_plan(ProofSession([]), incoming, NOW)


@pytest.mark.parametrize("phase", ["reserved", "MAY_HAVE_SENT", "unknown", "filled"])
def test_nonterminal_or_possible_wire_proof_never_releases_budget(phase):
    root = proof_row()
    receipt(root, "root", phase)
    with pytest.raises(ValueError, match="UNKNOWN"):
        unsent_plan(ProofSession([root]), root, NOW)


@pytest.mark.asyncio
async def test_transport_thread_fence_runs_before_any_http(monkeypatch):
    adapter = SchwabBrokerAdapter(Settings())
    calls = []
    monkeypatch.setattr(adapter, "_blocking_request_json", lambda *args: calls.append(args))

    def stale():
        raise ValueError("orb_pastlevel_quote_stale")

    with pytest.raises(RuntimeError, match="stale"):
        await adapter._request_json("POST", "/unused", access_token="fixture", body={}, prewire_check=stale)
    assert calls == []


@pytest.mark.asyncio
async def test_token_await_rechecks_quote_and_fenced_401_is_not_retried(monkeypatch):
    adapter = SchwabBrokerAdapter(Settings())
    checked, calls = [], []
    async def token(**_kwargs):
        checked.append("token")
        return "fixture"
    async def wire(*_args, prewire_check=None, **_kwargs):
        prewire_check()
        calls.append("wire")
        return 401, {}, {}
    monkeypatch.setattr(adapter, "_get_access_token", token)
    monkeypatch.setattr(adapter, "_request_json", wire)
    adapter._refresh_token, adapter.client_id, adapter.client_secret = "fixture", "fixture", "fixture"
    result = await adapter._authorized_request_json("POST", "/unused", body={},
        prewire_check=lambda: checked.append("fence"))
    assert result[0] == 401 and checked == ["token", "fence"] and calls == ["wire"]
