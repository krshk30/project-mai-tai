"""[codex] Controlled direct GET proofs; no production calls or historical fill claim."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.deploy_preflight import evaluate_live_deploy_preflight
from project_mai_tai.operator_holding_evidence import BotActivity, DirectAccountPositions, build_operator_holdings_proof
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings
from tests.unit.test_deploy_preflight import _healthy_overview
from tests.unit.test_operator_holdings import NOW
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


@pytest.fixture
def census(monkeypatch):
    factory = build_test_session_factory()
    monkeypatch.setattr("project_mai_tai.reconciliation.service.utcnow", lambda: NOW)
    with factory() as session:
        accounts = [BrokerAccount(name=name, provider=provider, environment="production")
                    for name, provider in (("live:schwab_1m_v2", "schwab"), ("live:orb", "webull"))]
        strategy = Strategy(code="schwab_1m_v2", name="controlled", execution_mode="live", metadata_json={})
        session.add_all([*accounts, strategy])
        session.flush()
        session.add(AccountPosition(broker_account_id=accounts[0].id, symbol="MOBX",
            quantity=Decimal("1000"), average_price=Decimal("1"), source_updated_at=NOW))
        session.commit()
        return factory, {a.id: a.name for a in accounts}, strategy.id


def order(census, *, side="buy", status="rejected", quantity="127", age=0,
          symbol="MOBX", account_index=0, submitted=False, intent=None):
    factory, accounts, strategy_id = census
    with factory() as session:
        row = BrokerOrder(broker_account_id=list(accounts)[account_index], strategy_id=strategy_id,
            client_order_id=str(uuid4()), symbol=symbol, side=side, quantity=Decimal(quantity),
            status=status, order_type="limit", time_in_force="day", payload={},
            intent_id=intent, submitted_at=NOW - timedelta(seconds=age) if submitted else None,
            updated_at=NOW - timedelta(seconds=age))
        session.add(row)
        session.commit()
        return row.id


def fill(census, *, side="buy", quantity="127", age=0, symbol="MOBX", strategy_id=None):
    factory, accounts, default_strategy = census
    order_id = order(census, side=side, status="filled", quantity=quantity, age=age, symbol=symbol)
    with factory() as session:
        session.add(Fill(order_id=order_id, broker_account_id=list(accounts)[0],
            strategy_id=strategy_id or default_strategy, symbol=symbol, side=side,
            quantity=Decimal(quantity), price=Decimal("1"), filled_at=NOW - timedelta(seconds=age), payload={}))
        session.commit()


def proof(census, *, symbol="MOBX", quantity="1000"):
    factory, accounts, _ = census
    direct = tuple(DirectAccountPositions(account_id, name, NOW, True,
        ((symbol, Decimal(quantity)),) if index == 0 else ())
        for index, (account_id, name) in enumerate(accounts.items()))
    with factory() as session:
        return build_operator_holdings_proof(session, accounts, direct_positions=direct, now=NOW,
            fill_balance_since=Settings().reconciliation_fill_balance_since)


def gate(p, *, count=1, overview=None):
    overview = overview or _healthy_overview(NOW)
    overview["counts"]["open_account_positions"] = count
    return evaluate_live_deploy_preflight(overview, service_target="oms", now=NOW,
                                          operator_holdings_proof=p)


def findings(census):
    factory, _, _ = census
    service = ReconciliationService(Settings(), FakeRedis(), session_factory=factory)
    with factory() as session:
        return service._build_position_findings(session)


def test_actual_gate_caller_admits_only_complete_general_operator_proof(census):
    assert gate(proof(census)) == []
    assert findings(census) == []
    assert gate(None)  # Ordinary CLI without the reviewed producer is still literal-flat.


@pytest.mark.parametrize("status", ["rejected", "expired", "cancelled", "aborted", "refused"])
def test_no_submitted_timestamp_still_counts_refused_order(census, status):
    order(census, status=status)
    assert gate(proof(census))
    assert findings(census)[0].severity == "critical"


def test_linked_intent_created_today_counts_order_with_old_updated_at(census):
    factory, accounts, strategy = census
    with factory() as session:
        intent = TradeIntent(broker_account_id=list(accounts)[0], strategy_id=strategy,
            symbol="MOBX", side="buy", intent_type="open", quantity=Decimal("1"),
            reason="controlled", status="rejected", payload={}, created_at=NOW, updated_at=NOW)
        session.add(intent)
        session.commit()
        intent_id = intent.id
    order(census, intent=intent_id, age=86400)
    assert gate(proof(census))


def test_roundtrip_counts_not_net_zero_and_exact_ipdn_exception(census):
    fill(census)
    fill(census, side="sell")
    assert gate(proof(census))
    fill(census, symbol="IPDN")
    fill(census, symbol="IPDN", side="sell")
    assert gate(proof(census, symbol="IPDN")) == []
    assert gate(proof(census, symbol="IPDN", quantity="1127"))
    p = proof(census, symbol="IPDN")
    assert p.failures(NOW + timedelta(days=1))


@pytest.mark.parametrize("kind", ["managed_zero", "virtual_zero_open", "virtual_short"])
def test_open_rows_even_zero_quantities_block_general_and_exact(census, kind):
    factory, accounts, strategy_id = census
    with factory() as session:
        if kind == "managed_zero":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=next(iter(accounts.values())),
                symbol="MOBX", status="open", current_quantity=0, original_quantity=1,
                entry_price=Decimal("1"), entry_time=NOW))
        else:
            session.add(VirtualPosition(broker_account_id=list(accounts)[0], strategy_id=strategy_id,
                symbol="MOBX", quantity=Decimal("-1") if kind == "virtual_short" else Decimal("0"),
                average_price=Decimal("1"), opened_at=NOW))
        session.commit()
    assert gate(proof(census))


@pytest.mark.parametrize("status", ["working", "unknown", "cancel_pending", "partially_filled", "aborted"])
@pytest.mark.parametrize("shape", ["order", "intent"])
def test_pending_sell_alias_without_books_is_critical_never_waived(census, status, shape):
    factory, accounts, strategy_id = census
    if shape == "order":
        order(census, side="SELL", status=status, age=86400)
    else:
        with factory() as session:
            session.add(TradeIntent(broker_account_id=list(accounts)[0], strategy_id=strategy_id,
                symbol="MOBX", side="Sell", intent_type="close", quantity=Decimal("1"),
                reason="controlled", status=status, created_at=NOW, updated_at=NOW, payload={}))
            session.commit()
    result = findings(census)
    assert result[0].severity == "critical" and result[0].payload["direction"] == "unowned_sell"
    assert gate(proof(census))


def test_ignored_historical_pair_never_waives_current_unowned_sell(census):
    order(census, side="sell", status="working", symbol="MI")
    factory, _, _ = census
    service = ReconciliationService(Settings(reconciliation_ignored_position_mismatches="live:schwab_1m_v2:MI"),
        FakeRedis(), session_factory=factory)
    with factory() as session:
        rows = service._build_position_findings(session)
        assert rows, "historical allowance must never hide a current unowned SELL"
        assert rows[0].payload["direction"] == "unowned_sell" and rows[0].severity == "critical"


@pytest.mark.parametrize("shape", ["order", "intent"])
def test_old_pending_buy_is_not_zero_activity_permission(census, shape):
    factory, accounts, strategy_id = census
    if shape == "order":
        order(census, status="working", age=86400)
    else:
        with factory() as session:
            session.add(TradeIntent(broker_account_id=list(accounts)[0], strategy_id=strategy_id,
                symbol="MOBX", side="buy", intent_type="open", quantity=Decimal("1"),
                reason="controlled", status="working", created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1), payload={}))
            session.commit()
    p = proof(census)
    data = next(d for (_, symbol), d in p.activity.items() if symbol == "MOBX")
    assert data.session_orders == 0 and data.session_fills == 0
    assert gate(p), "pending BUY remains owned even with no session order/fill"


def test_tiny_or_cross_strategy_historical_ownership_never_ages_or_nets_out(census):
    fill(census, quantity="0.00000001", age=86400)
    assert gate(proof(census))
    assert findings(census)[0].severity == "critical"
    factory, _, _ = census
    with factory() as session:
        strategy = Strategy(code="controlled_other", name="other", execution_mode="live", metadata_json={})
        session.add(strategy)
        session.commit()
        strategy_id = strategy.id
    fill(census, side="sell", quantity="0.00000001", age=86400, strategy_id=strategy_id)
    assert gate(proof(census))


def test_account_identity_is_not_inferred_from_another_accounts_counts(census):
    order(census, account_index=1)
    assert gate(proof(census)) == []  # Refused Webull order cannot contaminate Schwab MOBX.
    factory, accounts, _ = census
    with factory() as session, pytest.raises(ValueError, match="inventory"):
        build_operator_holdings_proof(session, {uuid4(): name for name in accounts.values()},
            direct_positions=proof(census).direct_positions, now=NOW,
            fill_balance_since=Settings().reconciliation_fill_balance_since)


@pytest.mark.parametrize("kind", ["direct_stale", "direct_future", "direct_incomplete", "missing_account",
    "foreign_account", "duplicate_account", "duplicate_symbol", "nan", "db_stale", "db_incomplete", "db_before_get"])
def test_gate_rejects_stale_unknown_partial_or_unbound_proofs(census, kind):
    p = proof(census)
    sources = list(p.direct_positions)
    if kind == "direct_stale":
        sources[0] = replace(sources[0], read_at=NOW - timedelta(seconds=121))
    if kind == "direct_future":
        sources[0] = replace(sources[0], read_at=NOW + timedelta(seconds=1))
    if kind == "direct_incomplete":
        sources[0] = replace(sources[0], complete=False)
    if kind == "missing_account":
        sources.pop()
    if kind == "foreign_account":
        sources[0] = replace(sources[0], account_id=uuid4())
    if kind == "duplicate_account":
        sources.append(sources[0])
    if kind == "duplicate_symbol":
        sources[0] = replace(sources[0], positions=sources[0].positions * 2)
    if kind == "nan":
        sources[0] = replace(sources[0], positions=(("MOBX", Decimal("NaN")),))
    p = replace(p, direct_positions=tuple(sources))
    if kind == "db_stale":
        p = replace(p, database_read_at=NOW - timedelta(seconds=121))
    if kind == "db_incomplete":
        p = replace(p, database_complete=False)
    if kind == "db_before_get":
        p = replace(p, database_read_at=NOW - timedelta(seconds=1))
    assert gate(p)


def test_gate_does_not_accept_dict_or_mask_inventory_or_other_critical_gates(census):
    p = proof(census)
    assert gate({"complete": True})
    assert gate(p, count=2)
    overview = _healthy_overview(NOW)
    overview["counts"]["pending_intents"] = 1
    overview["reconciliation"]["latest_run"]["summary"]["critical_findings"] = 1
    failures = gate(p, overview=overview)
    assert any("pending/submitted/accepted" in f for f in failures)
    assert any("critical findings" in f for f in failures)


def test_failed_census_never_returns_empty_permission(census, monkeypatch):
    factory, accounts, _ = census
    with factory() as session:
        monkeypatch.setattr(session, "scalars", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("controlled DB timeout")))
        with pytest.raises(RuntimeError, match="timeout"):
            build_operator_holdings_proof(session, accounts, direct_positions=(), now=NOW,
                fill_balance_since=Settings().reconciliation_fill_balance_since)


@pytest.mark.parametrize("change", [
    {"virtual_quantity": Decimal("1")}, {"managed_quantity": Decimal("1")},
    {"session_orders": -1}, {"session_fills": True}, {"net_fill_balance": Decimal("NaN")},
])
def test_flat_direct_read_cannot_hide_inconsistent_or_unknown_bot_evidence(census, change):
    p = proof(census, quantity="0")
    key = (next(iter(dict(p.accounts))), "MOBX")
    p.activity[key] = BotActivity(**change)
    assert gate(p, count=0)


@pytest.mark.parametrize("kind", ["direct_object", "direct_rows", "db_time"])
def test_nested_unknown_proof_is_a_block_not_an_empty_permission(census, kind):
    p = proof(census)
    if kind == "direct_object":
        p = replace(p, direct_positions=(None,))
    elif kind == "direct_rows":
        p = replace(p, direct_positions=(replace(p.direct_positions[0], positions=None), p.direct_positions[1]))
    else:
        p = replace(p, database_read_at=None)
    assert gate(p)


def test_partial_historical_checkpoint_cannot_claim_complete_ownership(census):
    factory, accounts, _ = census
    with factory() as session, pytest.raises(ValueError, match="checkpoint"):
        build_operator_holdings_proof(session, accounts, direct_positions=(), now=NOW,
            fill_balance_since=NOW - timedelta(seconds=1))
