"""FLYE chronology through the real PG journal; broker responses are controlled, not live."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from threading import get_ident

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import cancel_terminal as journal
from project_mai_tai.v2_flip_entry_ownership import FlipPositionLeg
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.integration.test_cancel_terminal_runtime import (
    Client, adapter, sdk, sessions,  # noqa: F401
)
from tests.unit.test_flye_bound_owner_target_close import (
    FLYE, PRIMARY, WEBULL, book, confirm_controlled_next_entry, replay, sell,
)


@pytest.fixture
def pg(request):
    url = make_url(os.environ.get("MAI_TAI_DATABASE_URL", ""))
    assert url.host in {"localhost", "127.0.0.1"} and url.database.endswith("_test")
    return request.getfixturevalue("sessions")


def routed_accounts(client):
    webull = adapter(client)._adapter_for_account(WEBULL)
    primary = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    primary.accounts_by_name = {PRIMARY: SchwabAccountConfig(account_hash="TEST-SCHWAB")}
    calls = []

    async def get(method, path):
        calls.append((method, path, get_ident()))
        return 200, {}, {"orderId": "test-schwab-target", "status": "CANCELED",
                         "filledQuantity": 0,
                         "orderLegCollection": [{"instrument": {"symbol": "FLYE"}}]}

    primary._authorized_request_json = get
    routed = RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={PRIMARY: "schwab", WEBULL: "webull"},
        factories_by_provider={"schwab": lambda: primary, "webull": lambda: webull})
    return routed, calls


def persist_controls(factory, routed, request, drafts, *, unbound=False):
    """Explicit future-request target bindings, never assigned to the legacy FLYE receipt."""
    ids = []
    at = datetime.fromtimestamp((request.requested_at_ms + 200) / 1000, UTC)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="I controlled PG replay")
        accounts = {PRIMARY: BrokerAccount(name=PRIMARY, provider="schwab", environment="test",
                                          external_account_id="TEST-SCHWAB"),
                    WEBULL: BrokerAccount(name=WEBULL, provider="webull", environment="test",
                                         external_account_id="ACC1")}
        session.add_all([strategy, *accounts.values()])
        session.flush()
        for name, draft in zip((PRIMARY, WEBULL), drafts, strict=True):
            md = dict(draft.metadata)
            if not unbound:
                md["target_client_order_id"] = "test-primary" if name == PRIMARY else "test-webull"
                if name == PRIMARY:
                    md["broker_order_id"] = "test-schwab-target"
                    session.add(BrokerOrder(strategy_id=strategy.id, broker_account_id=accounts[name].id,
                        client_order_id="test-primary", broker_order_id="test-schwab-target",
                        symbol="FLYE", side="buy", order_type="limit", time_in_force="day",
                        quantity=1, status="cancelled", submitted_at=at, updated_at=at))
            event = TradeIntentEvent(source_service="I controlled replay", payload=TradeIntentPayload(
                strategy_code="schwab_1m_v2", broker_account_name=name, symbol="FLYE",
                side="buy", intent_type="cancel", quantity=Decimal(1),
                reason=draft.reason, metadata=md))
            intent = TradeIntent(strategy_id=strategy.id, broker_account_id=accounts[name].id,
                symbol="FLYE", side="buy", intent_type="cancel", quantity=1, reason=draft.reason,
                status="rejected", created_at=at, updated_at=at,
                payload={"event_id": str(event.event_id), "metadata": md,
                         "refusal_origin": "skipped_before_submit",
                         "refusal_code": "cancel_target_not_found"})
            journal.bind_cancel_target(intent, event, routed)
            session.add(intent)
            session.flush()
            ids.append(intent.id)
        session.commit()
    return ids


async def journal_proof(factory, routed, ids, store, request, now_ms):
    await journal.acquire_cancel_terminal_evidence(factory, routed, ids)
    return (await asyncio.to_thread(store.proofs, (request,), set(request.account_names),
                                   now=datetime.fromtimestamp(now_ms / 1000, UTC)))[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "working", "partial", "unbound", "stale", "open", "unknown", "held"])
async def test_pg_fresh_sell_requires_bound_broker_proof_and_all_rows_closed(pg, monkeypatch, pm, fault):
    strategy, state, record, clock, _ = replay(pm=pm)
    store = RemovedWaitStore(pg)
    strategy._removed_wait_persist = store.record
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    drafts = (strategy.drain_pending_intents()[0], strategy.drain_webull_direct_intents()[0])
    pages = [{"has_next": False, "orders": []}]
    if fault == "working":
        pages[0]["orders"] = [{"client_order_id": "test-webull", "symbol": "FLYE",
                                "order_status": "SUBMITTED"}]
    elif fault == "partial":
        pages = [{"has_next": True, "orders": []}]
    client = Client(pages=pages)
    routed, calls = routed_accounts(client)
    ids = persist_controls(pg, routed, request, drafts, unbound=fault == "unbound")
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    assert state.flip_owner_phase == "awaiting_close"
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0] - (15_001 if fault == "stale" else 0))
    proof = await journal_proof(pg, routed, ids, store, request, clock[0])
    assert proof.clear is (fault in {"none", "open", "unknown", "held"})
    strategy.apply_removed_wait_proofs((proof,))
    legs = (FlipPositionLeg(WEBULL, "test-open-sibling", clock[0], 1),) if fault == "open" else ()
    if fault == "held":
        state.position_qty_held = 1
    book(strategy, record, clock, legs=legs, readable=fault != "unknown")
    released = fault == "none"
    assert (state.flip_owner_phase == "idle") is released
    assert strategy._strict_first_rest_admitted(state, slot="first") is released
    assert state.retry_one_closes_in_segment == 0
    if not released:
        assert not strategy.drain_pending_intents()
        assert not strategy.drain_webull_direct_intents()
        return
    assert calls == [("GET", "/trader/v1/accounts/TEST-SCHWAB/orders/test-schwab-target", get_ident())]
    assert all(thread != get_ident() for _, _, thread in client.calls)
    strategy._queue_resting_place(state, 2.52, slot="first")
    if pm:
        assert state.resting_active and not state.resting_is_broker_order
    else:
        entry, = strategy.drain_pending_intents()
        assert entry.intent_type == "open"
        assert int(entry.metadata["fanout_segment_id"]) > record.opportunity_id
        assert strategy.drain_webull_direct_intents()[0].intent_type == "open"
    assert not store.restore()
    confirm_controlled_next_entry(strategy, state, clock)


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [False, True])
async def test_pg_same_real_segment_terminal_proof_does_not_grant_second_buy(pg, monkeypatch, pm):
    strategy, state, record, clock, _ = replay(pm=pm)
    store = RemovedWaitStore(pg)
    strategy._removed_wait_persist = store.record
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    drafts = (strategy.drain_pending_intents()[0], strategy.drain_webull_direct_intents()[0])
    routed, _ = routed_accounts(Client())
    ids = persist_controls(pg, routed, request, drafts)
    clock[0] += 1_000
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    proof = await journal_proof(pg, routed, ids, store, request, clock[0])
    assert proof.clear
    strategy.apply_removed_wait_proofs((proof,))
    book(strategy, record, clock)
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert not store.restore()
    with pg() as session:
        assert all(i.intent_type == "cancel" for i in session.scalars(select(TradeIntent)))


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
async def test_pg_open_managed_row_in_either_account_blocks_canonical_release(pg, monkeypatch, pm, account):
    strategy, state, record, clock, _ = replay(pm=pm)
    store = RemovedWaitStore(pg)
    strategy._removed_wait_persist = store.record
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    drafts = (strategy.drain_pending_intents()[0], strategy.drain_webull_direct_intents()[0])
    routed, _ = routed_accounts(Client())
    ids = persist_controls(pg, routed, request, drafts)
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    with pg() as session:
        row = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="FLYE", entry_price=2.24, original_quantity=1, current_quantity=1,
            status="open", entry_time=datetime.fromtimestamp(clock[0] / 1000, UTC))
        session.add(row)
        session.commit()
        row_id = str(row.id)
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    proof = await journal_proof(pg, routed, ids, store, request, clock[0])
    assert not proof.clear and proof.reason == "position_stays_managed"
    strategy.apply_removed_wait_proofs((proof,))
    book(strategy, record, clock, legs=(FlipPositionLeg(account, row_id, clock[0], 1),))
    assert state.flip_owner_phase != "idle"
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert store.restore() == {"FLYE": request}


def controlled_unbound_db(pg):
    with pg() as session:
        strategy = Strategy(code="schwab_1m_v2", name="I controlled boot ordering")
        accounts = {name: BrokerAccount(name=name, provider="webull" if name == WEBULL else "schwab",
            environment="test", external_account_id=name) for name in (PRIMARY, WEBULL)}
        session.add_all([strategy, *accounts.values()])
        session.flush()
        strategy_id = strategy.id
        ids = {name: account.id for name, account in accounts.items()}
        session.commit()
    return RemovedWaitStore(pg), pg, ids, strategy_id


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("fault", ["none", "stale", "open", "unknown"])
async def test_pg_actual_boot_restores_scoped_witness_before_owner_state(pg, active, fault):
    from tests.unit.test_flye_unbound_owner_release import (
        test_actual_service_boot_delivers_closed_owner_witness_before_watch as boot_control,
    )

    # Real PG store and service loader; the reused books/drain remain explicit controls.
    await boot_control(controlled_unbound_db(pg), active, fault)


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "schwab_buy", "webull_buy", "open_owned", "unknown_book",
                                  "same_segment", "operator_sell"])
def test_pg_controlled_unbound_flye_sell_rest_buy_chronology(pg, pm, fault):
    from tests.unit.test_flye_unbound_owner_release import (
        test_controlled_unbound_store_flye_sell_rest_buy_chronology as chronology_control,
    )

    chronology_control(controlled_unbound_db(pg), pm, fault)
