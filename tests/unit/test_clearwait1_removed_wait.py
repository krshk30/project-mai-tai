"""Recorded CLEARWAIT1 rows plus explicitly synthetic cancellation acknowledgements."""

from __future__ import annotations

import copy
import asyncio
import json
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base,
    BrokerOrder,
    BrokerOrderEvent,
    DashboardSnapshot,
    TradeIntent,
)
from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, TradeIntentDraft
from project_mai_tai.v2_removed_wait import (
    DISPATCH_SNAPSHOT_TYPE,
    RemovedWait,
    RemovedWaitProof,
    RemovedWaitStore,
    assess_removed_wait,
)

EVIDENCE = json.loads(
    (
        Path(__file__).parents[2] / "docs/review-artifacts/clearwait1/released_evidence.json"
    ).read_text()
)
PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"
NOW = datetime(2026, 10, 6, 17, 31, tzinfo=UTC)


def _row(data):
    fields = copy.deepcopy(data)
    for key in ("created_at", "updated_at", "submitted_at", "event_at"):
        if fields.get(key):
            fields[key] = datetime.fromisoformat(fields[key])
    fields["broker_account_id"] = fields.get("account", PRIMARY)
    return SimpleNamespace(**fields)


def _recorded(symbol, *, include_tickets=True, now=NOW, opportunity=None):
    opportunity = opportunity or int(EVIDENCE["current_opportunities"][symbol])
    request = RemovedWait(
        symbol,
        opportunity,
        "CONTROLLED-REMOVAL-RECEIPT",
        int((now - timedelta(minutes=1)).timestamp() * 1000),
        (PRIMARY, WEBULL),
    )

    def own(row):
        return row.get("symbol") == symbol and row.get("strategy") == "schwab_1m_v2"

    orders = [_row(r) for r in EVIDENCE["orders"] if own(r)]
    intents = [_row(r) for r in EVIDENCE["intents"] if own(r)]
    snapshots = [
        _row(r)
        for r in EVIDENCE["owner_snapshots"]
        if r["snapshot_type"] == "v2_flip_entry_ownership" and r["payload"].get("symbol") == symbol
    ]
    if include_tickets:
        snapshots += [_row(r) for r in EVIDENCE["retry_owners"]]
    # A controlled complete future journal, NOT reconstructed historical coverage.
    # Old live episodes lacking this positive coverage remain unknown on upgrade.
    base = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": symbol,
            "opportunity_id": str(opportunity), "account_names": [PRIMARY, WEBULL]}
    snapshots.append(SimpleNamespace(
        id="controlled-begin", snapshot_type=DISPATCH_SNAPSHOT_TYPE,
        created_at=now - timedelta(minutes=2), payload={**base, "kind": "begin"},
    ))
    tokens = {}
    for intent in intents:
        if intent.intent_type != "open":
            continue
        md = intent.payload.get("metadata", {})
        if str(md.get("fanout_segment_id", "")) != str(opportunity):
            continue
        token = "controlled-attempt-" + str(intent.id)
        tokens[intent.id] = token
        md["clearwait_dispatch_token"] = token
        snapshots.append(SimpleNamespace(
            id=token, snapshot_type=DISPATCH_SNAPSHOT_TYPE,
            created_at=now - timedelta(minutes=2),
            payload={**base, "kind": "attempt", "attempt_token": token,
                     "account_name": intent.broker_account_id},
        ))
    for order in orders:
        if order.intent_id in tokens:
            order.payload["clearwait_dispatch_token"] = tokens[order.intent_id]
    snapshots.append(
        SimpleNamespace(
            id="controlled-request",
            snapshot_type="v2_removed_wait",
            payload=request.payload(active=True),
        )
    )
    # The old records predate this protocol. These are controlled future receipts,
    # not historical broker evidence or a claim that six live re-adds would work.
    for account in (PRIMARY, WEBULL):
        intents.append(
            SimpleNamespace(
                id="controlled-" + account,
                symbol=symbol,
                broker_account_id=account,
                intent_type="cancel",
                status="rejected",
                created_at=now - timedelta(seconds=40),
                updated_at=now - timedelta(seconds=30),
                payload={
                    "metadata": {
                        "clearwait_removal_token": request.token,
                        "clearwait_opportunity_id": str(opportunity),
                        "clearwait_buy_only": "true",
                    },
                    "refusal_origin": "skipped_before_submit",
                    "refusal_code": "cancel_target_not_found",
                },
            )
        )
    return request, dict(
        now=now,
        accounts={PRIMARY: PRIMARY, WEBULL: WEBULL},
        orders=orders,
        intents=intents,
        snapshots=snapshots,
        has_position=False,
        filled_order_ids={
            r["order_id"] for r in EVIDENCE["fills"] if r["strategy"] == "schwab_1m_v2"
        },
        order_events=[
            _row(r) for r in EVIDENCE["order_events"] if r["order_id"] in {o.id for o in orders}
        ],
    )


@pytest.mark.parametrize("symbol", ["AIXI", "XHG", "AIFA"])
def test_recorded_terminal_rows_with_controlled_receipts_clear(symbol):
    request, rows = _recorded(symbol, include_tickets=False)
    proof = assess_removed_wait(request, **rows)
    assert proof.clear, proof.reason


def test_recorded_xhg_unknown_ticket_overrules_rejected_order():
    request, rows = _recorded("XHG")
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear
    assert proof.reason == "replacement_ticket_not_terminal"


def test_recorded_aixi_different_old_unknown_is_not_retired_or_waived():
    request, rows = _recorded("AIXI")
    before = copy.deepcopy(EVIDENCE["retry_owners"])
    assert assess_removed_wait(request, **rows).clear
    assert EVIDENCE["retry_owners"] == before
    assert any(
        r["payload"].get("phase") == "held_unknown"
        and r["payload"].get("segment_id") != request.opportunity_id
        for r in before
    )


def test_recorded_aifa_latest_cancel_not_first_cancel_controls_settle():
    early = datetime(2026, 10, 6, 15, 22, 11, tzinfo=UTC)
    request, rows = _recorded("AIFA", include_tickets=False, now=early)
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear
    assert proof.reason == "terminal_order_settling"
    rows["now"] = datetime(2026, 10, 6, 15, 22, 26, tzinfo=UTC)
    assert assess_removed_wait(request, **rows).clear


@pytest.mark.parametrize(
    "fault",
    [
        "missing_receipt",
        "unknown_cancel",
        "fresh_cancel",
        "late_fill",
        "filled_order",
        "pending_order",
        "position",
        "position_id",
        "unreadable_snapshot",
        "missing_request",
        "pending_open",
        "unbound_new_open",
        "nfq_hold",
        "rpg_unknown",
        "partial_fill",
        "generic_absence",
        "unclassified_abort",
        "missing_webull",
    ],
)
def test_acceptance_faults_fail_closed(fault):
    request, rows = _recorded("AIXI", include_tickets=False)
    if fault == "missing_receipt":
        rows["intents"].pop()
    elif fault == "unknown_cancel":
        rows["intents"][-1].payload["refusal_code"] = "network_unknown"
    elif fault == "fresh_cancel":
        rows["intents"][-1].updated_at = NOW
    elif fault == "late_fill":
        rows["filled_order_ids"].add(rows["orders"][0].id)
    elif fault in {"filled_order", "pending_order", "partial_fill"}:
        rows["orders"][0].status = {
            "filled_order": "filled",
            "pending_order": "pending",
            "partial_fill": "partially_filled",
        }[fault]
    elif fault == "position":
        rows["has_position"] = True
    elif fault == "position_id":
        next(
            r
            for r in rows["snapshots"]
            if str(r.payload.get("opportunity_id")) == str(request.opportunity_id)
        ).payload["position_ids"] = {WEBULL: "owned"}
    elif fault == "unreadable_snapshot":
        rows["snapshots"][0].payload = None
    elif fault == "missing_request":
        rows["snapshots"].pop()
    elif fault in {"pending_open", "unbound_new_open"}:
        intent = copy.deepcopy(rows["intents"][0])
        intent.created_at = NOW
        if fault == "pending_open":
            intent.status = "pending"
        else:
            intent.payload["metadata"] = {}
        rows["intents"].append(intent)
    elif fault == "nfq_hold":
        rows["snapshots"].append(
            SimpleNamespace(
                id="nfq",
                snapshot_type="oms_webull_mirror_price_hold",
                payload={
                    "phase": "uncertain",
                    "event": {
                        "payload": {
                            "strategy_code": "schwab_1m_v2",
                            "symbol": "AIXI",
                            "metadata": rows["orders"][0].payload,
                        }
                    },
                },
            )
        )
    elif fault in {"generic_absence", "unclassified_abort"}:
        rows["intents"][0].payload.pop("refusal_origin", None)
        if fault == "unclassified_abort":
            rows["intents"][0].status = "aborted"
    elif fault == "missing_webull":
        rows["accounts"].pop(WEBULL)
    else:
        ticket = copy.deepcopy(EVIDENCE["retry_owners"][0])
        ticket["payload"]["segment_id"] = request.opportunity_id
        rows["snapshots"].append(_row(ticket))
    assert not assess_removed_wait(request, **rows).clear


def test_recorded_foreign_jagx_fill_is_not_own_fill_but_ambiguous_owner_stays_closed():
    request, rows = _recorded("JAGX", opportunity=1791293419343)
    assert rows["orders"] == []
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear and proof.reason == "filled_or_ambiguous_owner"
    foreign = next(r for r in EVIDENCE["fills"] if r["symbol"] == "JAGX")
    assert foreign["order_id"] not in rows["filled_order_ids"]


def test_recorded_own_mi_fill_cannot_become_unfilled_after_close():
    request, rows = _recorded("MI", opportunity=1791207240886)
    assert not assess_removed_wait(request, **rows).clear


def _strategy(*, enabled=True, eh=False):
    strategy = SchwabV2Strategy(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_removed_wait_clear_enabled=enabled,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
            strategy_schwab_1m_v2_account_name=PRIMARY,
            strategy_schwab_1m_v2_webull_account_name=WEBULL,
        )
    )
    clock = [int(NOW.timestamp() * 1000)]
    strategy._now_ms = lambda: clock[0]
    writes = []
    strategy.configure_fanout_identity_persistence(lambda *args: writes.append(("identity", args)))
    strategy.configure_flip_entry_ownership(
        lambda *args: writes.append(("owner", args)), restore_readable=True
    )
    strategy.configure_removed_wait(
        lambda *args: writes.append(("removal", args)), restored={}, readable=True
    )
    state = strategy.watchlist_state("AIXI")
    state.flip_owner_opportunity_id = state.fanout_segment_id = 1791294242804
    state.flip_owner_phase = "resting"
    state.flip_owner_first_rest_placed = True
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]
    state.resting_active = True
    state.resting_is_broker_order = not eh
    state.webull_resting_active = not eh
    state.cw_armed = True
    state.cw_resting_taken = True
    return strategy, state, clock, writes


@pytest.mark.parametrize("eh", [False, True])
def test_removal_cancel_then_terminal_clear_readd_new_episode_both_sessions(eh):
    strategy, state, clock, writes = _strategy(eh=eh)
    old = state.fanout_segment_id
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    assert not state.cw_armed and not state.resting_active
    assert not strategy.line_buy_ready("AIXI")
    for queue in (strategy._pending_intents, strategy._pending_webull_direct_intents):
        assert any(d.metadata.get("clearwait_removal_token") == request.token for d in queue)
        assert all(
            d.intent_type == "cancel" and d.metadata["clearwait_buy_only"] == "true" for d in queue
        )
    strategy.scanner_readded("AIXI")
    assert not strategy.line_buy_ready("AIXI")
    strategy.apply_removed_wait_proofs(
        [RemovedWaitProof(request, clock[0], True, "controlled_terminal")]
    )
    assert state.flip_owner_phase == "idle" and state.fanout_segment_id == 0
    assert strategy.line_buy_ready("AIXI")
    assert strategy._ensure_flip_owner_opportunity(state) > old
    assert [kind for kind, _ in writes[:4]] == ["removal", "identity", "owner", "removal"]


@pytest.mark.parametrize("owner", ["fill", "position_id", "open_position", "consumed", "held"])
def test_filled_owner_and_consumed_semantics_survive_removal(owner):
    strategy, state, clock, _ = _strategy()
    if owner == "fill":
        state.flip_owner_fill_accounts.add(WEBULL)
    elif owner == "position_id":
        state.flip_owner_position_ids[WEBULL] = "owned"
    elif owner == "open_position":
        state.flip_owner_open_positions[WEBULL] = SimpleNamespace(quantity=1)
    elif owner == "held":
        state.position_qty_held = 1
    else:
        state.flip_owner_phase = "consumed"
    before = state.flip_owner_phase
    strategy.release_and_drop_symbol("AIXI")
    assert strategy._removed_wait_requests == {}
    assert state.flip_owner_phase == before and state.flip_owner_opportunity_id > 0
    strategy.scanner_readded("AIXI")
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "changed_token",
        "late_fill",
        "identity_write",
        "owner_write",
        "removal_write",
        "seed_cap",
        "restore_unreadable",
    ],
)
def test_proof_application_fences_and_seed_cap_preservation(fault):
    strategy, state, clock, _ = _strategy()
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    observed = clock[0]
    if fault == "stale":
        observed -= 15_001
    elif fault == "changed_token":
        request = RemovedWait(
            request.symbol,
            request.opportunity_id,
            "different",
            request.requested_at_ms,
            request.account_names,
        )
    elif fault == "late_fill":
        state.flip_owner_fill_accounts.add(PRIMARY)
    elif fault == "seed_cap":
        state.cw_seed_cap_watch_start_ms = clock[0] - 1
    elif fault == "restore_unreadable":
        strategy._removed_wait_restore_readable = False
    else:

        def fail(*args):
            raise RuntimeError("controlled durable write failure")

        if fault == "identity_write":
            strategy._fanout_identity_persist = fail
        elif fault == "owner_write":
            strategy._flip_owner_persist = fail
        else:
            strategy._removed_wait_persist = fail
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, observed, True, "controlled")])
    if fault == "seed_cap":
        assert state.cw_resting_taken
    else:
        assert strategy._removed_wait_requests
        assert not strategy.line_buy_ready("AIXI")


def test_removed_about_to_place_drops_queued_opens_without_minting_identity():
    strategy, state, clock, _ = _strategy()
    state.flip_owner_opportunity_id = state.fanout_segment_id = 0
    state.flip_owner_first_rest_placed = False
    draft = TradeIntentDraft(
        symbol="AIXI", side="buy", intent_type="open", quantity=1, reason="controlled",
        metadata={"fanout_segment_id": str(strategy.watchlist_state("AIXI").fanout_segment_id)},
    )
    for queue in (
        strategy._pending_intents,
        strategy._pending_webull_direct_intents,
        strategy._pending_webull_fanout_intents,
    ):
        queue.append(draft)
    strategy.release_and_drop_symbol("AIXI")
    assert strategy._removed_wait_requests["AIXI"].opportunity_id == 0
    assert state.fanout_segment_id == 0
    assert all(
        d.intent_type != "open"
        for queue in (
            strategy._pending_intents,
            strategy._pending_webull_direct_intents,
            strategy._pending_webull_fanout_intents,
        )
        for d in queue
    )
    request = strategy._removed_wait_requests["AIXI"]
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "controlled")])
    assert strategy._removed_wait_requests and not strategy.line_buy_ready("AIXI")


def test_store_restart_request_and_rollback_default():
    assert Settings(_env_file=None).strategy_schwab_1m_v2_removed_wait_clear_enabled
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = RemovedWaitStore(sessionmaker(bind=engine))
    request, _ = _recorded("AIXI")
    store.record(request, True)
    assert store.restore() == {"AIXI": request}
    strategy, _, _, _ = _strategy()
    strategy.configure_removed_wait(store.record, restored=store.restore(), readable=True)
    strategy.scanner_readded("AIXI")
    assert not strategy.line_buy_ready("AIXI")
    store.record(request, False)
    assert store.restore() == {}
    strategy, state, _, _ = _strategy(enabled=False)
    strategy.release_and_drop_symbol("AIXI")
    assert not strategy._removed_wait_requests and state.flip_owner_phase == "unknown"


def test_clearwait_cancellation_cannot_target_protective_sell():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    strategy_id, account_id = uuid4(), uuid4()
    with sessions() as session:
        sell = BrokerOrder(
            strategy_id=strategy_id,
            broker_account_id=account_id,
            symbol="AIXI",
            side="sell",
            client_order_id="controlled-exit",
            order_type="STOP",
            time_in_force="day",
            quantity=1,
            status="accepted",
            payload={},
        )
        session.add(sell)
        session.commit()
        store = OmsStore()
        assert (
            store.find_open_order_for_cancel(
                session,
                strategy_id=strategy_id,
                broker_account_id=account_id,
                symbol="AIXI",
                metadata={},
            )
            is sell
        )
        for metadata in (
            {"clearwait_buy_only": "true"},
            {"clearwait_buy_only": "true", "target_client_order_id": sell.client_order_id},
        ):
            assert (
                store.find_open_order_for_cancel(
                    session,
                    strategy_id=strategy_id,
                    broker_account_id=account_id,
                    symbol="AIXI",
                    metadata=metadata,
                )
                is None
            )
        buy = BrokerOrder(
            strategy_id=strategy_id,
            broker_account_id=account_id,
            symbol="AIXI",
            side="buy",
            client_order_id="controlled-old-buy",
            order_type="STOP_LIMIT",
            time_in_force="day",
            quantity=1,
            status="accepted",
            payload={"fanout_segment_id": "old"},
        )
        session.add(buy)
        session.commit()
        assert (
            store.find_open_order_for_cancel(
                session,
                strategy_id=strategy_id,
                broker_account_id=account_id,
                symbol="AIXI",
                metadata={"clearwait_buy_only": "true", "fanout_segment_id": "different"},
            )
            is None
        )


def test_real_db_reader_replays_recorded_aixi_and_requires_configured_accounts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    store = RemovedWaitStore(sessions)
    request, rows = _recorded("AIXI", include_tickets=False)
    oms = OmsStore()
    with sessions() as session:
        strategy = oms.ensure_strategy(session, "schwab_1m_v2")
        account_ids = {
            name: oms.ensure_broker_account(
                session, name, provider="webull" if name == WEBULL else "schwab", environment="live"
            ).id
            for name in request.account_names
        }
        for r in rows["intents"]:
            session.add(
                TradeIntent(
                    id=uuid4() if r.id.startswith("controlled-") else UUID(r.id),
                    strategy_id=strategy.id,
                    broker_account_id=account_ids[r.broker_account_id],
                    symbol="AIXI",
                    side="buy",
                    intent_type=r.intent_type,
                    quantity=1,
                    reason="recorded or controlled",
                    status=r.status,
                    payload=r.payload,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
            )
        for r in rows["orders"]:
            session.add(
                BrokerOrder(
                    id=UUID(r.id),
                    intent_id=UUID(r.intent_id),
                    strategy_id=strategy.id,
                    broker_account_id=account_ids[r.broker_account_id],
                    symbol="AIXI",
                    side="buy",
                    client_order_id=r.client_order_id,
                    broker_order_id=r.broker_order_id,
                    order_type=r.order_type,
                    time_in_force="day",
                    quantity=r.quantity,
                    status=r.status,
                    payload=r.payload,
                    submitted_at=r.submitted_at,
                    updated_at=r.updated_at,
                )
            )
        for r in rows["order_events"]:
            session.add(
                BrokerOrderEvent(
                    id=UUID(r.id),
                    order_id=UUID(r.order_id),
                    event_type=r.event_type,
                    event_at=r.event_at,
                    event_source=r.event_source,
                    payload=r.payload,
                )
            )
        session.add(
            DashboardSnapshot(
                snapshot_type="v2_removed_wait",
                payload=request.payload(active=True),
                created_at=NOW - timedelta(minutes=1),
            )
        )
        for row in rows["snapshots"]:
            if row.snapshot_type == DISPATCH_SNAPSHOT_TYPE:
                session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                                              payload=row.payload,
                                              created_at=row.created_at))
        session.commit()
    proof = store.proofs([request], {PRIMARY, WEBULL}, now=NOW)[0]
    assert proof.clear, proof.reason
    with pytest.raises(ValueError, match="configuration changed"):
        store.proofs([request], {PRIMARY}, now=NOW)
    with pytest.raises(ValueError, match="accounts unavailable"):
        store.proofs([request], {PRIMARY, "missing-webull"}, now=NOW)


def test_nfq_real_store_uses_same_row_on_retirement_and_old_held_does_not_block():
    request, rows = _recorded("AIXI", include_tickets=False)
    p = {
        "phase": "retired",
        "event": {
            "payload": {
                "strategy_code": "schwab_1m_v2",
                "symbol": "AIXI",
                "metadata": rows["orders"][0].payload,
            }
        },
    }
    rows["snapshots"].append(
        SimpleNamespace(
            id="stable-hold-row", snapshot_type="oms_webull_mirror_price_hold", payload=p
        )
    )
    assert assess_removed_wait(request, **rows).clear
    from project_mai_tai.oms.mirror_fresh_price import MirrorFreshPriceMixin, PriceHold
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    hold = PriceHold(
        TradeIntentEvent(
            source_service="controlled",
            payload=TradeIntentPayload(
                strategy_code="schwab_1m_v2",
                broker_account_name=WEBULL,
                symbol="AIXI",
                side="buy",
                intent_type="open",
                quantity=1,
                reason="controlled",
                metadata=rows["orders"][0].payload,
            ),
        ),
        uuid4(),
    )
    mixin = MirrorFreshPriceMixin()
    with sessions() as session:
        mixin._nfq_save(session, hold)
        session.commit()
        hold.phase = "retired"
        mixin._nfq_save(session, hold)
        session.commit()
        assert session.get(DashboardSnapshot, hold.row_id).payload["phase"] == "retired"


def test_scanner_departure_cancels_waiting_buy_even_protected_union_unchanged(monkeypatch):
    strategy, state, _, _ = _strategy()
    bot = SchwabV2BotService(strategy.settings)
    bot.strategy = strategy
    bot._watchlist = {"AIXI"}
    bot._clearwait_scanner_symbols = {"AIXI"}
    monkeypatch.setattr(bot, "_strategy_state_event_is_current", lambda _: True)
    monkeypatch.setattr(bot, "_extract_confirmed_symbols", lambda _: [])
    monkeypatch.setattr(bot, "_protected_symbols", lambda: {"AIXI"})
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: set())
    monkeypatch.setattr(bot, "_webull_ineligible_symbols", lambda: set())
    monkeypatch.setattr(bot, "_try_complete_boot_state_restoration", lambda *a, **k: None)
    event = StrategyStateSnapshotEvent(
        source_service="controlled", payload=StrategyStateSnapshotPayload()
    )
    bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=25)
    assert bot._watchlist == {"AIXI"}
    assert "AIXI" in strategy._removed_wait_requests and not state.cw_armed


@pytest.mark.parametrize("eh", [False, True])
def test_fresh_first_rest_after_clean_readd_really_places_new_episode(eh):
    strategy, state, clock, _ = _strategy(eh=eh)
    strategy._eh_resting_enabled = eh
    strategy._resting_session_is_eh = lambda now=None: eh
    strategy._resting_stop_ask_allows = lambda *a, **k: True
    strategy._liquidity_floor_ok = lambda *a: True
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    strategy.scanner_readded("AIXI")
    strategy._queue_resting_place(state, 2.6)
    assert not state.resting_active
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "controlled")])
    strategy._queue_resting_place(state, 2.6)
    assert state.resting_active and state.flip_owner_first_rest_placed
    assert state.flip_owner_opportunity_id > request.opportunity_id
    assert state.resting_is_broker_order is not eh


def test_accepted_retryoff_true_zero_allows_fresh_unfilled_episode_without_spending_budget():
    from tests.unit.test_v2_retry_one import _place_first, _strategy as retry_strategy

    strategy, clock, _ = retry_strategy(enabled=True, max_retries=0, dual=True)
    strategy.configure_removed_wait(lambda *a: None, restored={}, readable=True)
    state, old = _place_first(strategy, clock, "AIXI")
    budget = (state.retry_one_segment_id, state.retry_one_closes_in_segment)
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    strategy.scanner_readded("AIXI")
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "controlled_terminal")])
    strategy._queue_resting_place(state, 3.859, slot="first")
    assert strategy._retry_one_enabled and strategy._retry_one_max_retries == 0
    assert state.flip_owner_opportunity_id > old
    assert any(d.intent_type == "open" for d in strategy.drain_pending_intents())
    assert (state.retry_one_segment_id, state.retry_one_closes_in_segment) == budget


@pytest.mark.parametrize("reason", ["CONFIRMATION_EXIT", "CW_HARD_STOP"])
def test_accepted_retryoff_true_zero_keeps_closed_filled_episode_consumed_on_removal(reason):
    from tests.unit.test_v2_retry_one import (
        _close_primary, _fill_primary, _place_first, _strategy as retry_strategy,
    )

    strategy, clock, _ = retry_strategy(enabled=True, max_retries=0)
    strategy.configure_removed_wait(lambda *a: None, restored={}, readable=True)
    state, old = _place_first(strategy, clock, "AIXI")
    _fill_primary(strategy, clock, "AIXI", "controlled-owned-fill")
    _close_primary(strategy, clock, "AIXI", "controlled-owned-fill", reason)
    assert state.flip_owner_phase == "consumed"
    strategy.release_and_drop_symbol("AIXI")
    strategy.scanner_readded("AIXI")
    strategy._queue_resting_place(state, 3.859, slot="first")
    assert "AIXI" not in strategy._removed_wait_requests
    assert state.flip_owner_phase == "consumed" and state.flip_owner_opportunity_id == old
    assert not any(d.intent_type == "open" for d in strategy.drain_pending_intents())


def test_pending_removal_survives_legacy_recovery_sell_and_session_retire_paths():
    strategy, state, clock, _ = _strategy()
    strategy.release_and_drop_symbol("AIXI")
    old = state.flip_owner_opportunity_id
    state.flip_owner_phase = "unknown"
    assert not strategy._recover_unknown_flip_owner(state, (), (), frozenset({old}))
    assert not strategy._retire_flip_owner_opportunity(state, reason="atr_sell_flip")
    state.atr_session_anchor_ms = clock[0] - 86_400_000
    assert strategy.roll_stale_session_state(clock[0], is_protected=lambda *a: False) == []
    assert state.flip_owner_opportunity_id == old


def test_fill_racing_removal_ends_request_only_and_keeps_own_exit_episode():
    strategy, state, clock, _ = _strategy()
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    state.flip_owner_phase = "bound"
    state.flip_owner_fill_accounts.add(WEBULL)
    state.flip_owner_position_ids[WEBULL] = "owned-position"
    strategy.apply_removed_wait_proofs(
        [RemovedWaitProof(request, clock[0], False, "own_fill_stays_owned")]
    )
    assert not strategy._removed_wait_requests
    assert (
        state.flip_owner_phase == "bound"
        and state.flip_owner_position_ids[WEBULL] == "owned-position"
    )
    assert not strategy.line_buy_ready("AIXI")
    strategy.scanner_readded("AIXI")
    assert not strategy._strict_first_rest_admitted(state, slot="first")


def test_foreign_or_unknown_position_cannot_complete_pending_removal_as_own_fill():
    strategy, state, clock, _ = _strategy()
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    for reason in (
        "position_stays_managed",
        "filled_or_ambiguous_owner",
        "cancel_unknown_or_refused",
    ):
        strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], False, reason)])
    assert strategy._removed_wait_requests
    assert not state.flip_owner_fill_accounts and not state.flip_owner_position_ids


def test_quiet_symbol_removal_does_not_manufacture_episode_or_cancel():
    strategy, _, _, writes = _strategy()
    state = strategy.watchlist_state("QUIET")
    strategy.release_and_drop_symbol("QUIET")
    assert state.flip_owner_opportunity_id == 0
    assert "QUIET" not in strategy._removed_wait_requests
    assert not strategy._pending_intents and not strategy._pending_webull_direct_intents
    assert writes == []


def test_duplicate_configured_accounts_cannot_collapse_two_required_receipts_to_one():
    request, rows = _recorded("AIXI", include_tickets=False)
    request = replace(request, account_names=(PRIMARY, PRIMARY))
    rows["accounts"] = {PRIMARY: PRIMARY}
    assert assess_removed_wait(request, **rows).reason == "accounts_unreadable"


@pytest.mark.parametrize("kind", ["rpg", "nfq"])
def test_malformed_current_retry_metadata_is_unknown_not_evidence_of_absence(kind):
    request, rows = _recorded("AIXI", include_tickets=False)
    if kind == "rpg":
        ticket = copy.deepcopy(EVIDENCE["retry_owners"][0])
        ticket["payload"]["segment_id"] = request.opportunity_id
        ticket["payload"]["old"].pop("strategy_code")
        rows["snapshots"].append(_row(ticket))
    else:
        rows["snapshots"].append(
            SimpleNamespace(
                id="malformed",
                snapshot_type="oms_webull_mirror_price_hold",
                payload={"phase": "held", "event": {"payload": {"symbol": "AIXI", "metadata": {}}}},
            )
        )
    assert not assess_removed_wait(request, **rows).clear
    p = rows["snapshots"][-1].payload
    if kind == "rpg":
        p["old"]["strategy_code"] = "schwab_1m_v2"
        p["old"]["metadata"]["fanout_segment_id"] = str(request.opportunity_id)
        p.update(phase="expired", local_no_wire="false")
    else:
        p["event"]["payload"]["strategy_code"] = "schwab_1m_v2"
        p["event"]["payload"]["metadata"] = {**rows["orders"][0].payload, "fanout_slot": "reclaim"}
    assert not assess_removed_wait(request, **rows).clear


def test_broker_terminal_receipt_required_not_merely_local_terminal_status():
    request, rows = _recorded("AIXI", include_tickets=False)
    rows["order_events"] = []
    assert assess_removed_wait(request, **rows).reason == "broker_terminal_unproven"


def test_zero_episode_with_own_fill_history_is_unknown_not_a_new_unfilled_owner():
    request, rows = _recorded("MI", opportunity=1791207240886)
    request = replace(request, opportunity_id=0)
    assert (
        assess_removed_wait(request, **rows).reason == "missing_opportunity_with_own_fill_history"
    )


@pytest.mark.asyncio
async def test_already_emitting_open_finishes_before_both_cancel_barriers():
    strategy, _, _, _ = _strategy()
    bot = SchwabV2BotService(strategy.settings)
    bot.strategy = strategy
    bot._rpg_handoff_pass = AsyncMock()
    started, finish = asyncio.Event(), asyncio.Event()
    emitted = []

    async def emit(draft):
        if draft.intent_type == "open":
            started.set()
            await finish.wait()
        emitted.append(draft)

    bot.intent_emitter = bot.webull_intent_emitter = SimpleNamespace(emit=emit)
    draft = TradeIntentDraft(
        symbol="AIXI", side="buy", intent_type="open", quantity=1, reason="controlled",
        metadata={"fanout_segment_id": str(strategy.watchlist_state("AIXI").fanout_segment_id)},
    )
    task = asyncio.create_task(bot._emit_removal_tracked(bot.intent_emitter, draft))
    await asyncio.wait_for(started.wait(), 2)
    strategy.release_and_drop_symbol("AIXI")
    assert bot._removed_wait_emits_inflight()
    await bot._drain_direct_strategy_intents()
    assert emitted == []
    finish.set()
    await task
    assert not bot._removed_wait_emits_inflight()
    await bot._drain_direct_strategy_intents()
    assert emitted[0].intent_type == "open"
    assert all(d.intent_type == "cancel" for d in emitted[1:])
    assert len([d for d in emitted if d.metadata.get("clearwait_removal_token")]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("eh", [False, True])
async def test_draft_held_across_removal_cannot_dispatch_into_fresh_episode(eh):
    strategy, state, clock, _ = _strategy(eh=eh)
    bot = SchwabV2BotService(strategy.settings)
    bot.strategy = strategy
    emitter = SimpleNamespace(emit=AsyncMock(), broker_account_name=PRIMARY)
    old = state.fanout_segment_id
    draft = TradeIntentDraft(symbol="AIXI", side="buy", intent_type="open", quantity=1,
                             reason="controlled", metadata={"fanout_segment_id": str(old)})
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    strategy.scanner_readded("AIXI")
    strategy.apply_removed_wait_proofs([RemovedWaitProof(request, clock[0], True, "controlled_terminal")])
    assert strategy.line_buy_ready("AIXI")
    assert not await bot._emit_removal_tracked(emitter, draft)
    fresh = strategy._ensure_flip_owner_opportunity(state)
    assert fresh > old
    assert not await bot._emit_removal_tracked(emitter, draft)
    emitter.emit.assert_not_awaited()
    draft.metadata["fanout_segment_id"] = str(fresh)
    assert await bot._emit_removal_tracked(emitter, draft)
    emitter.emit.assert_awaited_once()


def test_no_target_receipts_and_missing_opening_rows_are_not_never_wire_proof():
    request, rows = _recorded("AIXI", include_tickets=False)
    rows["orders"] = []
    rows["order_events"] = []
    rows["intents"] = [i for i in rows["intents"] if i.intent_type == "cancel"]
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear and proof.reason == "dispatch_attempt_unproven"


def test_lost_later_generation_not_absolved_by_earlier_cancelled_order():
    request, rows = _recorded("AIXI", include_tickets=False)
    rows["snapshots"].insert(-1, SimpleNamespace(
        id="lost-later-attempt", snapshot_type=DISPATCH_SNAPSHOT_TYPE,
        payload={"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "AIXI",
                 "opportunity_id": str(request.opportunity_id), "account_names": [PRIMARY, WEBULL],
                 "kind": "attempt", "attempt_token": "lost-later-wire", "account_name": PRIMARY},
    ))
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear and proof.reason == "dispatch_attempt_unproven"


def test_restored_unknown_history_not_inferred_from_positive_known_terminal_rows():
    request, rows = _recorded("AIXI", include_tickets=False)
    rows["snapshots"] = [r for r in rows["snapshots"] if r.snapshot_type != DISPATCH_SNAPSHOT_TYPE]
    assert assess_removed_wait(request, **rows).reason == "dispatch_history_unknown"


def test_complete_local_begin_with_no_attempts_is_positive_never_wire():
    request, rows = _recorded("AIXI", include_tickets=False)
    rows["intents"] = [i for i in rows["intents"] if i.intent_type == "cancel"]
    rows["orders"] = []
    rows["order_events"] = []
    rows["snapshots"] = [r for r in rows["snapshots"]
                         if r.snapshot_type != DISPATCH_SNAPSHOT_TYPE or r.payload["kind"] == "begin"]
    assert assess_removed_wait(request, **rows).clear


@pytest.mark.asyncio
async def test_dispatch_journal_precedes_emit_and_failed_emit_remains_an_unresolved_attempt():
    strategy, state, _, _ = _strategy()
    journal, published = [], []
    strategy.configure_removed_wait(lambda *a: None, restored={}, readable=True,
                                    dispatch_persist=journal.append)
    state.flip_owner_opportunity_id = state.fanout_segment_id = 0
    strategy._ensure_flip_owner_opportunity(state)
    bot = SchwabV2BotService(strategy.settings)
    bot.strategy = strategy

    async def emit(draft):
        assert journal[-1]["kind"] == "attempt"
        assert journal[-1]["attempt_token"] == draft.metadata["clearwait_dispatch_token"]
        published.append(draft)
        raise RuntimeError("controlled transport unknown")

    emitter = SimpleNamespace(broker_account_name=PRIMARY, emit=emit)
    draft = TradeIntentDraft(symbol="AIXI", side="buy", intent_type="open", quantity=1,
                             reason="controlled", metadata={"fanout_segment_id": str(state.fanout_segment_id)})
    with pytest.raises(RuntimeError, match="transport unknown"):
        await bot._emit_removal_tracked(emitter, draft)
    assert [p["kind"] for p in journal] == ["begin", "attempt"]
    assert len(published) == 1

    def fail(payload):
        raise RuntimeError("controlled journal failure")

    strategy._removed_wait_dispatch_persist = fail
    assert not await bot._emit_removal_tracked(emitter, draft)
    assert len(published) == 1


def test_zero_identity_idle_and_no_target_are_not_positive_never_wire():
    request, rows = _recorded("AIXI", include_tickets=False)
    request = replace(request, opportunity_id=0)
    rows["orders"] = []
    rows["order_events"] = []
    rows["intents"] = [i for i in rows["intents"] if i.intent_type == "cancel"]
    for i in rows["intents"]:
        i.payload["metadata"]["clearwait_opportunity_id"] = "0"
    rows["snapshots"] = [SimpleNamespace(id="request", snapshot_type="v2_removed_wait",
                                          payload=request.payload(active=True))]
    assert assess_removed_wait(request, **rows).reason == "dispatch_history_unknown"
    rows["snapshots"][0].payload["local_never_wire"] = True
    assert not assess_removed_wait(request, **rows).clear


@pytest.mark.parametrize("fault", ["none", "restored_unknown", "broker_wait", "first_rest"])
def test_zero_waiting_identity_never_infers_no_wire_from_local_state(fault):
    strategy, state, _, _ = _strategy()
    state.flip_owner_opportunity_id = state.fanout_segment_id = 0
    state.flip_owner_first_rest_placed = False
    state.flip_owner_phase = "idle"
    state.resting_active = state.webull_resting_active = False
    if fault == "restored_unknown":
        state.flip_owner_phase = "unknown"
    elif fault == "broker_wait":
        state.resting_active = True
        state.resting_is_broker_order = True
    elif fault == "first_rest":
        state.flip_owner_first_rest_placed = True
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    assert request.opportunity_id == 0 and "local_never_wire" not in request.payload(active=True)


def test_fresh_fanout_bind_before_first_rest_journals_begin_once_not_on_restore():
    strategy, state, _, _ = _strategy()
    journal = []
    strategy.configure_removed_wait(lambda *a: None, restored={}, readable=True,
                                    dispatch_persist=journal.append)
    state.fanout_segment_id = state.flip_owner_opportunity_id = 0
    segment = strategy._ensure_fanout_segment_id(state)
    assert strategy._ensure_flip_owner_opportunity(state) == segment
    assert [p["kind"] for p in journal] == ["begin"]
    assert journal[0]["opportunity_id"] == str(segment)
    journal.clear()
    state.fanout_segment_id = state.flip_owner_opportunity_id = 0
    strategy._restored_fanout_segment_ids["AIXI"] = segment
    strategy._ensure_fanout_segment_id(state)
    assert journal == []


@pytest.mark.asyncio
async def test_actual_ordinary_oms_lost_transaction_counterexample_now_fails_closed(capsys):
    import importlib.util
    path = Path(__file__).parents[2] / "docs/review-artifacts/clearwait1/probe_ordinary_submit_durability.py"
    spec = importlib.util.spec_from_file_location("clearwait1_real_rollback_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.main()
    output = json.loads(capsys.readouterr().out)
    assert output["mock_submit_calls"] == 1
    assert output["durable_open_intents_after_rollback"] == 0
    assert output["actual_local_oms_no_target_cancel_receipts"] == 2
    assert output["candidate_clear"] is False
    assert output["classification"] == "FAIL_CLOSED"


def test_unknown_dispatch_reason_is_visible_once_and_keeps_same_owned_wait(caplog):
    strategy, state, clock, _ = _strategy()
    strategy.release_and_drop_symbol("AIXI")
    request = strategy._removed_wait_requests["AIXI"]
    for _ in range(2):
        strategy.apply_removed_wait_proofs([
            RemovedWaitProof(request, clock[0], False, "dispatch_attempt_unproven")
        ])
    assert caplog.text.count("verdict=UNKNOWN reason=dispatch_attempt_unproven") == 1
    assert strategy._removed_wait_requests["AIXI"] == request
    assert state.flip_owner_opportunity_id == request.opportunity_id
    assert not strategy.line_buy_ready("AIXI")


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
def test_recorded_cancel_then_late_fill_on_either_account_cannot_clear(account):
    request, rows = _recorded("AIFA", include_tickets=False)
    order = next(o for o in rows["orders"] if o.broker_account_id == account)
    rows["filled_order_ids"].add(order.id)
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear and proof.reason == "own_fill_stays_owned"


@pytest.mark.parametrize("event_type", ["filled", "partially_filled", "cancelled"])
def test_durable_fill_event_identity_is_not_erased_by_missing_fill_table_row(event_type):
    request, rows = _recorded("AIFA", include_tickets=False)
    event = copy.deepcopy(rows["order_events"][0])
    event.event_type = event_type
    event.payload["broker_fill_id"] = "controlled-positive-fill-identity"
    rows["order_events"].append(event)
    proof = assess_removed_wait(request, **rows)
    assert not proof.clear and proof.reason == "fill_history_not_unfilled"


@pytest.mark.parametrize("outcome", ["already_absent", "confirmed_after_accepted_request", "could_not_tell", "not_confirmed"])
def test_broker_absence_or_cancel_ack_is_not_exact_terminal_proof(outcome):
    request, rows = _recorded("AIXI", include_tickets=False)
    event = next(e for e in rows["order_events"] if e.event_type == "cancelled")
    event.payload["metadata"]["cancel_outcome"] = outcome
    assert assess_removed_wait(request, **rows).reason == "broker_terminal_unproven"


def test_removed_wait_gate_normalizes_symbol_and_cannot_be_bypassed_by_case():
    strategy, state, _, _ = _strategy()
    state.symbol = "aixi"
    strategy._remove_waiting_buy(state, reason="controlled removal")
    assert "AIXI" in strategy._removed_wait_requests
    assert strategy._removed_wait_gate_closed("aixi")
    assert strategy._removed_wait_gate_closed("AIXI")
