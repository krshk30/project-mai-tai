"""UNRUN L2 SOURCE. Main-only private DB replay, not native/process history.

Retained CHOW orders/audits/intents/dispatch are exact. Missing strategy and
owner/native-book runtime are CONTROLLED. Fresh C1 reports zero CHOW fills/
positions/managed rows; the private empty tables remain a fixture operand,
never worker query or original native-flat proof.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, select

from project_mai_tai import brokerdecides1_sync as sync
from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
from project_mai_tai.brokerdecides1_entry import entry_decision
from project_mai_tai.cancelack1 import _at, _closed_order, _closed_with_replacements, working_buy_present
from project_mai_tai.db.models import (BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot,
                                     Fill, Strategy, TradeIntent)
from project_mai_tai.entry_rejected_closure import (broker_rejected_never_accepted,
                                                 historical_country_rejected_accounts)
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from tests.unit.wbpa3_episode_port import ACCOUNT_NAMES, OPPORTUNITY, seed_episode

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA3_SOURCE_DIRECTORY"),
    reason="Requires the exact retained private CHOW exports in Main's bounded profile")


@pytest.fixture
def episode(tmp_path, monkeypatch):
    case = seed_episode(tmp_path)  # Main's frozen L1 retained-row loader, unchanged.
    observed = _at(case.observed)
    monkeypatch.setattr(sync, "now_ms", lambda: observed)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.observed.astimezone(tz) if tz else case.observed.replace(tzinfo=None)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    case.observed_ms = observed
    try:
        yield case
    finally:
        case.engine.dispose()


def settings():
    return Settings(_env_file=None, strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_max_age_ms=20_000,
        strategy_schwab_1m_v2_country_route_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNT_NAMES[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNT_NAMES[1])


def terminal_recovery(session):
    return SchwabV2BotService._terminal_unfilled_first_rest_opportunities(
        SimpleNamespace(settings=settings()), session, account_names=ACCOUNT_NAMES,
        unknown_opportunities={"CHOW": OPPORTUNITY})


def snapshot_sources(session):
    """Full current source-row fingerprint; no synthetic DB conservation proof."""
    result = {}
    for model in (BrokerAccount, Strategy, TradeIntent, BrokerOrder, BrokerOrderEvent, Fill, DashboardSnapshot):
        columns = tuple(inspect(model).column_attrs)
        rows = [{attr.key: deepcopy(getattr(row, attr.key)) for attr in columns}
                for row in session.scalars(select(model)).all()]
        result[model.__tablename__] = sorted(json.dumps(row, default=str, sort_keys=True) for row in rows)
    return result


def driver(case, inflight, fills, terminals, *, phase="resting", age=2, working=None):
    strategy = SchwabV2Strategy(settings())
    strategy._now_ms = lambda: case.observed_ms
    owners = FlipEntryOwnershipStore(case.factory)
    identities = FanoutSegmentIdentityStore(case.factory)
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: owners.record(record, active=active, reason=reason,
                                                    now=case.observed), restore_readable=True)
    strategy.configure_fanout_identity_persistence(
        lambda symbol, segment, active, reason: identities.record(symbol, segment, active, reason,
                                                                 now=case.observed))
    state = strategy.watchlist_state("CHOW")
    state.flip_owner_phase = phase
    state.flip_owner_opportunity_id = state.fanout_segment_id = OPPORTUNITY
    state.flip_owner_first_rest_placed = True
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = case.observed_ms
    state.atr_short_flip_bar_ts = case.observed_ms - 60000
    state.atr_state = "short"
    books = tuple(NativeAccountBook(account, provider, case.observed_ms - age,
        case.observed_ms - age + 1, frozenset({"CHOW"}) if provider == working else frozenset(),
        frozenset(), True, "CONTROLLED_NOT_NATIVE_PROCESS_FACTS")
        for account, provider in zip(ACCOUNT_NAMES, ("schwab", "webull"), strict=True))
    memory = strategy._brokerdecides1_memory = AdmissionMemory()
    memory.apply(AdmissionSnapshot("CONTROLLED", 1, case.observed_ms - age,
        case.observed_ms - age + 1, books, inflight, fills, True,
        "CONTROLLED_NATIVE_BOOK_LOCAL_CENSUS_REAL", terminals))
    return strategy, state


def test_retained_four_original_rejections_positive_closure_and_real_local_census(episode, record_property):
    case = episode
    with case.factory() as session:
        before = snapshot_sources(session)
        for raw in case.raw["orders"]:
            order = session.get(BrokerOrder, UUID(raw["id"]))
            event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
            assert order.broker_order_id is None and _at(event.event_at) < _at(order.submitted_at)
            assert broker_rejected_never_accepted(session, order, case.observed_ms)
            assert _closed_order(session, order, case.observed_ms)
            assert _closed_with_replacements(session, order, case.observed_ms)
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
        expected = frozenset(("CHOW", row["payload"]["attempt_token"]) for row in case.dispatch
                             if row["payload"]["kind"] == "attempt")
        assert len(expected) == 4 and terminals == expected
        assert not inflight and not fills
        assert not working_buy_present(session, "CHOW", now=case.observed)
        assert snapshot_sources(session) == before
    record_property("OLD_actual_replay_receipt_sha256", "673f80392e2a84ad7a0d9aa08ed25ebdb598b0b0852e76056972402b042bcf9d")
    record_property("NEW_scope", "ACTUAL_EXPORTED_LOCAL_ROWS; C1_REPORTED_ZERO_CENSUS; NO_NATIVE_FLAT_CLAIM")


def test_real_country_recovery_reader_and_actual_sell_retirement_with_fresh_flat_operands(episode):
    case = episode
    with case.factory() as session:
        before = snapshot_sources(session)
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) == frozenset({"live:orb"})
        terminal, evaluated = terminal_recovery(session)
        assert terminal == {"CHOW": frozenset({OPPORTUNITY})} and evaluated == 1
        assert snapshot_sources(session) == before
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
    strategy, state = driver(case, inflight, fills, terminals, phase="unknown")
    assert strategy._sell_owner_current_blocker(state) == ""
    # Real L12 exception admission requires this fresh, complete native-flat
    # CONTROLLED card; neither terminal rows nor the waiver manufacture it.
    assert entry_decision(strategy, "CHOW").allowed
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "idle"
    assert state.fanout_segment_id == state.flip_owner_opportunity_id == 0
    with case.factory() as session:
        inactive = list(session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership")))
        assert inactive and inactive[-1].payload["active"] is False
        # Reader proof never changes source accounting rows. Only the real
        # normal strategy persistence writes owner/segment snapshots above.
        after = snapshot_sources(session)
        assert all(after[key] == before[key] for key in before if key != "dashboard_snapshots")


@pytest.mark.parametrize("fault", ["missing_audit", "unknown_origin", "timeout_no_reply", "foreign_coid",
    "accepted_before_reject", "native_id_empty", "fill", "positive_partial", "unknown_metadata",
    "future_event", "future_order", "other_reject_code", "no_error_code", "http_unknown",
    "missing_native_field", "quantity_mismatch", "dispatch_token_mismatch", "real_working_native"])
def test_unknown_unanswered_conflicting_or_executed_entry_never_closes(episode, fault):
    case = episode
    with case.factory.begin() as session:
        order = session.get(BrokerOrder, UUID(case.raw["orders"][0]["id"]))
        parent = session.get(TradeIntent, order.intent_id)
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        p = deepcopy(event.payload)
        if fault == "missing_audit": session.delete(event)
        elif fault == "unknown_origin": event.event_source = "unknown"
        elif fault == "timeout_no_reply":
            event.event_source = "unknown"
            p["metadata"].pop("webull_error_code")
            p["metadata"].pop("webull_http_status")
            parent.payload = {**parent.payload, "refusal_origin": "could_not_tell"}
        elif fault == "foreign_coid": p["client_order_id"] = "CONTROLLED-FOREIGN"
        elif fault == "accepted_before_reject":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                event_at=event.event_at - timedelta(milliseconds=1), payload=deepcopy(event.payload)))
        elif fault == "native_id_empty": order.broker_order_id = ""
        elif fault == "fill":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, broker_fill_id="CONTROLLED-fill", symbol="CHOW",
                side="buy", quantity=Decimal(1), price=Decimal("6.26"), filled_at=event.event_at,
                payload={"provenance": "CONTROLLED adverse, original raw unchanged"}))
        elif fault == "positive_partial":
            event.event_type = "partially_filled"
            p["filled_quantity"] = "1"
        elif fault == "unknown_metadata": parent.payload = {**parent.payload, "metadata": None}
        elif fault == "future_event": event.event_at = case.observed + timedelta(seconds=1)
        elif fault == "future_order": order.updated_at = case.observed + timedelta(seconds=1)
        elif fault == "other_reject_code": p["metadata"]["webull_error_code"] = "CONTROLLED-OTHER-417"
        elif fault == "no_error_code": p["metadata"].pop("webull_error_code")
        elif fault == "http_unknown": p["metadata"].pop("webull_http_status")
        elif fault == "missing_native_field": p.pop("broker_order_id")
        elif fault == "quantity_mismatch": parent.quantity -= 1
        elif fault == "dispatch_token_mismatch":
            payload = deepcopy(parent.payload)
            payload["metadata"]["clearwait_dispatch_token"] = "CONTROLLED-UNMATCHED"
            parent.payload = payload
        elif fault == "real_working_native":
            order.status, parent.status, event.event_type = "accepted", "submitted", "accepted"
            order.broker_order_id = p["broker_order_id"] = "CONTROLLED-WEBULL-WORKING"
            p["metadata"].pop("webull_error_code")
            p["metadata"].pop("webull_http_status")
        if fault != "missing_audit": event.payload = p
    with case.factory() as session:
        before = snapshot_sources(session)
        order = session.get(BrokerOrder, UUID(case.raw["orders"][0]["id"]))
        assert not broker_rejected_never_accepted(session, order, case.observed_ms)
        assert not _closed_order(session, order, case.observed_ms)
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
        assert "CHOW" in inflight
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) is None
        assert terminal_recovery(session)[0] == {}
        assert snapshot_sources(session) == before
    strategy, state = driver(case, inflight, fills, terminals)
    assert strategy._sell_owner_current_blocker(state) == "unanswered_submit"
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown" and state.flip_owner_opportunity_id == OPPORTUNITY


def test_generic_Schwab_native_closure_and_real_working_buy_path_are_unchanged(episode):
    case = episode
    with case.factory.begin() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCOUNT_NAMES[0]))
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        parent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="SCHWABCTRL",
            side="buy", intent_type="open", quantity=Decimal(1), status="cancelled", reason="CONTROLLED legacy",
            payload={"metadata": {}}, created_at=case.observed - timedelta(seconds=60),
            updated_at=case.observed - timedelta(seconds=30))
        session.add(parent); session.flush()
        order = BrokerOrder(intent_id=parent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="CONTROLLED-SCHWAB-LEGACY", broker_order_id="CONTROLLED-SCHWAB-NATIVE",
            symbol="SCHWABCTRL", side="buy", order_type="stop_limit", time_in_force="day",
            quantity=Decimal(1), status="cancelled", payload={},
            submitted_at=case.observed - timedelta(seconds=55), updated_at=case.observed - timedelta(seconds=30))
        session.add(order); session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
            event_at=case.observed - timedelta(seconds=30), payload={"client_order_id": order.client_order_id,
                "broker_order_id": order.broker_order_id, "broker_fill_id": None}))
        identity = order.id
    with case.factory() as session:
        order = session.get(BrokerOrder, identity)
        assert not broker_rejected_never_accepted(session, order, case.observed_ms)
        assert _closed_order(session, order, case.observed_ms)
        assert _closed_with_replacements(session, order, case.observed_ms)
        assert not working_buy_present(session, "SCHWABCTRL", now=case.observed)
    with case.factory.begin() as session:
        order = session.get(BrokerOrder, identity)
        order.status = "working"
        session.get(TradeIntent, order.intent_id).status = "submitted"
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == identity))
        event.event_type = "accepted"
    with case.factory() as session:
        order = session.get(BrokerOrder, identity)
        assert not broker_rejected_never_accepted(session, order, case.observed_ms)
        assert not _closed_order(session, order, case.observed_ms)
        assert working_buy_present(session, "SCHWABCTRL", now=case.observed)


@pytest.mark.parametrize("fault", ["missing_begin", "opposite_attempt", "extra_open", "older_active_other_strategy"])
def test_complete_historical_country_scope_never_waives_an_unanswered_other_entry(episode, fault):
    case = episode
    with case.factory.begin() as session:
        journal = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
        original = session.get(TradeIntent, UUID(case.raw["intents"][0]["id"]))
        if fault == "missing_begin":
            session.delete(next(row for row in journal if row.payload["kind"] == "begin"))
        elif fault == "opposite_attempt":
            source = next(row for row in journal if row.payload["kind"] == "attempt")
            session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch", created_at=source.created_at,
                payload={**source.payload, "attempt_token": str(uuid4()), "account_name": ACCOUNT_NAMES[0]}))
        else:
            strategy_id = original.strategy_id
            created_at = case.observed - timedelta(seconds=30)
            if fault == "older_active_other_strategy":
                strategy = Strategy(code="CONTROLLED_other", name="CONTROLLED other active owner", execution_mode="live")
                session.add(strategy); session.flush()
                strategy_id = strategy.id
                created_at = datetime.fromtimestamp(OPPORTUNITY / 1000, UTC) - timedelta(minutes=1)
            session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=original.broker_account_id,
                symbol="CHOW", side="buy", intent_type="open", quantity=Decimal(1), status="pending",
                reason="CONTROLLED unanswered entry", payload={"event_id": str(uuid4()), "metadata": {}},
                created_at=created_at, updated_at=created_at))
    with case.factory() as session:
        before = snapshot_sources(session)
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) is None
        assert terminal_recovery(session)[0] == {}
        assert "CHOW" in sync.local_evidence(session, ACCOUNT_NAMES)[0]
        assert snapshot_sources(session) == before


@pytest.mark.parametrize("fault", ["stale25", "working_webull", "working_schwab", "native_unknown"])
def test_local_closure_does_not_manufacture_fresh_flat_L12_admission(episode, fault):
    case = episode
    with case.factory() as session:
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
    assert not inflight
    strategy, state = driver(case, inflight, fills, terminals, phase="unknown",
        age=25000 if fault == "stale25" else 2,
        working=fault.removeprefix("working_") if fault.startswith("working_") else None)
    if fault == "native_unknown":
        memory = strategy._brokerdecides1_memory
        previous = memory.snapshot
        memory.apply(replace(previous, generation=2,
            books=(replace(previous.books[0], complete=False), previous.books[1])))
    decision = entry_decision(strategy, "CHOW")
    assert not decision.allowed
    assert decision.reason == {"stale25": "snapshot_stale", "working_webull": "native_working_order",
        "working_schwab": "native_working_order", "native_unknown": "native_coverage_UNKNOWN"}[fault]
    # Do not impose a new L12 veto on OLD's normal SELL reset. This control is
    # the actual UNKNOWN-owner exception admission; no ordinary entry is emitted.


def add_controlled_later_opportunity(session, case, *, answered):
    """A separate later generation, never an alteration of retained CHOW rows."""
    root = session.get(TradeIntent, UUID(case.raw["orders"][0]["intent_id"]))
    raw_order = session.get(BrokerOrder, UUID(case.raw["orders"][0]["id"]))
    raw_event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == raw_order.id))
    opportunity = case.observed_ms - 120000
    at = lambda offset: datetime.fromtimestamp((opportunity + offset) / 1000, UTC)
    token, event_id = str(uuid4()), uuid4()
    common = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "CHOW",
        "opportunity_id": opportunity, "account_names": list(ACCOUNT_NAMES)}
    session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch", created_at=at(1),
        payload={**common, "kind": "begin", "attempt_token": "", "account_name": ""}))
    session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch", created_at=at(10),
        payload={**common, "kind": "attempt", "attempt_token": token, "account_name": ACCOUNT_NAMES[1]}))
    if not answered:
        return opportunity, token
    md = deepcopy(root.payload["metadata"])
    md.update(fanout_segment_id=str(opportunity), atr_country_segment_id=str(opportunity),
        fanout_slot_id=fanout_slot_id(strategy_code="schwab_1m_v2", symbol="CHOW",
            segment_id=opportunity, slot="resting"), rpg_resting_generation=str(uuid4()),
        webull_mirror_generation_id=str(uuid4()), clearwait_dispatch_token=token,
        atr_country_sealed_at=at(50).isoformat(), webull_wire_submitted_at_utc=at(200).isoformat())
    payload = {**deepcopy(root.payload), "event_id": str(event_id), "metadata": md}
    parent = TradeIntent(strategy_id=root.strategy_id, broker_account_id=root.broker_account_id,
        symbol="CHOW", side="buy", intent_type="open", quantity=root.quantity, status="rejected",
        reason="CONTROLLED independent later refusal", payload=payload, created_at=at(100), updated_at=at(350))
    session.add(parent); session.flush()
    coid = f"schwab_1m_v2-CHOW-open-{event_id.hex[:12]}"[:40]
    order = BrokerOrder(intent_id=parent.id, strategy_id=parent.strategy_id,
        broker_account_id=parent.broker_account_id, symbol="CHOW", side="buy",
        order_type=raw_order.order_type, time_in_force=raw_order.time_in_force,
        quantity=parent.quantity, status="rejected", client_order_id=coid, broker_order_id=None,
        payload=deepcopy(md), submitted_at=at(330), updated_at=at(340))
    session.add(order); session.flush()
    audit = {**deepcopy(raw_event.payload), "client_order_id": coid,
        "metadata": {**deepcopy(raw_event.payload["metadata"]), **deepcopy(md)}}
    session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected", event_source="broker",
        event_at=at(300), payload=audit))
    return opportunity, token


@pytest.mark.parametrize("answered", [True, False], ids=["later_closed", "later_unanswered"])
def test_later_known_opportunity_never_resurrects_old_rejected_orders(episode, answered):
    case = episode
    with case.factory.begin() as session:
        later_opportunity, later_token = add_controlled_later_opportunity(session, case, answered=answered)
        assert later_opportunity > OPPORTUNITY
    with case.factory() as session:
        before = snapshot_sources(session)
        for raw in case.raw["orders"]:
            old = session.get(BrokerOrder, UUID(raw["id"]))
            assert broker_rejected_never_accepted(session, old, case.observed_ms)
            assert _closed_order(session, old, case.observed_ms)
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
        original_tokens = frozenset(("CHOW", row["payload"]["attempt_token"]) for row in case.dispatch
            if row["payload"]["kind"] == "attempt")
        assert original_tokens.issubset(terminals) and len(original_tokens) == 4
        assert ("CHOW" in inflight) is (not answered)
        assert (("CHOW", later_token) in terminals) is answered
        assert not fills
        # Broader historical single-account recovery remains conservative even
        # when the old individual order closure is stable across generations.
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) is None
        assert terminal_recovery(session)[0] == {}
        assert snapshot_sources(session) == before


@pytest.mark.parametrize("unbound", [None, "UNKNOWN"])
def test_unknown_later_opportunity_is_never_blindly_excluded(episode, unbound):
    case = episode
    with case.factory.begin() as session:
        opportunity, token = add_controlled_later_opportunity(session, case, answered=False)
        for row in session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all():
            if row.payload.get("opportunity_id") == opportunity:
                row.payload = {**row.payload, "opportunity_id": unbound}
    with case.factory() as session:
        before = snapshot_sources(session)
        for raw in case.raw["orders"]:
            assert not broker_rejected_never_accepted(session,
                session.get(BrokerOrder, UUID(raw["id"])), case.observed_ms)
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) is None
        assert snapshot_sources(session) == before


@pytest.mark.parametrize("fault", ["accepted", "fill", "conflicting_audit"])
def test_later_executed_or_conflicting_order_still_holds_its_own_census(episode, fault):
    case = episode
    with case.factory.begin() as session:
        opportunity, token = add_controlled_later_opportunity(session, case, answered=True)
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.payload["clearwait_dispatch_token"].as_string() == token))
        event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        if fault == "accepted":
            order.status, order.broker_order_id = "accepted", "CONTROLLED-LATER-NATIVE"
            session.get(TradeIntent, order.intent_id).status = "submitted"
            event.event_type = "accepted"
            event.payload = {**event.payload, "broker_order_id": order.broker_order_id}
        elif fault == "fill":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, broker_fill_id="CONTROLLED-later-fill",
                symbol="CHOW", side="buy", quantity=Decimal(1), price=Decimal("6.26"),
                filled_at=event.event_at, payload={"provenance": "CONTROLLED later generation"}))
        else:
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                event_at=event.event_at - timedelta(milliseconds=1), payload=deepcopy(event.payload)))
    with case.factory() as session:
        before = snapshot_sources(session)
        for raw in case.raw["orders"]:
            assert _closed_order(session, session.get(BrokerOrder, UUID(raw["id"])), case.observed_ms)
        inflight, fills, terminals = sync.local_evidence(session, ACCOUNT_NAMES)
        original_tokens = frozenset(("CHOW", row["payload"]["attempt_token"]) for row in case.dispatch
            if row["payload"]["kind"] == "attempt")
        assert len(original_tokens) == 4 and original_tokens.issubset(terminals)
        assert "CHOW" in inflight and ("CHOW", token) not in terminals
        assert historical_country_rejected_accounts(session, "CHOW", OPPORTUNITY,
            ACCOUNT_NAMES, case.observed_ms) is None
        assert snapshot_sources(session) == before
