"""Exact retained rows + disclosed CONTROLLED runtime, never native replay."""
import json
import os
from types import SimpleNamespace
from uuid import UUID

import pytest

from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
from project_mai_tai.brokerdecides1_entry import entry_decision
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.cancelack1 import _closed_order, _closed_with_replacements, _at
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from tests.unit.wbpa3_episode_port import ACCOUNT_NAMES, OPPORTUNITY, seed_episode

pytestmark = pytest.mark.skipif(
    not os.environ.get("WBPA3_SOURCE_DIRECTORY") or os.environ.get("WBPA3_EXPECTED_MODE") != "OLD",
    reason="Historical OLD-only private profile; original execution receipt retains the unmodified body")


def test_original_four_rejections_real_census_and_sell_path(tmp_path, monkeypatch, record_property):
    case = seed_episode(tmp_path)
    observed = _at(case.observed)
    from project_mai_tai import brokerdecides1_sync as sync
    monkeypatch.setattr(sync, "now_ms", lambda: observed)
    try:
        with case.factory() as session:
            predicates = []
            for raw in case.raw["orders"]:
                order = session.get(BrokerOrder, UUID(raw["id"]))
                parent = session.get(TradeIntent, order.intent_id)
                event = session.get(BrokerOrderEvent, UUID(next(r["id"] for r in case.raw["order_events"] if r["order_id"] == raw["id"])))
                assert order.status == parent.status == event.event_type == "rejected"
                assert event.event_source == "broker"
                assert order.broker_order_id is None
                assert _at(event.event_at) < _at(order.submitted_at)
                assert not _closed_order(session, order, observed)
                assert not _closed_with_replacements(session, order, observed)
                predicates.append({"coid": order.client_order_id, "native_id": None,
                    "event_ms": _at(event.event_at), "submitted_ms": _at(order.submitted_at),
                    "first_false": "broker_order_id_or_events", "latent_false": "submitted_at_le_event_at"})
            inflight, fills, terminals = local_evidence(session, ACCOUNT_NAMES)
            assert inflight == frozenset({"CHOW"}) and not fills and not terminals
        settings = Settings(_env_file=None, strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_keepwatch1_enabled=True,
            strategy_schwab_1m_v2_brokerdecides1_enabled=True,
            strategy_schwab_1m_v2_brokerdecides1_max_age_ms=20000,
            strategy_schwab_1m_v2_country_route_enabled=True,
            strategy_schwab_1m_v2_account_name=ACCOUNT_NAMES[0],
            strategy_schwab_1m_v2_webull_account_name=ACCOUNT_NAMES[1])
        strategy = SchwabV2Strategy(settings)
        strategy._now_ms = lambda: observed
        owners = FlipEntryOwnershipStore(case.factory)
        identities = FanoutSegmentIdentityStore(case.factory)
        strategy.configure_flip_entry_ownership(
            lambda record, active, reason: owners.record(record, active=active, reason=reason, now=case.observed), restore_readable=True)
        strategy.configure_fanout_identity_persistence(
            lambda symbol, segment, active, reason: identities.record(symbol, segment, active, reason, now=case.observed))
        state = strategy.watchlist_state("CHOW")
        state.flip_owner_phase = "resting"
        state.flip_owner_opportunity_id = state.fanout_segment_id = OPPORTUNITY
        state.flip_owner_first_rest_placed = True
        state.flip_owner_evidence_readable = True
        state.flip_owner_evidence_at_ms = observed
        state.atr_short_flip_bar_ts = observed - 60000
        state.atr_state = "short"
        memory = strategy._brokerdecides1_memory = AdmissionMemory()
        # CONTROLLED complete native-empty operands expose the durable local
        # blocker; these are NOT exported native-book/current runtime evidence.
        def snapshot(generation, age):
            books = tuple(NativeAccountBook(a, p, observed-age, observed-age+1,
                frozenset(), frozenset(), True, "CONTROLLED_not_native")
                for a,p in zip(ACCOUNT_NAMES,("schwab","webull"),strict=True))
            return AdmissionSnapshot("CONTROLLED",generation,observed-age,observed-age+1,
                books,inflight,frozenset(),True,"CONTROLLED_not_native")
        memory.apply(snapshot(1, 2))
        assert strategy._sell_owner_current_blocker(state) == "unanswered_submit"
        strategy._end_flip_owner_on_sell(state)
        assert state.flip_owner_phase == "unknown"
        assert entry_decision(strategy, "CHOW").reason == "own_inflight_UNKNOWN_or_working"
        memory.apply(snapshot(2, 25000))
        assert entry_decision(strategy, "CHOW").reason == "snapshot_stale"
        # The original recovery reader independently demands both accounts,
        # while retained country-primary opens/dispatches only contain Webull.
        bot = SimpleNamespace(settings=settings)
        with case.factory() as session:
            terminal, evaluated = SchwabV2BotService._terminal_unfilled_first_rest_opportunities(
                bot, session, account_names=ACCOUNT_NAMES, unknown_opportunities={"CHOW": OPPORTUNITY})
        assert terminal == {} and evaluated == 1
        assert {r["broker_account_id"] for r in case.raw["intents"] if r["intent_type"] == "open"} == {case.raw["accounts"][1]["id"]}
        record_property("predicate_rows", json.dumps(predicates,sort_keys=True))
        record_property("provenance", "actual exported4orders4events7intents5dispatchrows; omittedstrategy/fillabsence/nativeempty/ownerstate CONTROLLED; no actual runtime replay")
        record_property("actual_v2_checkpoint", "UNKNOWN_not_supplied")
    finally:
        case.engine.dispose()
