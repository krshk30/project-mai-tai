"""Private local alias controls; never proof of native account equivalence."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from project_mai_tai.brokerdecides1_accounts import MAX_LOCAL_ACCOUNT_ALIASES
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.brokerdecides1_retirement import retire_batch
from project_mai_tai.db.models import BrokerAccount, BrokerDecidesDispatchRetirement, Fill, Strategy, TradeIntent
from test_brokerdecides1_control_overlay import control, nonentry
from test_brokerdecides1_local_census import ACCOUNTS, attempt, census as census, order


def alias(session, *, provider="webull", active=True):
    row = BrokerAccount(name="live:private-alias", provider=provider, environment="production",
        is_active=active)
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("code", ["schwab_1m_v2", "orb", "momentum_warn_live"])
def test_pending_buy_under_another_live_alias_is_not_missing_local_evidence(census, provider, code):
    session, accounts, strategy = census
    other = alias(session, provider=provider)
    if code != strategy.code:
        strategy = Strategy(code=code, name="private fixture")
        session.add(strategy)
        session.flush()
    session.add(TradeIntent(strategy_id=strategy.id, broker_account_id=other.id,
        symbol="WORX", side="buy", intent_type="open", quantity=Decimal("1"),
        reason="private fixture", status="pending", payload={}))
    session.flush()
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_alias_control_unknown_is_held_even_without_order(census, provider):
    session, accounts, strategy = census
    control(session, alias(session, provider=provider), strategy)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("kind", ["intent", "order", "cancel"])
def test_pending_sell_or_cancel_under_alias_is_held(census, provider, kind):
    session, accounts, strategy = census
    nonentry(session, alias(session, provider=provider), strategy, kind, "pending")
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


@pytest.mark.parametrize("proof", ["no_order", "native_terminal", "no_native_source"])
def test_legacy_alias_dispatch_unknown_is_not_dropped_on_new_configured_names(census, proof):
    session, accounts, strategy = census
    other = alias(session)
    journal = attempt(session)
    journal.payload = {**journal.payload, "account_name": other.name,
                       "account_names": [ACCOUNTS[0], other.name]}
    if proof != "no_order":
        order(session, [other, accounts[1]], strategy,
              source="broker" if proof == "native_terminal" else None)
    session.flush()
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert not fills
    if proof == "native_terminal":
        assert not inflight and terminals == frozenset({("WORX", "persisted-attempt")})
    else:
        assert inflight == frozenset({"WORX"}) and not terminals
    assert journal.payload["account_name"] == other.name
    _, retired, visited = retire_batch(session, ACCOUNTS, None,
        int(datetime.now(UTC).timestamp() * 1000))
    assert visited == 1 and retired == (1 if proof == "native_terminal" else 0)
    assert (session.get(BrokerDecidesDispatchRetirement, journal.id) is not None) == (proof == "native_terminal")


def test_same_segment_real_v2_fill_under_old_alias_still_blocks(census):
    session, accounts, strategy = census
    other = alias(session)
    child = order(session, [other, accounts[1]], strategy, status="filled")
    session.add(Fill(order_id=child.id, strategy_id=strategy.id, broker_account_id=other.id,
        broker_fill_id="private-alias-fill", symbol="WORX", side="buy", quantity=Decimal("1"),
        price=Decimal("3"), filled_at=datetime.now(UTC)))
    session.flush()
    assert ("WORX", 900) in local_evidence(session, ACCOUNTS)[1]


def test_inactive_alias_never_erases_unresolved_ownership(census):
    session, accounts, strategy = census
    control(session, alias(session, active=False), strategy)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


def test_unknown_provider_alias_cannot_make_pending_local_effect_flat(census):
    session, accounts, strategy = census
    control(session, alias(session, provider="unresolved-provider"), strategy)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


def test_alias_inventory_cap_is_unknown_not_partial_scope(census):
    session, accounts, strategy = census
    session.add_all(BrokerAccount(name=f"live:private-{index}", provider="webull", environment="production")
                    for index in range(MAX_LOCAL_ACCOUNT_ALIASES - len(accounts) + 1))
    session.flush()
    with pytest.raises(ValueError, match="account_alias_census_overflow"):
        local_evidence(session, ACCOUNTS)
