"""Recorded OLOX identities/phases; later cache, quotes and wire are controlled."""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

import pytest

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE, replacement_terminal_zero
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_rpg1_runtime import runtime
from tests.unit.test_t43_one_leg_recovery import RECORDED
from tests.unit.test_all_on_pm import ALL_ON, completed_seeded_line  # noqa: F401
from tests.unit.t43_recorded_audit_support import seed_recorded_abort

CHAIN = json.loads((Path(__file__).parents[1] / "fixtures/t43_olox_cancel_chain_20261006.json").read_text())
PRIOR_TOKEN = UUID("48c765e5-a03a-5fe7-b488-78355518da16")
C9_TOKEN = UUID("c9b13cc4-d0c7-5e87-84c1-5308f001158d")
GENERATION = "48c765e5-a03a-5fe7-b488-78355518da16:0"


async def chain_runtime(monkeypatch):
    h = await runtime(monkeypatch, "schwab", notional=600)
    h.strategy.drain_pending_intents()
    h.strategy.drain_webull_direct_intents()
    prior = deepcopy(CHAIN["prior"]["payload"])
    assert "phase=placed reason=replacement_accepted" in CHAIN["placed_log"]
    prior.update(phase="placed", reason="replacement_accepted")
    c9 = deepcopy(next(j for j in RECORDED["tickets"] if
                       j["replacement"]["metadata"]["rpg_handoff_token"] == str(C9_TOKEN)))
    h.clock[0] = datetime(2026, 10, 6, 14, 16, 3, tzinfo=UTC)
    h.state = st = h.strategy.watchlist_state("OLOX")
    st.fanout_segment_id = c9["segment_id"]
    st.atr_short_flip_bar_ts = int(c9["old"]["metadata"]["rpg_short_segment"])
    st.atr_state, st.atr_state_age, st.atr_trail = "short", 45, 1.456585
    # High/low/close/volume/trail are the saved 14:16:02.833 ATR probe.
    # Open, bid and renewed quote timestamp are controlled continuation inputs.
    st.bars.append(OHLCVBar(1791296100000, 1.375, 1.38, 1.3525, 1.375, 127705))
    st.last_quote = Quote("OLOX", 1.36, 1.37, 1.375, h.strategy._now_ms())
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), prior)
    assert st.resting_schwab_generation == GENERATION and st.resting_schwab_quantity == 405
    h.strategy.rpg_handoff_authorization(str(C9_TOKEN), c9)
    # Recorded sibling identity/price, with its accepted phase from the saved OMS
    # 14:15:09.296 report. Retaining that phase in this cache is controlled.
    mirror = deepcopy(CHAIN["surviving_mirror"])
    mirror.update(phase="placed", reason="replacement_accepted")
    h.strategy.rpg_handoff_authorization(mirror["replacement"]["metadata"]["rpg_handoff_token"], mirror)
    raw = CHAIN["cancelled_replacement"]
    event = TradeIntentEvent(event_id=UUID(prior["replacement"]["metadata"]["rpg_event_id"]),
        source_service="schwab-1m-v2", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            symbol="OLOX", side="buy", intent_type="open", quantity=Decimal(raw["quantity"]),
            reason=prior["replacement"]["reason"], metadata=raw["payload"]))
    with h.factory() as session:
        strategy = h.service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = h.service.store.ensure_broker_account(session, "live:schwab_1m_v2",
                                                       provider="simulated", environment="test")
        intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.status = "cancelled"
        order = h.service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=raw["client"], symbol="OLOX", side="buy",
            quantity=Decimal(raw["quantity"]), metadata=raw["payload"], status=raw["status"],
            broker_order_id=raw["broker_id"], order_type="STOP_LIMIT", time_in_force="day")
        order.id = UUID(raw["id"])
        session.add_all([DashboardSnapshot(id=PRIOR_TOKEN, snapshot_type=SNAPSHOT_TYPE, payload=prior),
                         DashboardSnapshot(id=C9_TOKEN, snapshot_type=SNAPSHOT_TYPE, payload=c9)])
        session.commit()
    seed_recorded_abort(h, c9)
    journal = HandoffJournal(h.factory)
    h.strategy.rpg_handoff_authorization(str(C9_TOKEN), journal.reconcile_feedback(C9_TOKEN, journal.read(C9_TOKEN)))
    return h


@pytest.mark.asyncio
async def test_recorded_1013_placed_1015_cancel_refusal_releases_only_primary_1016(monkeypatch):
    h = await chain_runtime(monkeypatch)
    assert h.state.resting_active and h.state.resting_schwab_quantity == 0
    assert h.state.resting_schwab_generation == ""
    assert h.state.resting_schwab_retired_generation == GENERATION
    journal = HandoffJournal(h.factory)
    await h.bot._rpg_handoff_pass()
    assert journal.read(PRIOR_TOKEN)["phase"] == "refused"
    assert replacement_terminal_zero(journal.read(PRIOR_TOKEN))
    before = tuple(getattr(h.state, "resting_webull_" + key)
                   for key in ("quantity", "generation", "wire_stop", "wire_limit"))
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    assert not h.strategy.drain_webull_direct_intents()
    assert primary.quantity > 0 and primary.symbol == "OLOX"
    assert tuple(getattr(h.state, "resting_webull_" + key)
                 for key in ("quantity", "generation", "wire_stop", "wire_limit")) == before
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            symbol=primary.symbol, side=primary.side, intent_type=primary.intent_type,
            quantity=primary.quantity, reason=primary.reason, metadata=primary.metadata))
    await h.service.process_trade_intent(event)
    assert len(h.adapter.opens) == 1  # Simulated normal OMS submission, never a live broker.
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_successor", "unknown", "fill", "fill_row", "generation", "account",
                                   "client", "quantity", "order_id", "unrecorded", "no_rebuy",
                                   "malformed_quantity", "missing_broker_id", "broker_status", "slot", "segment"])
async def test_cancel_transition_requires_exact_durable_zero_proof(monkeypatch, damage):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        row = session.get(DashboardSnapshot, C9_TOKEN)
        proof = deepcopy(row.payload)
        if damage == "missing_successor":
            session.delete(row)
        elif damage == "unknown":
            proof.pop("cleared_at", None)
        elif damage in {"fill", "fill_row"}:
            order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
            if damage == "fill":
                order.status = "partially_filled"
            else:
                session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, symbol=order.symbol,
                    side="buy", quantity=Decimal(1), price=Decimal("1.47"), payload={}))
        elif damage in {"generation", "account", "client", "quantity"}:
            if damage == "generation":
                proof["old"]["metadata"]["rpg_resting_generation"] = "CONTROLLED-foreign-generation"
            else:
                field = {"account": "broker_account_name", "client": "client_order_id", "quantity": "quantity"}[damage]
                proof["old"][field] = "406" if damage == "quantity" else "CONTROLLED-foreign-identity"
        elif damage == "order_id":
            proof["original_order_id"] = "00000000-0000-0000-0000-000000000001"
        elif damage == "unrecorded":
            proof["clear_recorded"] = False
        elif damage == "malformed_quantity":
            proof["old"]["quantity"] = "not-a-number"
        elif damage == "missing_broker_id":
            order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
            order.broker_order_id = None
            proof["old"]["metadata"]["broker_order_id"] = ""
        elif damage == "broker_status":
            proof["broker_status"] = "UNKNOWN"
        elif damage in {"slot", "segment"}:
            proof["old"]["metadata"]["cw_entry_slot" if damage == "slot" else "fanout_segment_id"] = "foreign"
        else:
            proof["no_rebuy"] = True
        if damage != "missing_successor":
            row.payload = proof
        session.commit()
    journal = HandoffJournal(h.factory)
    if damage == "missing_successor":
        def forbid_unproven_publication(*args, **kwargs):
            assert not kwargs.get("replacement_terminal_report"), "status-only proof reached publication"
        monkeypatch.setattr(journal, "change", forbid_unproven_publication)
    result = journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))
    assert result["phase"] == ("filled" if damage in {"fill", "fill_row"} else "placed")
    assert not replacement_terminal_zero(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["unknown", "filled", "generation"])
async def test_successor_feedback_cannot_clear_unknown_filled_or_newer_latch(monkeypatch, damage):
    h = await chain_runtime(monkeypatch)
    c9 = deepcopy(h.strategy._rpg_handoffs[str(C9_TOKEN)])
    h.strategy._rpg_feedback_applied.discard((str(C9_TOKEN), "refused"))
    h.state.resting_schwab_generation = GENERATION
    if damage == "unknown":
        c9.pop("cleared_at", None)
    elif damage == "filled":
        c9["no_rebuy"] = True
    else:
        h.state.resting_schwab_generation = "CONTROLLED-newer-working-generation"
        h.state.resting_schwab_quantity = 405
    generation = h.state.resting_schwab_generation
    h.strategy.rpg_handoff_authorization(str(C9_TOKEN), c9)
    assert h.state.resting_schwab_generation == generation
    if damage == "filled":
        assert h.state.cw_resting_taken


@pytest.mark.asyncio
async def test_placed_generation_skip_names_account_and_reason(monkeypatch, capsys):
    h = await chain_runtime(monkeypatch)
    h.state.resting_schwab_generation = GENERATION
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()
    output = capsys.readouterr().out
    assert "V2-RESTING-LEG-SKIP" in output
    assert "account=live:schwab_1m_v2" in output
    assert "reason=placed_generation_owned" in output
    assert f"generation={GENERATION}" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["", "CONTROLLED-newer-generation"])
@pytest.mark.parametrize("status", ["cancelled", "expired", "rejected", "aborted", "pending",
                                  "partially_filled", "fill_row"])
async def test_unproven_replacement_cannot_buy_with_inactive_or_different_generation(monkeypatch, generation, status):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        session.delete(session.get(DashboardSnapshot, C9_TOKEN))
        order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
        if status == "fill_row":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, symbol=order.symbol,
                side="buy", quantity=Decimal(1), price=Decimal("1.47"), payload={}))
        else:
            order.status = status
        session.commit()
    journal = HandoffJournal(h.factory)
    job = journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))
    assert job["phase"] == ("filled" if status in {"partially_filled", "fill_row"} else "placed")
    h.strategy._rpg_handoffs = {str(PRIOR_TOKEN): job}
    h.strategy._rpg_feedback_applied.clear()
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    # Fault controls model stale shared projections, not broker clearance.
    h.state.resting_active = False
    h.state.resting_schwab_generation = generation
    h.state.resting_schwab_quantity = 0
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="first")
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents()
    assert HandoffJournal(h.factory).read(PRIOR_TOKEN)["phase"] == job["phase"]


@pytest.mark.asyncio
async def test_placed_buy_ownership_does_not_prevent_serial_reprice_cancel(monkeypatch):
    h = await chain_runtime(monkeypatch)
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2")
    assert not h.strategy._rpg_leg_owned(h.state, "live:schwab_1m_v2")
    h.strategy._queue_resting_cancel(h.state, reason="reprice")
    cancel, = h.strategy.drain_pending_intents()
    assert cancel.intent_type == "cancel" and cancel.metadata["rpg_generation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["", GENERATION, "CONTROLLED-newer-generation"])
@pytest.mark.parametrize("reason", ["replacement_terminal_accounted", "replacement_refused", "price_retry_budget_exhausted", "unknown"])
async def test_status_only_terminal_projection_without_proof_still_owns_buy(monkeypatch, generation, reason):
    h = await chain_runtime(monkeypatch)
    job = deepcopy(h.strategy._rpg_handoffs[str(PRIOR_TOKEN)])
    job.update(phase="refused", reason=reason)
    job.pop("replacement_terminal_report", None)
    h.strategy._rpg_handoffs = {str(PRIOR_TOKEN): job}
    h.strategy._rpg_feedback_applied.clear()
    h.state.resting_schwab_generation = generation
    h.state.resting_schwab_quantity = 405
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert h.state.resting_schwab_quantity == 405
    assert h.state.resting_schwab_generation == generation
    h.state.resting_active = False
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2")
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()


@pytest.mark.asyncio
async def test_filled_first_slot_does_not_consume_the_independent_reclaim_slot(monkeypatch):
    h = await chain_runtime(monkeypatch)
    job = deepcopy(h.strategy._rpg_handoffs[str(PRIOR_TOKEN)])
    job.update(phase="filled", replacement_filled=True)
    h.strategy._rpg_handoffs = {str(PRIOR_TOKEN): job}
    h.strategy._rpg_feedback_applied.clear()
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert h.state.cw_resting_taken and not h.state.cw_reclaim_taken
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="first")
    assert not h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="reclaim")
    h.state.resting_active = False
    h.strategy._queue_resting_place(h.state, h.state.atr_trail, slot="first")
    assert not h.strategy.drain_pending_intents()


@pytest.mark.asyncio
async def test_same_phase_new_revision_zero_proof_retires_latch_once(monkeypatch):
    h = await chain_runtime(monkeypatch)
    journal = HandoffJournal(h.factory)
    job = journal.change(PRIOR_TOKEN, journal.read(PRIOR_TOKEN)["revision"],
                         phase="refused", reason="replacement_terminal_accounted")
    h.state.resting_schwab_generation = GENERATION
    h.state.resting_schwab_quantity = 405
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert h.state.resting_schwab_generation == GENERATION
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2")
    proven = journal.reconcile_feedback(PRIOR_TOKEN, job)
    assert proven["phase"] == job["phase"] and proven["revision"] > job["revision"]
    assert replacement_terminal_zero(proven)
    sibling = h.state.resting_webull_generation, h.state.resting_webull_quantity
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), proven)
    assert h.state.resting_schwab_generation == "" and h.state.resting_schwab_quantity == 0
    assert (h.state.resting_webull_generation, h.state.resting_webull_quantity) == sibling
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert h.strategy._rpg_handoffs[str(PRIOR_TOKEN)] == proven
    h.strategy._cw_v2_resting_track(h.state, None)
    assert len(h.strategy.drain_pending_intents()) == 1
    assert not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["account", "generation", "symbol", "quantity", "client"])
async def test_invalid_filled_identity_cannot_adopt_or_consume(monkeypatch, damage):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
        order.status = "filled"
        if damage == "generation":
            order.payload = {**order.payload, "rpg_resting_generation": "foreign"}
        elif damage == "account":
            other = h.service.store.ensure_broker_account(session, "CONTROLLED:foreign",
                                                          provider="simulated", environment="test")
            order.broker_account_id = other.id
        elif damage == "quantity":
            order.quantity += 1
        else:
            setattr(order, "client_order_id" if damage == "client" else "symbol", "foreign")
        session.commit()
    journal = HandoffJournal(h.factory)
    job = journal.read(PRIOR_TOKEN)
    def forbid_wrong_identity_adoption(*args, **kwargs):
        assert not kwargs.get("replacement_filled"), "wrong identity reached fill adoption"
    monkeypatch.setattr(journal, "change", forbid_wrong_identity_adoption)
    assert journal.reconcile_feedback(PRIOR_TOKEN, job) == job
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert not h.state.cw_resting_taken
    h.state.resting_active = False
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()
    assert h.service._rpg_open_refusal(TradeIntentEvent(source_service="schwab-1m-v2",
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            symbol="OLOX", side="buy", intent_type="open", quantity=Decimal(1), reason="control")))


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["filled_status", "partial_status", "fill_row", "fill_report", "positive_report",
                                   "canonical_positive", "canonical_unknown", "generation", "revision"])
async def test_zero_proof_cas_rechecks_fill_and_identity_at_commit(monkeypatch, race):
    h = await chain_runtime(monkeypatch)
    journal = HandoffJournal(h.factory)
    change = journal.change

    def inject(token, revision, **updates):
        if updates.get("replacement_terminal_report"):
            with h.factory() as session:
                order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
                if race == "filled_status":
                    order.status = "filled"
                elif race == "partial_status":
                    order.status = "partially_filled"
                elif race == "fill_row":
                    session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                        broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
                        quantity=Decimal(1), price=Decimal("1.47"), payload={}))
                elif race in {"fill_report", "positive_report", "canonical_positive", "canonical_unknown"}:
                    session.add(BrokerOrderEvent(order_id=order.id,
                        event_type="partially_filled" if race == "fill_report" else "cancelled",
                        event_source="broker", payload={"client_order_id": order.client_order_id,
                            **({"filled_quantity": "1" if race == "canonical_positive" else "NaN"}
                               if race.startswith("canonical") else {}),
                            "metadata": {"rpg_terminal_filled_quantity": "0" if race.startswith("canonical") else "1"}}))
                elif race == "generation":
                    order.payload = {**order.payload, "rpg_resting_generation": "foreign"}
                else:
                    row = session.get(DashboardSnapshot, token)
                    row.payload = {**row.payload, "revision": revision + 1}
                session.commit()
        return change(token, revision, **updates)

    monkeypatch.setattr(journal, "change", inject)
    job = journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))
    assert not replacement_terminal_zero(job) and job["phase"] == "placed"
    sibling = h.state.resting_webull_generation, h.state.resting_webull_quantity
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert (h.state.resting_webull_generation, h.state.resting_webull_quantity) == sibling


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["1", "NaN", "-1", "malformed"])
async def test_canonical_report_quantity_cannot_be_hidden_by_later_zero(monkeypatch, quantity):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
            payload={"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id,
                     "filled_quantity": quantity, "metadata": {"rpg_terminal_filled_quantity": "0"}}))
        session.commit()
    journal = HandoffJournal(h.factory)
    if quantity != "1":
        def forbid_unknown_quantity_publication(*args, **kwargs):
            assert not kwargs.get("replacement_terminal_report"), "unknown quantity reached zero publication"
        monkeypatch.setattr(journal, "change", forbid_unknown_quantity_publication)
    job = journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))
    assert not replacement_terminal_zero(job)
    assert job["phase"] == ("filled" if quantity == "1" else "placed")
    assert bool(job.get("no_rebuy")) is (quantity == "1")
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    h.state.resting_active = False
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()
    assert h.state.cw_resting_taken is (quantity == "1")
    assert journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))["phase"] == job["phase"]


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["0", None, "1", "NaN"])
async def test_expired_exact_broker_report_requires_explicit_zero(monkeypatch, quantity):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        session.delete(session.get(DashboardSnapshot, C9_TOKEN))
        order = session.get(BrokerOrder, UUID(CHAIN["cancelled_replacement"]["id"]))
        order.status = "expired"
        metadata = {**order.payload, "atr_reprice_terminal_cancel": "true"}
        if quantity is not None:
            metadata["rpg_terminal_filled_quantity"] = quantity
        session.add(BrokerOrderEvent(order_id=order.id, event_type="expired", event_source="broker",
            payload={"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id,
                     "metadata": metadata}))
        session.commit()
    journal = HandoffJournal(h.factory)
    job = journal.reconcile_feedback(PRIOR_TOKEN, journal.read(PRIOR_TOKEN))
    assert replacement_terminal_zero(job) is (quantity == "0")
    assert job["phase"] == ("refused" if quantity == "0" else "filled" if quantity == "1" else "placed")


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
async def test_roundup_missing_sibling_wire_does_not_block_exact_clear_leg(monkeypatch, account):
    h = await chain_runtime(monkeypatch)
    h.strategy.settings = h.strategy.settings.model_copy(update=ALL_ON)
    assert all(getattr(h.strategy.settings, flag) for flag in ALL_ON)
    assert h.strategy._resting_round_up_enabled()
    prior = deepcopy(h.strategy._rpg_handoffs[str(PRIOR_TOKEN)])
    other = "live:orb" if account == "live:schwab_1m_v2" else "live:schwab_1m_v2"
    clear = deepcopy(h.strategy._rpg_handoffs[str(C9_TOKEN)])
    # Controlled account permutation of a validated OMS proof, not a new
    # recorded broker observation. Keep every proof identity field consistent.
    for request in (clear["old"], clear["replacement"], clear["replacement_terminal_report"]):
        request["broker_account_name"] = account
    prior["old"]["broker_account_name"] = other
    prior.pop("replacement_wire_prices", None)
    h.strategy._rpg_handoffs = {"controlled-sibling": prior, "controlled-clear": clear}
    assert not h.strategy._rpg_entry_owned(h.state, account=account, slot="first")
    assert h.strategy._rpg_entry_owned(h.state, account=other, slot="first")
    assert h.strategy._rpg_leg_owned(h.state, other)  # Its own unproven wire still blocks serial repricing.


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "cancelled_empty", "fills", "wrong_broker"])
async def test_proof_only_replacement_read_is_bounded_by_evidence_edge_and_never_submits(monkeypatch, outcome):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        session.delete(session.get(DashboardSnapshot, C9_TOKEN))
        session.commit()
    journal = HandoffJournal(h.factory)
    journal.change(PRIOR_TOKEN, journal.read(PRIOR_TOKEN)["revision"],
                   phase="refused", reason="replacement_terminal_accounted")
    broker_id = CHAIN["cancelled_replacement"]["broker_id"]
    h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED missing exact detail")
    if outcome in {"cancelled_empty", "wrong_broker"}:
        h.adapter.override = AtrBuyReadback("cancelled_empty", "CONTROLLED exact zero",
            Decimal(0), terminal_cancel=True, broker_status="CANCELED",
            broker_order_id=broker_id if outcome == "cancelled_empty" else "foreign")
    elif outcome == "fills":
        h.adapter.override = AtrBuyReadback("fills", "CONTROLLED positive with price unavailable",
                                           Decimal(1), broker_order_id=broker_id)
    await h.service._rpg_advance(PRIOR_TOKEN, proof_edge="CONTROLLED:startup")
    await h.service._rpg_advance(PRIOR_TOKEN, proof_edge="CONTROLLED:startup")
    assert len(h.adapter.reads) == 1 and not h.adapter.opens and not h.adapter.cancels
    job = journal.read(PRIOR_TOKEN)
    assert replacement_terminal_zero(job) is (outcome == "cancelled_empty")
    assert bool(job.get("replacement_filled")) is (outcome == "fills")
    h.strategy.rpg_handoff_authorization(str(PRIOR_TOKEN), job)
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="first") is (outcome != "cancelled_empty")
    if outcome == "unknown":
        h.adapter.override = AtrBuyReadback("cancelled_empty", "CONTROLLED later exact zero",
            Decimal(0), terminal_cancel=True, broker_status="CANCELED", broker_order_id=broker_id)
        await h.service._rpg_advance(PRIOR_TOKEN, proof_edge="CONTROLLED:new-durable-evidence")
        assert len(h.adapter.reads) == 2
        assert replacement_terminal_zero(journal.read(PRIOR_TOKEN))
    elif outcome == "fills":
        h.adapter.override = AtrBuyReadback("cancelled_empty", "CONTROLLED zero cannot undo fill",
            Decimal(0), terminal_cancel=True, broker_status="CANCELED", broker_order_id=broker_id)
        await h.service._rpg_advance(PRIOR_TOKEN, proof_edge="CONTROLLED:new-durable-evidence")
        assert journal.read(PRIOR_TOKEN).get("no_rebuy") and not replacement_terminal_zero(journal.read(PRIOR_TOKEN))
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "origin", "code", "quantity", "generation", "segment", "slot",
                                   "account", "symbol", "side", "event", "order_exists", "old_unknown"])
async def test_legacy_audit_requires_exact_identity_and_positive_no_wire(monkeypatch, damage):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        row = session.get(DashboardSnapshot, C9_TOKEN)
        job = deepcopy(row.payload)
        job.pop("replacement_terminal_report", None)
        intent = session.get(TradeIntent, UUID("99ee8c8c-0e8a-452f-b828-d09cd55ec9e7"))
        payload = deepcopy(intent.payload)
        if damage == "missing":
            session.delete(intent)
        elif damage in {"origin", "code", "event"}:
            payload[{"origin": "refusal_origin", "code": "refusal_code", "event": "event_id"}[damage]] = "unknown"
        elif damage in {"generation", "segment", "slot"}:
            payload["metadata"][{"generation": "rpg_resting_generation", "segment": "fanout_segment_id",
                                 "slot": "cw_entry_slot"}[damage]] = "foreign"
        elif damage == "account":
            account = h.service.store.ensure_broker_account(session, "CONTROLLED:foreign",
                                                            provider="simulated", environment="test")
            intent.broker_account_id = account.id
        elif damage == "quantity":
            intent.quantity += 1
        elif damage in {"symbol", "side"}:
            setattr(intent, damage, "foreign")
        elif damage == "order_exists":
            session.add(BrokerOrder(intent_id=intent.id, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id=job["replacement"]["client_order_id"],
                symbol=intent.symbol, side="buy", quantity=intent.quantity, status="pending",
                order_type="STOP_LIMIT", time_in_force="day", payload=payload["metadata"]))
        else:
            job.pop("cleared_at", None)
            job.pop("local_no_wire", None)
        if damage != "missing":
            intent.payload = payload
        row.payload = deepcopy(job)
        session.commit()
    journal = HandoffJournal(h.factory)
    before = journal.read(C9_TOKEN)
    assert journal.reconcile_feedback(C9_TOKEN, before) == before
    assert not replacement_terminal_zero(before)
    h.strategy._rpg_handoffs = {str(C9_TOKEN): before}
    h.state.resting_active = False
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="first")
    h.strategy._queue_resting_place(h.state, h.state.atr_trail)
    assert not h.strategy.drain_pending_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["origin", "quantity", "generation", "order_exists", "revision"])
async def test_legacy_zero_publication_revalidates_audit_under_cas(monkeypatch, race):
    h = await chain_runtime(monkeypatch)
    with h.factory() as session:
        row = session.get(DashboardSnapshot, C9_TOKEN)
        job = deepcopy(row.payload)
        job.pop("replacement_terminal_report", None)
        row.payload = job
        session.commit()
    journal = HandoffJournal(h.factory)
    change = journal.change

    def inject(token, revision, **updates):
        with h.factory() as session:
            intent = session.get(TradeIntent, UUID(updates["_expected_intent"]))
            if race == "origin":
                intent.payload = {**intent.payload, "refusal_origin": "unknown"}
            elif race == "quantity":
                intent.quantity += 1
            elif race == "generation":
                intent.payload = {**intent.payload, "metadata": {
                    **intent.payload["metadata"], "rpg_resting_generation": "foreign"}}
            elif race == "order_exists":
                session.add(BrokerOrder(intent_id=intent.id, strategy_id=intent.strategy_id,
                    broker_account_id=intent.broker_account_id, client_order_id=job["replacement"]["client_order_id"],
                    symbol=intent.symbol, side="buy", quantity=intent.quantity, status="pending",
                    order_type="STOP_LIMIT", time_in_force="day", payload=intent.payload["metadata"]))
            else:
                row = session.get(DashboardSnapshot, token)
                row.payload = {**row.payload, "revision": revision + 1}
            session.commit()
        return change(token, revision, **updates)

    monkeypatch.setattr(journal, "change", inject)
    job = journal.reconcile_feedback(C9_TOKEN, journal.read(C9_TOKEN))
    assert not replacement_terminal_zero(job)
    h.strategy._rpg_handoffs = {str(C9_TOKEN): job}
    assert h.strategy._rpg_entry_owned(h.state, account="live:schwab_1m_v2", slot="first")
