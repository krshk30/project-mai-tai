from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent
from project_mai_tai.reconciliation.operator_closed_exits import (
    REVIEWED_CLOSES, exclude_reviewed_operator_closes, match_ruling,
)

NOW = datetime(2026, 10, 22, 18, tzinfo=UTC)
RULE = REVIEWED_CLOSES[0]


def setup_entry():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
    account = BrokerAccount(name=RULE.account, provider="webull", environment="production")
    session.add_all([strategy, account])
    session.flush()
    order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
        client_order_id="schwab_1m_v2-SAIQ-open-2a3bc04ec248", broker_order_id=RULE.native_entry_id,
        symbol="SAIQ", side="buy", order_type="limit", time_in_force="day", quantity=RULE.quantity,
        status="filled", payload={}, submitted_at=RULE.entry_second, updated_at=RULE.entry_second)
    session.add(order)
    session.flush()
    fill = Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
        broker_fill_id="controlled-saiq-fill", symbol="SAIQ", side="buy", quantity=RULE.quantity,
        price=RULE.price, filled_at=RULE.entry_second + timedelta(microseconds=954000), payload={})
    session.add(fill)
    session.commit()
    return session, fill, order, account


def test_saiq_reviewed_entry_excluded_and_once_audit_survives_repeat():
    session, fill, _, _ = setup_entry()
    with session:
        for _ in range(2):
            assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW) == [str(fill.id)]
            session.commit()
        assert session.scalar(select(func.count()).select_from(SystemIncident)) == 1
        audit = session.scalar(select(SystemIncident))
        assert audit.severity == "info" and "operator-closed" in audit.title
        assert audit.payload["allowance_id"] == RULE.allowance_id
        assert session.get(Fill, fill.id).quantity == 39  # No fabricated exit fill / ledger repair.


@pytest.mark.parametrize("field,value", [("quantity", Decimal("40")), ("price", Decimal("7.63")),
                                         ("symbol", "OTHER"), ("filled_at", RULE.entry_second + timedelta(seconds=1))])
def test_same_symbol_or_day_cannot_borrow_exact_entry_ruling(field, value):
    session, fill, order, account = setup_entry()
    with session:
        setattr(fill, field, value)
        assert match_ruling(fill, order, account, now=NOW) is None


@pytest.mark.parametrize("kind", ["sell_order_no_fill", "sell_intent_no_fill", "sell_fill"])
def test_actual_bot_missing_exit_or_sell_evidence_keeps_page(kind):
    session, fill, order, _ = setup_entry()
    with session:
        if kind == "sell_order_no_fill":
            session.add(BrokerOrder(strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
                client_order_id="controlled-bot-sell", symbol="SAIQ", side="sell", order_type="market",
                time_in_force="day", quantity=39, status="filled", payload={}, updated_at=NOW))
        elif kind == "sell_intent_no_fill":
            session.add(TradeIntent(strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
                symbol="SAIQ", side="sell", intent_type="close", quantity=39, reason="bot exit",
                status="filled", payload={}, created_at=NOW))
        else:
            session.add(Fill(order_id=order.id, strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
                broker_fill_id="controlled-exit", symbol="SAIQ", side="sell", quantity=39,
                price=Decimal("7.50"), filled_at=NOW, payload={}))
        session.flush()
        assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW) == []
        assert session.scalar(select(func.count()).select_from(SystemIncident)) == 0


def test_no_positive_operator_flat_proof_never_becomes_manual_from_empty_books():
    session, fill, _, _ = setup_entry()
    with session:
        assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW,
            rulings=(replace(RULE, broker_flat_proof=""),)) == []


def test_contradictory_native_position_keeps_page():
    session, fill, _, account = setup_entry()
    with session:
        session.add(AccountPosition(broker_account_id=account.id, symbol="SAIQ", quantity=39,
                                    average_price=Decimal("7.62"), source_updated_at=NOW))
        session.flush()
        assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW) == []


def test_two_fills_cannot_borrow_one_reviewed_manual_close():
    session, fill, order, _ = setup_entry()
    with session:
        other = Fill(order_id=order.id, strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
            broker_fill_id="controlled-duplicate-entry", symbol="SAIQ", side="buy", quantity=39,
            price=Decimal("7.62"), filled_at=fill.filled_at, payload={})
        session.add(other)
        session.flush()
        assert exclude_reviewed_operator_closes(session, [str(fill.id), str(other.id)], now=NOW) == []


def test_native_entry_identity_and_account_must_match():
    session, fill, order, account = setup_entry()
    with session:
        order.broker_order_id = "different-native-entry"
        assert match_ruling(fill, order, account, now=NOW) is None
        order.broker_order_id = RULE.native_entry_id
        account.name = "other-account"
        assert match_ruling(fill, order, account, now=NOW) is None


def test_future_manual_close_requires_separate_explicit_reviewed_positive_ruling():
    session, fill, order, account = setup_entry()
    with session:
        fill.symbol = order.symbol = "FUT"
        order.broker_order_id = "future-native-entry"
        assert match_ruling(fill, order, account, now=NOW) is None
        positive = replace(RULE, allowance_id="CONTROLLED-FUTURE-MANUAL", symbol="FUT",
            native_entry_id=order.broker_order_id, broker_flat_proof="SYNTHETIC confirmed broker flat",
            operator_ruling="SYNTHETIC explicit operator close", incident_minutes=())
        assert match_ruling(fill, order, account, now=NOW, rulings=(positive,)) == positive


def add_recorded_incidents(session):
    path = Path(__file__).resolve().parents[1] / "fixtures/alerts1_saiq_incidents_20261022.jsonl"
    rows = []
    for line in path.read_text().splitlines():
        original = json.loads(line)
        values = {**original, "id": UUID(original["id"]),
                  "opened_at": datetime.fromisoformat(original["opened_at"])}
        incident = SystemIncident(**values)
        session.add(incident)
        rows.append((incident, original))
    session.flush()
    return rows


def test_only_three_entry_bound_incidents_become_info_and_payloads_survive():
    session, fill, _, _ = setup_entry()
    with session:
        recorded = add_recorded_incidents(session)
        foreign = SystemIncident(service_name="oms-risk", severity="critical", status="open",
            title="unrelated SAIQ incident", opened_at=RULE.incident_minutes[0],
            payload={"symbol": "SAIQ", "broker_account_name": RULE.account, "managed_row_id": str(uuid4())})
        session.add(foreign)
        session.flush()
        for _ in range(2):
            assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW) == [str(fill.id)]
            session.commit()
        assert foreign.severity == "critical"
        tagged = [row for row in session.scalars(select(SystemIncident)).all()
                  if row.payload.get("operator_closed_allowance")]
        assert len(tagged) == 3
        for row, original in recorded:
            assert row.severity == "info" and row.status == original["status"] and row.closed_at is None
            assert {key: value for key, value in row.payload.items()
                    if key not in ("operator_closed_allowance", "operator_closed_entry_fill_id")} == original["payload"]
        assert session.scalar(select(func.count()).select_from(SystemIncident).where(
            SystemIncident.service_name == "oco-capture")) == 1


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("drift", ["id", "service", "clock", "account", "extra_identity", "allowance"])
def test_recorded_incident_drift_remains_critical(index, drift):
    session, fill, _, _ = setup_entry()
    with session:
        rows = add_recorded_incidents(session)
        changed = rows[index][0]
        if drift == "id":
            changed.id = uuid4()
        elif drift == "service":
            changed.service_name = "foreign-service"
        elif drift == "clock":
            changed.opened_at += timedelta(microseconds=1)
        elif drift == "account":
            key = next(key for key in ("account_name", "broker_account_name", "account") if key in changed.payload)
            changed.payload = {**changed.payload, key: "foreign-account"}
        elif drift == "extra_identity":
            changed.payload = {**changed.payload, "entry_order_id": "foreign-entry"}
        else:
            changed.payload = {**changed.payload, "operator_closed_allowance": "foreign-ruling"}
        session.flush()
        exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW)
        session.commit()
        assert changed.severity == "critical"
        assert len([row for row, _ in rows if row.severity == "info"]) == 2


def test_actual_client_identity_is_required_for_wbattach_incident():
    session, fill, order, _ = setup_entry()
    with session:
        rows = add_recorded_incidents(session)
        order.client_order_id = "foreign-client-entry"
        session.flush()
        exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW)
        assert rows[2][0].severity == "critical"


def test_actual_incident_packet_never_overrides_bot_sell_veto():
    session, fill, _, _ = setup_entry()
    with session:
        rows = add_recorded_incidents(session)
        session.add(TradeIntent(strategy_id=fill.strategy_id, broker_account_id=fill.broker_account_id,
            symbol="SAIQ", side="sell", intent_type="close", quantity=39, reason="real missing exit",
            status="filled", payload={}, created_at=NOW))
        session.flush()
        assert exclude_reviewed_operator_closes(session, [str(fill.id)], now=NOW) == []
        assert all(row.severity == "critical" for row, _ in rows)


def test_post_midnight_audit_does_not_subtract_a_fill_outside_current_phone_candidates():
    session, fill, _, _ = setup_entry()
    with session:
        assert exclude_reviewed_operator_closes(session, [], now=NOW + timedelta(days=1)) == []
        session.commit()
        assert session.scalar(select(func.count()).select_from(SystemIncident)) == 1


def load_checker(monkeypatch, excluded, *, error=False, commit_error=False):
    path = Path(__file__).resolve().parents[2] / "ops/health/oco_capture_check.py"
    spec = importlib.util.spec_from_file_location("controlled_oco_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args):
            return SimpleNamespace(one=lambda: (1, 0, 0, ["controlled-fill"]), scalar_one=lambda: 0)
        def begin_nested(self): return nullcontext()
        def commit(self):
            if commit_error:
                raise RuntimeError("controlled commit failure; no durable audit")
        def rollback(self): pass
    def classify(*args, **kwargs):
        if error:
            raise RuntimeError("controlled evidence failure")
        return ["controlled-fill"] if excluded else []
    sent = []
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        webull_bracket_realign_on_fill_enabled=False, oms_record_native_oco_exit_fills_enabled=True))
    monkeypatch.setattr(module, "build_session_factory", lambda settings: Session)
    monkeypatch.setattr(module, "exclude_reviewed_operator_closes", classify)
    monkeypatch.setattr(module, "log_count", lambda pattern: 0)
    monkeypatch.setattr(module, "push", lambda *args: sent.append(args))
    monkeypatch.setattr(module.sys, "argv", [str(path)])
    return module, sent


@pytest.mark.parametrize("excluded,error,exit_code,pages", [(True, False, 0, 0),
                                                            (False, False, 2, 1), (False, True, 2, 1)])
def test_real_checker_phone_route_manual_excluded_but_real_or_unknown_miss_pages(monkeypatch, excluded, error, exit_code, pages):
    module, sent = load_checker(monkeypatch, excluded, error=error)
    assert module.main() == exit_code
    assert len(sent) == pages
    if sent:
        assert sent[0][2] == "urgent" and "NO exit fill" in sent[0][1]


def test_audit_commit_failure_does_not_subtract_or_silence_phone(monkeypatch):
    module, sent = load_checker(monkeypatch, True, commit_error=True)
    assert module.main() == 2
    assert len(sent) == 1 and sent[0][2] == "urgent"
    assert "1 entry fill(s)" in sent[0][1] and "NO exit fill" in sent[0][1]
