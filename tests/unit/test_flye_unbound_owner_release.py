"""Approved option-2 consumer controls; no invented historical books or target IDs."""

from dataclasses import replace

import pytest

from project_mai_tai.v2_flip_entry_ownership import FlipPositionLeg
from project_mai_tai.v2_removed_wait import RemovedWaitProof, configured_removed_wait_bindings
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_flye_bound_owner_target_close import FLYE, WEBULL, book, replay, sell


ControlledUnboundProof = RemovedWaitProof
db = rollover_db


@pytest.fixture
def unbound_db(db):
    from sqlalchemy import select
    from project_mai_tai.db.models import BrokerAccount

    with db[1]() as session:
        for account in session.scalars(select(BrokerAccount)):
            account.external_account_id = None
        session.commit()
    return db


def controlled_routing():
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
    from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
    from tests.unit.test_flye_bound_owner_target_close import PRIMARY

    primary = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    primary.accounts_by_name = {PRIMARY: SchwabAccountConfig(account_hash="TEST-SCHWAB")}
    webull = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    webull.accounts_by_name = {WEBULL: WebullAccountConfig(account_id="TEST-WEBULL")}
    return RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={PRIMARY: "schwab", WEBULL: "webull"},
        factories_by_provider={"schwab": lambda: primary, "webull": lambda: webull})


def setup(*, pm=False):
    strategy, state, record, clock, writes = replay(pm=pm)
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    proof = ControlledUnboundProof(request, clock[0], True, "unbound_symbol_terminal",
                                   tuple(record.position_ids.items()))
    return strategy, state, record, clock, writes, proof


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "unknown", "open_owned", "open_sibling", "row", "token", "stale", "generic", "not_clear",
                                  "stale_book", "unknown_phase", "missing_owned_row", "opportunity", "accounts", "duplicate_rows",
                                  "future", "raised_again"])
def test_operator_1000_ignored_only_for_scoped_closed_owner_release(pm, fault):
    strategy, state, record, clock, writes, proof = setup(pm=pm)
    state.position_qty = state.position_qty_held = 1000
    if fault == "row":
        proof = replace(proof, closed_owned_rows=((WEBULL, "foreign-row"),))
    elif fault == "token":
        proof = replace(proof, request=replace(proof.request, token="foreign"))
    elif fault == "stale":
        proof = replace(proof, observed_at_ms=clock[0] - 15_001)
    elif fault == "future":
        proof = replace(proof, observed_at_ms=clock[0] + 1)
    elif fault == "raised_again":
        strategy._removed_wait_requests[state.symbol] = replace(
            proof.request, token="new-request-token", requested_at_ms=clock[0])
    elif fault == "generic":
        proof = replace(proof, reason="retry_leftovers_cancelled_owner_kept")
    elif fault == "not_clear":
        proof = replace(proof, clear=False)
    elif fault == "unknown_phase":
        state.flip_owner_phase = "unknown"
    elif fault == "missing_owned_row":
        state.flip_owner_fill_accounts.add(WEBULL)
    elif fault == "opportunity":
        proof = replace(proof, request=replace(proof.request, opportunity_id=proof.request.opportunity_id + 1))
    elif fault == "accounts":
        proof = replace(proof, request=replace(proof.request, account_names=(WEBULL, WEBULL)))
    elif fault == "duplicate_rows":
        proof = replace(proof, closed_owned_rows=proof.closed_owned_rows * 2)
    before = len(writes)
    strategy.apply_removed_wait_proofs((proof,))
    if fault != "generic":
        assert len(writes) == before, "F committed the CAS: I must not overwrite its durable witness"
    legs = ()
    if fault == "open_owned":
        account, row = next(iter(record.position_ids.items()))
        legs = (FlipPositionLeg(account, row, record.position_entry_ms[account], 1),)
    elif fault == "open_sibling":
        legs = (FlipPositionLeg(WEBULL, "unknown-sibling", clock[0], 1),)
    if fault == "stale_book":
        clock[0] += 15_001
    book(strategy, record, clock, legs=legs, readable=fault != "unknown")
    assert (state.flip_owner_phase == "idle") is (fault == "none")
    # Owner retirement does not change operator shares or ordinary position/exit gates.
    assert state.position_qty == state.position_qty_held == 1000
    strategy._cw_v2_resting_track(state, {"state": "short", "trail": 2.52})
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_direct_intents()


def test_same_segment_unbound_receipt_never_releases_bound_real_owner():
    strategy, state, record, clock, _ = replay()
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    proof = ControlledUnboundProof(request, clock[0], True, "unbound_symbol_terminal",
                                   tuple(record.position_ids.items()))
    state.position_qty = state.position_qty_held = 1000
    strategy.apply_removed_wait_proofs((proof,))
    book(strategy, record, clock)
    assert state.flip_owner_phase == "bound"
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("fresh", [False, True])
def test_durable_terminal_witness_restart_does_not_rejuvenate_old_books(fresh):
    strategy, state, record, clock, _, proof = setup()
    persisted = strategy._flip_owner_record(state)
    from datetime import UTC, datetime
    from tests.unit.test_retryleft1 import strategy_for_record

    restarted, restored, _, restart_clock, _ = strategy_for_record(
        persisted, datetime.fromtimestamp(clock[0] / 1000, UTC), quantities=(280, 140))
    restored.retry_one_segment_id = FLYE["fresh_sell_bar_ms"]
    restored.position_qty = restored.position_qty_held = 1000
    if not fresh:
        proof = replace(proof, observed_at_ms=clock[0] - 15_001)
    restarted.configure_removed_wait(lambda *_: None, restored={}, readable=True, terminal_proofs=(proof,))
    book(restarted, persisted, restart_clock)
    assert (restored.flip_owner_phase == "idle") is fresh


@pytest.mark.parametrize("pm", [False, True])
def test_fresh_sell_releases_closed_false_owner_without_carrying_old_budget(pm):
    from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
    from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook, FlipPositionClose
    from tests.unit.test_falseflip1_runtime import ack, book as false_book, runtime

    strategy, state, clock, proofs, _ = runtime(pm=pm)
    false_book(strategy, state, clock, proofs)
    ack(strategy)
    prior = strategy._falseflip_budget(state)
    false_book(strategy, state, clock, proofs)
    request, = strategy.pending_falseflip_cancel_publications()
    strategy._removed_wait_persist(request, True)
    strategy.acknowledge_falseflip_cancel_publication(request)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    clock[0] += 60_000
    state.bars.append(OHLCVBar(clock[0], 2.33, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = clock[0]
    state.position_qty = state.position_qty_held = 1000
    false_book(strategy, state, clock, proofs)
    strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": "live"})
    assert state.flip_owner_phase == "awaiting_close"
    assert strategy._removed_wait_evidence_wakes == {request}
    witness = ControlledUnboundProof(request, clock[0], True, "unbound_symbol_terminal",
                                     tuple(state.flip_owner_position_ids.items()))
    strategy.apply_removed_wait_proofs((witness,))
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {},
        closes_by_symbol={state.symbol: tuple(FlipPositionClose(p.identity.account,
            p.identity.managed_row_id, "CW_HARD_STOP") for p in proofs)},
        entry_classifications={state.symbol: tuple(p.as_payload() for p in proofs)},
        closed_entry_rows=frozenset((p.identity.account, p.identity.managed_row_id) for p in proofs)))
    assert state.flip_owner_phase == "idle"
    assert strategy._falseflip_budget(state).episodes == ()
    assert strategy._falseflip_budget(state).refunded_counts == ()
    assert strategy._falseflip_budgets[(prior.symbol, prior.segment)] == prior
    assert state.position_qty == state.position_qty_held == 1000


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "token", "stale", "not_clear", "row"])
def test_same_segment_false_restore_requires_exact_terminal_witness(pm, fault):
    from tests.unit.test_falseflip1_runtime import ack, book as false_book, runtime

    strategy, state, clock, proofs, _ = runtime(pm=pm)
    false_book(strategy, state, clock, proofs)
    ack(strategy)
    false_book(strategy, state, clock, proofs)
    request, = strategy.pending_falseflip_cancel_publications()
    strategy._removed_wait_persist(request, True)
    strategy.acknowledge_falseflip_cancel_publication(request)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    before = strategy._falseflip_budget(state)
    witness = ControlledUnboundProof(request, clock[0], True, "unbound_symbol_terminal",
                                     tuple(state.flip_owner_position_ids.items()))
    if fault == "token":
        witness = replace(witness, request=replace(request, token="foreign-token"))
    elif fault == "stale":
        witness = replace(witness, observed_at_ms=clock[0] - 15_001)
    elif fault == "not_clear":
        witness = replace(witness, clear=False)
    elif fault == "row":
        witness = replace(witness, closed_owned_rows=((WEBULL, "foreign-row"),))
    strategy.apply_removed_wait_proofs((witness,))
    false_book(strategy, state, clock, proofs)
    assert bool(strategy.pending_falseflip_budgets()) is (fault == "none")
    ack(strategy)
    false_book(strategy, state, clock, proofs)
    assert (state.flip_owner_phase == "idle") is (fault == "none")
    assert strategy._falseflip_budget(state).segment == before.segment
    assert state.retry_one_closes_in_segment == 1
    assert strategy._falseflip_effective_closes(state, 1) == 0
    assert (state.symbol in strategy._removed_wait_requests) is (fault != "none")
    assert strategy._falseflip_budget(state).cancelled == (
        (*before.cancelled, request.opportunity_id) if fault == "none" else before.cancelled)
    if fault != "none":
        assert strategy._falseflip_budget(state) == before
        assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "not_live", "same_segment", "unreadable", "persist", "identity", "token", "opportunity"])
def test_fresh_sell_wakes_only_exact_pending_request_after_committed_segment(pm, fault):
    strategy, state, _, clock, _, proof = setup(pm=pm)
    request = proof.request
    wakes = strategy.__dict__.setdefault("_removed_wait_evidence_wakes", set())
    assert wakes == {request}
    wakes.clear()  # The actual off-loop caller consumes this wake before acquisition.
    strategy._retry_one_start_segment_on_sell(state, live=True)
    assert not wakes, "duplicate same-bar delivery must not cause another broker read"
    clock[0] += 60_000
    from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar

    state.bars.append(OHLCVBar(clock[0], 2.3, 2.35, 2.27, 2.28, 1))
    state.atr_short_flip_bar_ts = clock[0]
    if fault == "same_segment":
        state.retry_one_segment_id = clock[0]
    elif fault == "unreadable":
        state.retry_one_budget_readable = False
    elif fault == "persist":
        def fail(*_):
            raise RuntimeError("controlled segment persistence failure")
        strategy._retry_one_budget_persist = fail
    elif fault == "identity":
        state.atr_short_flip_bar_ts = 0
    elif fault == "token":
        strategy._removed_wait_requests[state.symbol] = replace(request, token="new-current-token")
    elif fault == "opportunity":
        strategy._removed_wait_requests[state.symbol] = replace(request, opportunity_id=request.opportunity_id + 1)
    strategy._retry_one_start_segment_on_sell(state, live=fault != "not_live")
    expected = strategy._removed_wait_requests[state.symbol]
    assert wakes == ({expected} if fault in {"none", "token"} else set())
    assert request not in wakes or fault == "none"


@pytest.mark.parametrize("pm", [False, True])
def test_1304_terminal_book_needs_fresh_sell_resample_not_timestamp_refresh(pm):
    strategy, state, record, clock, _ = replay(pm=pm)
    book(strategy, record, clock)
    request = strategy._removed_wait_requests["FLYE"]
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    old = ControlledUnboundProof(request, clock[0], True, "unbound_symbol_terminal",
                                 tuple(record.position_ids.items()))
    strategy.apply_removed_wait_proofs((old,))
    clock[0] = FLYE["fresh_sell_poll_ms"]
    book(strategy, record, clock)
    sell(strategy, state, clock)
    assert strategy._removed_wait_evidence_wakes == {request}
    assert strategy._closed_owner_terminal_receipts[("FLYE", record.opportunity_id)] is old
    assert state.flip_owner_phase == "awaiting_close"
    assert "FLYE" in strategy._removed_wait_requests
    strategy.apply_removed_wait_proofs((replace(old, observed_at_ms=clock[0]),))
    book(strategy, record, clock)
    assert state.flip_owner_phase == "idle"


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("fault", ["none", "stale", "open", "unknown", "token", "persist"])
def test_boot_stores_configure_before_lazy_owner_creation(active, fault):
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

    strategy, state, _, clock, _, proof = setup()
    persisted = strategy._flip_owner_record(state)
    restarted = SchwabV2Strategy(strategy.settings)
    restarted._now_ms = lambda: clock[0]
    restarted.configure_fanout_identity_persistence(lambda *_: None)
    restarted.configure_flip_entry_ownership(
        lambda *_: None, restored={"FLYE": persisted},
        active_segments={"FLYE": persisted.opportunity_id},
        retry_budget_persist=lambda *_: None,
        restored_retry_budgets={"FLYE": (FLYE["fresh_sell_bar_ms"], 0)},
    )
    writes = []

    def complete(request, enabled):
        if fault == "persist":
            raise RuntimeError("controlled unavailable exact-token CAS")
        writes.append((request, enabled))

    if fault == "stale":
        proof = replace(proof, observed_at_ms=clock[0] - 15_001)
    current = replace(proof.request, token="newer-token") if fault == "token" else proof.request
    restored_requests = {"FLYE": current} if active or fault == "token" else {}
    assert not restarted._symbol_states
    restarted.configure_removed_wait(complete, restored=restored_requests,
                                     readable=True, terminal_proofs=iter((proof,)))
    if not restored_requests:
        assert not restarted._symbol_states, "completed proof restores before watch state creation"
    restored = restarted.watchlist_state("FLYE")
    restored.position_qty = restored.position_qty_held = 1000
    legs = (FlipPositionLeg(WEBULL, "unknown-open-sibling", clock[0], 1),) if fault == "open" else ()
    book(restarted, persisted, clock, legs=legs, readable=fault != "unknown")
    released = fault == "none" or (fault == "persist" and not active)
    assert (restored.flip_owner_phase == "idle") is released
    assert restored.position_qty == restored.position_qty_held == 1000
    cached = restarted._closed_owner_terminal_receipts.get(("FLYE", persisted.opportunity_id))
    if fault != "token":
        assert cached is proof
        assert cached.observed_at_ms == proof.observed_at_ms
    assert writes == ([(current, False)] if released and active else [])
    assert ("FLYE" in restarted._removed_wait_requests) is (bool(restored_requests) and not released)
    if fault != "token":
        assert not restarted.drain_pending_intents() and not restarted.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("fault", ["none", "stale", "open", "unknown"])
async def test_legacy_books_do_not_create_closed_owner_boot_witness(unbound_db, active, fault):
    from datetime import UTC, datetime
    from uuid import UUID

    from project_mai_tai.cancel_terminal_proof import CompleteWorkingBook
    from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
    from tests.unit.test_clearwait1_session_rollover import seed, service

    strategy, state, _, clock, _, controlled = setup()
    persisted = strategy._flip_owner_record(state)
    request = controlled.request
    store, sessions, ids, strategy_id = unbound_db
    seed(unbound_db, request, receipts=False)
    with sessions() as session:
        for account, row in persisted.position_ids.items():
            entry = BrokerOrder(strategy_id=strategy_id, broker_account_id=ids[account],
                symbol="FLYE", side="buy", order_type="limit", time_in_force="day",
                quantity=280, status="filled", client_order_id="controlled-owned-" + account,
                payload={"fanout_segment_id": str(request.opportunity_id)})
            session.add(entry)
            session.flush()
            session.add(OmsManagedPosition(id=UUID(row), strategy_code="schwab_1m_v2",
                broker_account_name=account, symbol="FLYE", entry_order_id=entry.id,
                entry_price=2.22, original_quantity=280, current_quantity=0, status="closed"))
        session.commit()
    # Explicit controlled both-account books/drain, not retained historical broker evidence.
    bindings = configured_removed_wait_bindings(controlled_routing(), request.account_names)
    books = {account: CompleteWorkingBook(account, bindings[account][1], clock[0], clock[0], True,
                                         "all_working", (), "broker") for account in request.account_names}
    proof, = store.retire_unbound((request,), set(request.account_names), books=books,
        expected_bindings=bindings, publication_closed={request: True},
        now=datetime.fromtimestamp(clock[0] / 1000, UTC))
    assert not proof.clear and proof.closed_owned_rows == ()
    assert store.restore_terminal_proofs() == ()
    if not active:
        store.record(request, False)
    if fault == "stale":
        clock[0] += 15_001
    restarted = SchwabV2Strategy(strategy.settings)
    restarted._now_ms = lambda: clock[0]
    restarted.configure_fanout_identity_persistence(lambda *_: None)
    restarted.configure_flip_entry_ownership(lambda *_: None, restored={"FLYE": persisted},
        active_segments={"FLYE": persisted.opportunity_id}, retry_budget_persist=lambda *_: None,
        restored_retry_budgets={"FLYE": (FLYE["fresh_sell_bar_ms"], 0)})
    assert not restarted._symbol_states
    bot = service(restarted, store)
    bot._removed_wait_adapter = controlled_routing()
    await bot._configure_removed_wait_store()
    if not active:
        assert not restarted._symbol_states
    restored = restarted.watchlist_state("FLYE")
    restored.position_qty = restored.position_qty_held = 1000
    assert not restarted._closed_owner_terminal_receipts
    for drafts in (restarted.drain_pending_intents(), restarted.drain_webull_direct_intents()):
        assert len(drafts) == int(active)
        assert all(d.intent_type == "cancel" for d in drafts)
    legs = (FlipPositionLeg(WEBULL, "unknown-open-row", clock[0], 1),) if fault == "open" else ()
    book(restarted, persisted, clock, legs=legs, readable=fault != "unknown")
    assert restored.flip_owner_phase != "idle"
    assert store.restore_terminal_proofs() == (), "boot must not promote legacy book absence"
    assert restored.position_qty == restored.position_qty_held == 1000
    assert bool(store.restore()) is active


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", ["none", "schwab_buy", "webull_buy", "open_owned", "unknown_book",
                                  "same_segment", "operator_sell", "missing_config", "retained_id", "provider"])
def test_legacy_unbound_store_books_never_clear_wired_owner(unbound_db, pm, fault):
    from datetime import UTC, datetime
    from uuid import UUID

    from project_mai_tai.cancel_terminal_proof import BookOrder, CompleteWorkingBook
    from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, OmsManagedPosition
    from tests.unit.test_clearwait1_session_rollover import seed
    from tests.unit.test_flye_bound_owner_target_close import PRIMARY

    strategy, state, record, clock, _, controlled = setup(pm=pm)
    request = controlled.request
    store, sessions, ids, strategy_id = unbound_db
    seed(unbound_db, request, receipts=False)
    with sessions() as session:
        if fault == "retained_id":
            session.get(BrokerAccount, ids[PRIMARY]).external_account_id = "retained-wrong-id"
        elif fault == "provider":
            session.get(BrokerAccount, ids[PRIMARY]).provider = "webull"
        for account, row in record.position_ids.items():
            entry = BrokerOrder(strategy_id=strategy_id, broker_account_id=ids[account], symbol="FLYE",
                side="buy", order_type="limit", time_in_force="day", quantity=280, status="filled",
                client_order_id="controlled-owned-entry", payload={"fanout_segment_id": str(request.opportunity_id)})
            session.add(entry)
            session.flush()
            session.add(OmsManagedPosition(id=UUID(row), strategy_code="schwab_1m_v2",
                broker_account_name=account, symbol="FLYE", entry_order_id=entry.id, entry_price=2.22,
                original_quantity=280, current_quantity=1 if fault == "open_owned" else 0,
                status="open" if fault == "open_owned" else "closed"))
        if fault in {"schwab_buy", "webull_buy", "operator_sell"}:
            account = WEBULL if fault == "webull_buy" else PRIMARY
            side = "sell" if fault == "operator_sell" else "buy"
            session.add(BrokerOrder(strategy_id=strategy_id, broker_account_id=ids[account], symbol="FLYE",
                side=side, order_type="limit", time_in_force="day", quantity=1000, status="accepted",
                client_order_id="controlled-working-order"))
            if fault == "operator_sell":
                session.add(AccountPosition(broker_account_id=ids[account], symbol="FLYE", quantity=1000))
        session.commit()
    # Counterfactual both-account broker shapes/drain; not acquired or historical books.
    bindings = configured_removed_wait_bindings(controlled_routing(), request.account_names)
    books = {account: CompleteWorkingBook(account, bindings[account][1], clock[0], clock[0], True,
                                         "all_working", (), "broker") for account in request.account_names}
    if fault in {"schwab_buy", "webull_buy", "operator_sell"}:
        books[account] = replace(books[account], orders=(BookOrder("controlled-working-order", "FLYE",
            "working", side),))
    if fault == "unknown_book":
        books[PRIMARY] = None
    if fault == "same_segment":
        state.flip_owner_phase = "bound"
        state.retry_one_segment_id = record.retry_segment_id
    if fault == "operator_sell":
        state.position_qty = state.position_qty_held = 1000
    strategy._removed_wait_persist = store.record
    proof, = store.retire_unbound((request,), set(request.account_names), books=books,
        expected_bindings=None if fault == "missing_config" else bindings,
        publication_closed={request: True}, now=datetime.fromtimestamp(clock[0] / 1000, UTC))
    assert not proof.clear and proof.closed_owned_rows == ()
    strategy.apply_removed_wait_proofs((proof,))
    legs = tuple(FlipPositionLeg(account, row, record.position_entry_ms[account], 1)
                 for account, row in record.position_ids.items()) if fault == "open_owned" else ()
    book(strategy, record, clock, legs=legs)
    assert state.flip_owner_phase != "idle"
    if fault == "operator_sell":
        assert state.position_qty == state.position_qty_held == 1000
        strategy._cw_v2_resting_track(state, {"state": "short", "trail": 2.558685})
        assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    assert store.restore() == {"FLYE": request}
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    with sessions() as session:
        assert session.get(BrokerAccount, ids[PRIMARY]).external_account_id == (
            "retained-wrong-id" if fault == "retained_id" else None)
        assert session.get(BrokerAccount, ids[WEBULL]).external_account_id is None
