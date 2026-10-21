"""UNRUN author controls. Reported/actual row SHAPES, not PG/native acceptance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace as Row
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.cancelack1 import proven_accounts, working_buy_present
from project_mai_tai.db.models import (
    Base, BotControlAttempt, BotControlCommand, BotControlState, BrokerAccount, BrokerOrder,
    BrokerOrderEvent, Fill, Strategy, TradeIntent,
)
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.v2_removed_wait import RemovedWait


PRIMARY, MIRROR = "live:schwab_1m_v2", "live:orb"
FIXTURE = Path(__file__).parents[1] / "fixtures/cancelack1_worx_repair_20261019.json"
CUT = datetime(2026, 10, 21, 20, 37, 50, tzinfo=UTC)


def reported_orders(case="POM", account=PRIMARY):
    """17:01 card shape only: real full tokens/UUIDs/payloads NOT supplied.

    POM's three coids/statuses are reported facts. All tokens, parent UUIDs,
    timestamps/segment/journals here are explicitly synthetic controls. SAIQ's
    seven-order distribution is a controlled example, not a raw-row assertion.
    """
    anchor = int((CUT - timedelta(hours=2)).timestamp() * 1000)
    request = RemovedWait(case, anchor, "SYNTHETIC-clearwait", anchor + 60_000,
                          (PRIMARY, MIRROR), "retry_exhausted")
    strategy, aid = uuid4(), uuid4()
    base = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": case,
            "opportunity_id": str(anchor), "account_names": list(request.account_names)}
    journal = [Row(payload={**base, "kind": "begin", "attempt_token": "", "account_name": ""},
                   created_at=datetime.fromtimestamp(anchor / 1000, UTC))]
    parents, orders = [], []
    suffixes = ("f4d9acee0f3c", "1af64b88605a", "d9441d475893") if case == "POM" else range(7)
    for index, suffix in enumerate(suffixes):
        token = f"SYNTHETIC-dispatch-{index}"
        at = datetime.fromtimestamp(anchor / 1000, UTC) + timedelta(milliseconds=index + 1)
        journal.append(Row(payload={**base, "kind": "attempt", "attempt_token": token,
                                    "account_name": account}, created_at=at))
        md = {"fanout_segment_id": str(anchor), "clearwait_dispatch_token": token}
        parent = Row(id=uuid4(), strategy_id=strategy, broker_account_id=aid, symbol=case,
                     side="buy", intent_type="open", payload={"metadata": md}, created_at=at)
        parents.append(parent)
        last = index == len(suffixes) - 1
        orders.append(Row(intent_id=parent.id, strategy_id=strategy, broker_account_id=aid,
            symbol=case, side="buy", status="filled" if last else "cancelled",
            client_order_id=(f"schwab_1m_v2-POM-open-{suffix}" if case == "POM"
                             else f"SYNTHETIC-SAIQ-order-{suffix}"),
            payload=dict(md) if last else {"fanout_segment_id": str(anchor)}))
    owners = [Row(payload={**base, "fill_accounts": [account], "position_ids": {}})]
    return request, {aid: account, uuid4(): MIRROR if account == PRIMARY else PRIMARY}, strategy, journal, owners, parents, orders


@pytest.mark.parametrize("case", ["POM", "SAIQ"])
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
def test_reported_terminal_reprice_shape_uses_exact_parent_identity(case, account):
    data = reported_orders(case, account)
    assert proven_accounts(*data) == frozenset({account})
    assert "clearwait_dispatch_token" not in data[-1][0].payload  # No token backfill/write.


@pytest.mark.parametrize("status", ["filled", "cancelled", "canceled", "rejected", "expired", "aborted", "replaced"])
def test_terminal_token_inheritance_is_identity_only(status):
    data = reported_orders()
    data[-1][0].status = status
    assert proven_accounts(*data) == frozenset({PRIMARY})


@pytest.mark.parametrize("status", [None, "pending", "submitted", "accepted", "working", "partially_filled", "unknown"])
def test_nonterminal_missing_token_never_inherits(status):
    data = reported_orders()
    data[-1][0].status = status
    with pytest.raises(ValueError, match="order_history_UNKNOWN"):
        proven_accounts(*data)


@pytest.mark.parametrize("fault", ["orphan", "strategy", "symbol", "side", "account", "segment", "conflict", "empty_token"])
def test_terminal_order_cannot_borrow_foreign_or_conflicting_parent(fault):
    data = reported_orders()
    order = data[-1][0]
    if fault == "orphan":
        order.intent_id = uuid4()
    elif fault == "strategy":
        order.strategy_id = uuid4()
    elif fault == "symbol":
        order.symbol = "OTHER"
    elif fault == "side":
        order.side = "sell"
    elif fault == "account":
        order.broker_account_id = next(key for key in data[1] if key != order.broker_account_id)
    elif fault == "segment":
        order.payload["fanout_segment_id"] = "SYNTHETIC-other-segment"
    else:
        order.payload["clearwait_dispatch_token"] = "SYNTHETIC-conflict" if fault == "conflict" else ""
    with pytest.raises(ValueError, match="order_history_UNKNOWN"):
        proven_accounts(*data)


def model_values(record):
    values = deepcopy(record)
    for key in ("id", "strategy_id", "broker_account_id", "intent_id"):
        if key in values and values[key] is not None:
            values[key] = UUID(values[key])
    for key in ("created_at", "updated_at", "submitted_at"):
        if key in values and values[key] is not None:
            values[key] = datetime.fromisoformat(values[key])
    values["quantity"] = Decimal(values["quantity"])
    return values


def repair_rows(writer):
    data = json.loads(FIXTURE.read_text())
    parent, order = data["parent"], data["order"]
    if writer == "settlement":
        # MODELED live SQL postimage; no actual SPHL repair/native receipt claim.
        pid, oid = "9ba4a2c0-aacc-5dba-a444-55a5e290ed2f", "75e3889e-fad2-44c5-8641-1b642cc68831"
        parent.update(id=pid, symbol="SPHL", quantity="26.00000000")
        parent["payload"].pop("repair_O_1019_26")
        parent["payload"]["momlive_episode"].update(episode_id=pid, symbol="SPHL")
        parent["payload"]["metadata"] = {"episode_id": pid}
        receipt = {"repair_id": "MOMSTUCK2:SPHL:20261021:refused-before-wire",
            "actor": "claude-1", "scope": "LIVE_STOPPED", "reason": "refused_before_wire",
            "prior_episode_state": "claimed", "prior_intent_status": "pending", "prior_order_status": "pending",
            "snapshot_id": "SYNTHETIC-snapshot", "at": "2026-10-20T00:34:15+00:00",
            "evidence": {name: {"ref": "SYNTHETIC-not-native", "sha256": "a" * 64, "verified_by": "claude-1"}
                for name in ("backup", "export", "schema", "preimage_review", "journal",
                             "native_webull", "native_schwab", "maintenance", "installed_permit_proof", "refusal_log")}}
        parent["payload"]["repair_MOMSTUCK2_20261021"] = receipt
        order.update(id=oid, intent_id=pid, symbol="SPHL", quantity="26.00000000", client_order_id="ml1-SPHL-e-9ba4a2c0aacc5dbaa444",
                     updated_at=parent["updated_at"])
        order["payload"] = {"episode_id": pid, "momlive_entry_never_dispatched": True,
            "reject_reason": "refused_before_wire", "repair_MOMSTUCK2_20261021": deepcopy(receipt)}
    return model_values(parent), model_values(order)


def database_case(monkeypatch, writer, fault):
    parent_values, order_values = repair_rows(writer)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        account = BrokerAccount(id=parent_values["broker_account_id"],
            name=PRIMARY if writer == "legacy" else MIRROR,
            provider="schwab" if writer == "legacy" else "webull", environment="live", is_active=True)
        other = BrokerAccount(name=MIRROR if writer == "legacy" else PRIMARY,
            provider="webull" if writer == "legacy" else "schwab", environment="live", is_active=True)
        mom = Strategy(id=parent_values["strategy_id"], code="momentum_warn_live", name="Controlled strategy")
        own = Strategy(code="schwab_1m_v2", name="Controlled ATR")
        session.add_all([account, other, mom, own])
        session.flush()
        parent, order = TradeIntent(**parent_values), BrokerOrder(**order_values)
        if fault == "bare_status":
            parent.payload = {**parent.payload, "repair_O_1019_26": None, "repair_MOMSTUCK2_20261021": None}
        elif fault == "native_id":
            order.broker_order_id = "SYNTHETIC-possible-wire"
        elif fault == "submitted":
            order.submitted_at = CUT - timedelta(minutes=10)
        elif fault == "permit":
            order.payload = {**order.payload, "bot_control_permit": "SYNTHETIC-unknown"}
        elif fault == "wrong_account":
            order.broker_account_id = other.id
        elif fault == "working":
            order.status = "accepted"
        elif fault == "settling":
            order.updated_at = CUT - timedelta(seconds=1)
        elif fault == "missing_clock":
            parent.payload["momlive_episode"].pop("first_submit_ns")
        elif fault == "wrong_episode":
            order.payload["episode_id"] = str(uuid4())
        elif fault == "wrong_quantity":
            order.quantity += 1
        elif fault in {"wrong_receipt", "copy_only"}:
            key = "repair_O_1019_26" if writer == "legacy" else "repair_MOMSTUCK2_20261021"
            parent.payload[key]["actor"] = "SYNTHETIC-unreviewed" if fault == "wrong_receipt" else "main-offline-copy-only"
            if fault == "copy_only":
                parent.payload[key]["scope"] = "COPY_ONLY"
        session.add_all([parent, order])
        session.flush()
        if fault == "unknown_event":
            session.add(BrokerOrderEvent(order_id=order.id, event_source="unknown", event_type="accepted",
                                         event_at=CUT - timedelta(minutes=5), payload={}))
        elif fault == "fill":
            session.add(Fill(order_id=order.id, strategy_id=mom.id, broker_account_id=account.id,
                symbol=order.symbol, side="buy", quantity=1, price=1, filled_at=CUT - timedelta(minutes=5)))
        elif fault == "replacement":
            session.add(BrokerOrder(intent_id=parent.id, strategy_id=mom.id, broker_account_id=account.id,
                symbol=order.symbol, side="buy", order_type="limit", time_in_force="day", quantity=1,
                client_order_id="SYNTHETIC-second-child", status="accepted", payload={}))
        elif fault == "control_attempt":
            session.add(BotControlState(strategy_id=mom.id, execution_mode="live", generation=1,
                                        revision=0, desired_state="STOP", updated_at=CUT))
            session.flush()
            command = BotControlCommand(strategy_id=mom.id, execution_mode="live", generation=1,
                expected_generation=0, revision=0, action="STOP", request_id=uuid4(),
                request_hash="a" * 64, account_scope={}, account_scope_hash="b" * 64,
                actor_reference="SYNTHETIC-author-control", reason="synthetic unknown-attempt hold",
                phase="UNKNOWN", active=True, inventory_census={}, accepted_at=CUT, updated_at=CUT)
            session.add(command)
            session.flush()
            session.add(BotControlAttempt(command_id=command.command_id, strategy_id=mom.id, account_id=account.id,
                execution_mode="live", generation=1, sequence_no=0, revision=0,
                symbol=order.symbol, binding_kind="episode", binding_key=str(parent.id),
                parent_intent_id=parent.id, entry_order_id=order.id, operation="CANCEL_ENTRY", status="UNKNOWN"))
        request = RemovedWait(order.symbol, int((CUT - timedelta(minutes=10)).timestamp() * 1000),
            "SYNTHETIC-current-clear", int((CUT - timedelta(minutes=2)).timestamp() * 1000),
            (PRIMARY, MIRROR), "retry_exhausted")
        for broker in (account, other):
            session.add(TradeIntent(strategy_id=own.id, broker_account_id=broker.id, symbol=order.symbol,
                side="buy", intent_type="cancel", quantity=0, status="cancelled", reason="synthetic control",
                created_at=CUT - timedelta(seconds=90), updated_at=CUT - timedelta(seconds=30),
                payload={"metadata": {"clearwait_removal_token": request.token,
                    "clearwait_opportunity_id": str(request.opportunity_id), "clearwait_buy_only": "true",
                    "reason": "retry_budget_exhausted"}}))
        # CONTROL ONLY: these fresh empty receipts are deliberately synthetic.
        record_receipts(session, [(PRIMARY, CUT, 0), (MIRROR, CUT, 0)], recorded_at=CUT)
        session.commit()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CUT.astimezone(tz) if tz is not None else CUT.replace(tzinfo=None)
    monkeypatch.setattr(service, "datetime", Clock)
    bot = service.SchwabV2BotService.__new__(service.SchwabV2BotService)
    bot.session_factory = factory
    bot.strategy = Row(_flip_owner_accounts={PRIMARY, MIRROR})
    return engine, factory, bot, request


@pytest.mark.parametrize("writer", ["legacy", "settlement"])
@pytest.mark.parametrize("fault", ["none", "bare_status", "native_id", "submitted", "permit", "wrong_account",
    "working", "settling", "missing_clock", "wrong_episode", "wrong_quantity", "wrong_receipt", "copy_only",
    "unknown_event", "fill", "replacement", "control_attempt"])
def test_exact_repair_closure_and_real_service_global_guard(monkeypatch, writer, fault):
    engine, factory, bot, request = database_case(monkeypatch, writer, fault)
    try:
        with factory() as session:
            assert working_buy_present(session, request.symbol, now=CUT) is (fault != "none")
        proof, = bot._fetch_fresh_sell_release_evidence((request,))
        assert proof.clear is (fault == "none")
        if fault != "none":
            assert proof.reason in {"working_buy_or_intent", "position_stays_owned", "own_position_evidence_UNKNOWN"}
        assert not bot.__dict__.get("_fresh_sell_read_generation")  # Reader does not release/cache.
    finally:
        engine.dispose()
