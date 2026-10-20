"""Controlled shared-account ownership cases, not a live SAIQ replay.

The existing FLYE fixture supplies exact settled cancellation tokens. Only the
shared-account inventory varies here; no provider, native-flat or cancel proof
is replaced with an always-clear stub.
"""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.owned_inventory import owned_position_present
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE, record_receipts
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from tests.unit.test_v2_fresh_sell_owner_release import (
    PRIMARY, MIRROR, instant, setup, sell,
)


SCENARIOS = [
    ("manual1000_own_flat", True),
    ("own_virtual_open", False),
    ("own_managed_open", False),
    ("manual_and_own_working_buy", False),
    ("manual_and_own_null_buy", False),
    ("unreadable_account", False),
    ("own149_manual4000", False),
    ("foreign_virtual_backed", True),
    ("foreign_virtual_underbacked", False),
    ("own_fill_without_projection", False),
    ("own_filled_buy_missing_fill", False),
]


def receipt(case, *, unreadable=False):
    with case.bot.session_factory.begin() as session:
        if unreadable:
            # Invalidate a prior receipt too; omitting a new write cannot prove
            # that an older complete read is unavailable.
            for row in session.scalars(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["broker_account_name"].as_string() == case.account)):
                session.delete(row)
        # Complete controlled position snapshots: one manual/mixed symbol on
        # the chosen account, known-empty on the other named account.
        rows = [(name, instant(case.clock[0]), int(name == case.account))
                for name in (PRIMARY, MIRROR) if not (unreadable and name == case.account)]
        record_receipts(session, rows, recorded_at=instant(case.clock[0]))
    case.bot.strategy.apply_flip_position_book(FlipPositionBook(case.clock[0], True, {}))


def inventory_case(monkeypatch, account, scenario):
    bot, state, clock, request, writes, engine = setup(monkeypatch, purpose="retry_exhausted")
    case = SimpleNamespace(bot=bot, state=state, clock=clock, request=request,
                           writes=writes, engine=engine, account=account)
    at = instant(clock[0]-60000)
    with bot.session_factory.begin() as session:
        accounts = {row.name: row for row in session.scalars(select(BrokerAccount))}
        strategies = {row.code: row for row in session.scalars(select(Strategy))}
        own, foreign = strategies["schwab_1m_v2"], strategies["orb_schwab"]
        case.accounts = {row.id: row.name for row in accounts.values()}
        case.strategy_id = own.id
        account_id = accounts[account].id
        raw = Decimal(4149 if scenario == "own149_manual4000" else
                      1050 if scenario == "foreign_virtual_backed" else
                      20 if scenario == "foreign_virtual_underbacked" else 1000)
        session.add(AccountPosition(broker_account_id=account_id, symbol=request.symbol,
                                    quantity=raw, average_price=Decimal("5.4483"),
                                    source_updated_at=instant(clock[0])))
        if scenario in {"own_virtual_open", "own149_manual4000"}:
            session.add(VirtualPosition(strategy_id=own.id, broker_account_id=account_id,
                symbol=request.symbol, quantity=Decimal(149), average_price=Decimal(5)))
        if scenario in {"own_managed_open", "own149_manual4000"}:
            session.add(OmsManagedPosition(strategy_code=own.code, broker_account_name=account,
                symbol=request.symbol, status="open", original_quantity=149, current_quantity=149,
                entry_price=Decimal(5), entry_time=at))
        if scenario in {"foreign_virtual_backed", "foreign_virtual_underbacked"}:
            session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=account_id,
                symbol=request.symbol, quantity=Decimal(50), average_price=Decimal(5)))
        opening = scenario in {"manual_and_own_working_buy", "manual_and_own_null_buy",
                               "own149_manual4000", "own_fill_without_projection",
                               "own_filled_buy_missing_fill", "foreign_virtual_backed",
                               "foreign_virtual_underbacked"}
        if opening:
            strategy = foreign if scenario.startswith("foreign_") else own
            quantity = Decimal(50 if scenario.startswith("foreign_") else 149)
            # Use the fixture's real exact-opportunity metadata. A NULL broker
            # order remains ambiguous even when the parent labels itself filled.
            cancel = session.scalar(select(TradeIntent).where(
                TradeIntent.broker_account_id == account_id, TradeIntent.intent_type == "cancel"))
            metadata = dict(cancel.payload["metadata"])
            working = scenario == "manual_and_own_working_buy"
            null = scenario == "manual_and_own_null_buy"
            parent = TradeIntent(id=uuid4(), strategy_id=strategy.id, broker_account_id=account_id,
                symbol=request.symbol, side="buy", intent_type="open", quantity=quantity,
                status="pending" if working else "filled", reason="CONTROLLED inventory lot",
                payload={"metadata": metadata}, created_at=at, updated_at=at)
            session.add(parent)
            session.flush()
            order = BrokerOrder(id=uuid4(), intent_id=parent.id, strategy_id=strategy.id,
                broker_account_id=account_id, client_order_id="controlled-ownmix-"+str(uuid4()),
                broker_order_id="controlled-native-"+str(uuid4()), symbol=request.symbol,
                side="buy", quantity=quantity, status=None if null else "accepted" if working else "filled",
                order_type="limit", time_in_force="day", payload=metadata,
                submitted_at=at, updated_at=at)
            session.add(order)
            session.flush()
            if not working and not null and scenario != "own_filled_buy_missing_fill":
                session.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account_id,
                    broker_fill_id="controlled-ownmix-fill-"+str(uuid4()), symbol=request.symbol,
                    side="buy", quantity=quantity, price=Decimal(5), filled_at=at,
                    payload={"source": "CONTROLLED broker execution"}))
    receipt(case, unreadable=scenario == "unreadable_account")
    return case


def probe(case, site):
    if site == "fresh_sell":
        return case.bot._fetch_fresh_sell_release_evidence((case.request,))[0]
    store = case.bot._removed_wait_store
    method = store.proofs if site == "removed_wait" else store.clear_rule_proofs
    return method((case.request,), {PRIMARY, MIRROR}, now=instant(case.clock[0]))[0]


@pytest.mark.parametrize("site", ["fresh_sell", "removed_wait", "clear_rules"])
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("scenario,clear", SCENARIOS, ids=[row[0] for row in SCENARIOS])
def test_actual_ownership_callers_ignore_only_proven_nonbot_inventory(monkeypatch, site, account, scenario, clear):
    case = inventory_case(monkeypatch, account, scenario)
    try:
        before = case.bot._removed_wait_store.restore()
        try:
            proof = probe(case, site)
        except ValueError:
            # The shared API explicitly represents unreadable/contradictory
            # inventory with ValueError; a caller must never turn it into CLEAR.
            assert not clear and scenario in {"unreadable_account", "foreign_virtual_underbacked",
                                             "own_filled_buy_missing_fill", "own_fill_without_projection"}
        else:
            assert proof.request == case.request and proof.clear is clear
            assert proof.reason
        assert case.bot._removed_wait_store.restore() == before
        assert case.bot.strategy._removed_wait_requests[case.request.symbol] == case.request
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("scenario", ["unreadable_account", "foreign_virtual_underbacked",
                                     "own_filled_buy_missing_fill", "own_fill_without_projection"])
def test_shared_inventory_unknown_cannot_be_coerced_to_flat(monkeypatch, account, scenario):
    case = inventory_case(monkeypatch, account, scenario)
    try:
        with case.bot.session_factory() as session:
            with pytest.raises(ValueError):
                owned_position_present(session, symbol=case.request.symbol, accounts=case.accounts,
                    strategy_id=case.strategy_id, now=instant(case.clock[0]))
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("count", [None, -1, True], ids=["missing", "negative", "boolean"])
def test_malformed_completeness_receipt_never_proves_manual_surplus(monkeypatch, account, count):
    case = inventory_case(monkeypatch, account, "manual1000_own_flat")
    try:
        with case.bot.session_factory.begin() as session:
            row = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.payload["broker_account_name"].as_string() == account))
            payload = dict(row.payload)
            if count is None:
                payload.pop("position_count")
            else:
                payload["position_count"] = count
            row.payload = payload
        with case.bot.session_factory() as session:
            with pytest.raises(ValueError):
                owned_position_present(session, symbol=case.request.symbol, accounts=case.accounts,
                    strategy_id=case.strategy_id, now=instant(case.clock[0]))
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("site", ["fresh_sell", "removed_wait", "clear_rules"])
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("null", [False, True], ids=["old-working", "old-null"])
@pytest.mark.parametrize("no_submit", [False, True], ids=["old-submitted", "missing-submitted"])
def test_old_or_null_own_buy_never_ages_into_manual_flat(monkeypatch, site, account, null, no_submit):
    scenario = "manual_and_own_null_buy" if null else "manual_and_own_working_buy"
    case = inventory_case(monkeypatch, account, scenario)
    try:
        with case.bot.session_factory.begin() as session:
            row = session.scalar(select(BrokerOrder))
            parent = session.get(TradeIntent, row.intent_id)
            # Older than the verified checkpoint; working/NULL is NEVER expired
            # by that checkpoint or by a missing submitted_at.
            old = instant(1_700_000_000_000)
            row.submitted_at = None if no_submit else old
            row.updated_at = parent.created_at = parent.updated_at = old
        assert not probe(case, site).clear
        assert case.bot._removed_wait_store.restore()[case.request.symbol] == case.request
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
async def test_manual_thousand_release_uses_actual_cancel_proofs_and_later_fresh_bar(monkeypatch, account):
    case = inventory_case(monkeypatch, account, "manual1000_own_flat")
    try:
        bot, state, clock = case.bot, case.state, case.clock
        await bot._fresh_sell_owner_release_poll()
        assert bot.strategy._fresh_sell_release_evidence[case.request.symbol].clear
        sell(bot, state)
        await bot._removed_wait_poll()
        clock[0] += 60000
        state.bars.append(type(state.bars[-1])(
            state.bars[-1].timestamp_ms+60000, state.bars[-1].open, state.bars[-1].high,
            state.bars[-1].low, state.bars[-1].close, state.bars[-1].volume))
        receipt(case)
        await bot._fresh_sell_owner_release_poll()
        bot.strategy._cw_v2_track(state, None)
        assert state.flip_owner_phase == "idle" and not bot.strategy._removed_wait_requests
        assert bot._removed_wait_store.restore() == {}
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
        with bot.session_factory() as session:
            raw = session.scalar(select(AccountPosition).where(AccountPosition.symbol == case.request.symbol))
            assert raw.quantity == 1000  # No account debit, sell or synthetic flat rewrite.
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("unreadable", [False, True], ids=["complete", "missing-receipt"])
def test_single_account_legacy_helper_and_actual_proofs_keep_supported_scope(monkeypatch, account, unreadable):
    case = inventory_case(monkeypatch, account, "manual1000_own_flat")
    try:
        case.request = replace(case.request, account_names=(account,))
        case.accounts = {identity: name for identity, name in case.accounts.items() if name == account}
        # This control has exactly one cancellation account. Do not let the
        # two-account fixture's unrelated receipt masquerade as a matching one.
        with case.bot.session_factory.begin() as session:
            other_ids = session.scalars(select(BrokerAccount.id).where(BrokerAccount.name != account)).all()
            for intent in session.scalars(select(TradeIntent).where(
                    TradeIntent.intent_type == "cancel", TradeIntent.broker_account_id.in_(other_ids))):
                session.delete(intent)
        case.bot._removed_wait_store.record(case.request, True)
        if unreadable:
            receipt(case, unreadable=True)
        with case.bot.session_factory() as session:
            if unreadable:
                with pytest.raises(ValueError, match="owned_inventory_account_read_UNKNOWN"):
                    owned_position_present(session, symbol=case.request.symbol, accounts=case.accounts,
                        strategy_id=case.strategy_id, now=instant(case.clock[0]))
            else:
                assert owned_position_present(session, symbol=case.request.symbol, accounts=case.accounts,
                    strategy_id=case.strategy_id, now=instant(case.clock[0])) is False
        if unreadable:
            with pytest.raises(ValueError, match="owned_inventory_account_read_UNKNOWN"):
                case.bot._removed_wait_store.proofs((case.request,), {account}, now=instant(case.clock[0]))
        else:
            proof, = case.bot._removed_wait_store.proofs((case.request,), {account}, now=instant(case.clock[0]))
            assert proof.request == case.request and proof.clear
            assert proof.reason == "retry_leftovers_cancelled_owner_kept"
        assert case.bot._removed_wait_store.restore()[case.request.symbol] == case.request
    finally:
        case.engine.dispose()
