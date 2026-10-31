"""17:47 applicability ruling controls. Author-only, execution belongs to main."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_pastlevel import assert_same_selection, select_entry
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
from tests.unit.test_orb_pastlevel import NOW


@pytest.mark.parametrize("level,trigger,cap", [
    (".50123", ".5013", ".5038"), (".99999", "1.0000", "1.01"),
    ("1", "1.0000", "1.01"), ("1.00001", "1.01", "1.02"),
    ("1.20", "1.20", "1.21"), ("3.42", "3.42", "3.44"),
])
@pytest.mark.parametrize("age_ms", [None, 2001])
def test_legacy_missing_or_stale_uses_same_pure_tick_cap(level, trigger, cap, age_ms):
    quote = None if age_ms is None else {"ask": "999", "received_at": NOW - timedelta(milliseconds=age_ms)}
    if Decimal(level) < 1:
        with pytest.raises(ValueError, match="^PRICE1-REFUSED$"):
            select_entry(Decimal(level), quote, NOW, max_age_ms=2000)
        # Price-plan arithmetic remains testable without admitting a subfloor BUY.
        planned = build_orb_schwab_bracket_metadata(Decimal(level), tick_cap=True)
        assert Decimal(planned["stop_price"]) == Decimal(trigger)
        assert Decimal(planned["limit_price"]) == Decimal(cap)
        return
    md = select_entry(Decimal(level), quote, NOW, max_age_ms=2000)
    assert md["order_type"] == md["bracket_entry_type"] == "STOP_LIMIT"
    assert Decimal(md["stop_price"]) == Decimal(trigger)
    assert Decimal(md["limit_price"]) == Decimal(md["orb_buy_cap_effective_price"]) == Decimal(cap)
    assert md["orb_quote_selection"] == "legacy_no_fresh_ask"
    assert Decimal(md["bracket_stop_price"]) < Decimal(trigger) < Decimal(cap) < Decimal(md["bracket_target_price"])
    assert_same_selection(Decimal(level), quote, NOW, md, max_age_ms=2000)


def test_legacy_missing_to_stale_retains_identical_previewed_wire():
    md = select_entry(Decimal("1.20"), None, NOW, max_age_ms=2000)
    snapshot = dict(md)
    assert_same_selection(Decimal("1.20"), {"ask": "999", "received_at": NOW - timedelta(seconds=3)},
                          NOW, md, max_age_ms=2000)
    assert md == snapshot


@pytest.mark.parametrize("ask,reason", [("1.19", "changed"), ("1.21", "changed"), ("1.22", "above_cap")])
def test_legacy_preview_cannot_silently_switch_applicability(ask, reason):
    md = select_entry(Decimal("1.20"), None, NOW, max_age_ms=2000)
    snapshot = dict(md)
    with pytest.raises(ValueError, match=reason):
        assert_same_selection(Decimal("1.20"), {"ask": ask, "received_at": NOW}, NOW, md, max_age_ms=2000)
    assert md == snapshot


@pytest.mark.parametrize("loss", ["stale", "missing"])
@pytest.mark.asyncio
async def test_fresh_limit_cannot_downgrade_after_token_await(monkeypatch, loss):
    clock = [NOW]
    quote = {"ask": "1.21", "received_at": NOW}
    md = select_entry(Decimal("1.20"), quote, NOW, max_age_ms=2000)
    snapshot = dict(md)
    adapter = SchwabBrokerAdapter(Settings())
    calls = []

    def fence():
        assert_same_selection(Decimal("1.20"), quote, clock[0], md, max_age_ms=2000)

    async def token(**_kwargs):
        if loss == "stale":
            clock[0] = NOW + timedelta(seconds=3)
        else:
            quote.clear()
        return "synthetic-token"

    def forbidden(*args):
        calls.append(args)
        raise AssertionError("late quote loss reached HTTP")

    monkeypatch.setattr(adapter, "_get_access_token", token)
    monkeypatch.setattr(adapter, "_blocking_request_json", forbidden)
    with pytest.raises(RuntimeError, match=f"quote_{loss}"):
        await adapter._authorized_order_request_json(SimpleNamespace(prewire_check=fence),
                                                     "POST", "/unused", body={"orderType": "LIMIT"})
    assert calls == [] and md == snapshot
    assert md["order_type"] == "LIMIT" and md["limit_price"] == "1.21"
