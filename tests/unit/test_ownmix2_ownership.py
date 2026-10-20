"""Controlled shared-account ownership cases, not a live SAIQ replay.

R1/R2 TOP15:47 excludes historical Fill/raw-account coherence at this
LOCAL ownership gate. TOP16:33 explicitly restores other bots' live-book holds;
manual surplus alone may clear. Historical rows remain inputs to the consumers'
independent cancellation/native/fill proofs, never execution attribution here.
The existing FLYE fixture supplies exact settled cancellation tokens. Only the
shared-account inventory varies here; no provider, native-flat or cancel proof
is replaced with an always-clear stub.
"""

from dataclasses import replace
from copy import deepcopy
from datetime import UTC, datetime
import asyncio
from decimal import Decimal
from threading import get_ident
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, event as sql_event, select
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.owned_inventory import owned_position_present
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE, record_receipts
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from tests.unit.test_v2_fresh_sell_owner_release import (
    PRIMARY, MIRROR, instant, setup, sell,
)


SCENARIOS = [
    ("manual1000_own_flat", True),
    ("manual4000_own_flat", True),
    ("own_virtual_open", False),
    ("own_managed_open", False),
    ("own_managed_open_zero", False),
    ("manual_and_own_working_buy", False),
    ("manual_and_own_null_buy", False),
    ("unreadable_account", False),
    ("own149_manual4000", False),
    # TOP16:33 supersedes the earlier foreign-book CLEAR interpretation only.
    ("foreign_virtual_backed", False),
    ("foreign_virtual_underbacked", False),
    ("foreign_managed_open", False),
    ("foreign_managed_open_zero", False),
    # Historical fill absence/coherence is still not a live holding predicate.
    ("own_fill_without_projection", True),
    ("own_filled_buy_missing_fill", True),
    ("negative_own_virtual", False),
    ("unknown_own_managed_status", False),
]


def receipt(case, *, unreadable=False, position_count=1):
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
        rows = [(name, instant(case.clock[0]), position_count if name == case.account else 0)
                for name in (PRIMARY, MIRROR) if not (unreadable and name == case.account)]
        record_receipts(session, rows, recorded_at=instant(case.clock[0]))
    case.bot.strategy.apply_flip_position_book(FlipPositionBook(case.clock[0], True, {}))


def inventory_case(monkeypatch, account, scenario):
    schema_metadata = None
    if scenario == "manual_and_own_null_buy":
        # Disposable legacy-schema control only: clone ALL tables/FKs, never
        # mutate Base or pretend the production NOT NULL schema accepts NULL.
        assert Base.metadata.tables["broker_orders"].c.status.nullable is False
        schema_metadata = MetaData(naming_convention=Base.metadata.naming_convention)
        for table in Base.metadata.tables.values():
            table.to_metadata(schema_metadata)
        schema_metadata.tables["broker_orders"].c.status.nullable = True
        assert Base.metadata.tables["broker_orders"].c.status.nullable is False
    bot, state, clock, request, writes, engine = setup(
        monkeypatch, purpose="retry_exhausted", schema_metadata=schema_metadata)
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
                      4000 if scenario == "manual4000_own_flat" else
                      1050 if scenario == "foreign_virtual_backed" else
                      20 if scenario == "foreign_virtual_underbacked" else 1000)
        session.add(AccountPosition(broker_account_id=account_id, symbol=request.symbol,
                                    quantity=raw, average_price=Decimal("5.4483"),
                                    source_updated_at=instant(clock[0])))
        if scenario in {"own_virtual_open", "own149_manual4000"}:
            session.add(VirtualPosition(strategy_id=own.id, broker_account_id=account_id,
                symbol=request.symbol, quantity=Decimal(149), average_price=Decimal(5)))
        if scenario == "negative_own_virtual":
            session.add(VirtualPosition(strategy_id=own.id, broker_account_id=account_id,
                symbol=request.symbol, quantity=Decimal(-1), average_price=Decimal(5)))
        if scenario == "unknown_own_managed_status":
            session.add(OmsManagedPosition(strategy_code=own.code, broker_account_name=account,
                symbol=request.symbol, status="unknown", original_quantity=149, current_quantity=0,
                entry_price=Decimal(5), entry_time=at))
        if scenario in {"own_managed_open", "own_managed_open_zero", "own149_manual4000"}:
            session.add(OmsManagedPosition(strategy_code=own.code, broker_account_name=account,
                symbol=request.symbol, status="open", original_quantity=149,
                current_quantity=0 if scenario == "own_managed_open_zero" else 149,
                entry_price=Decimal(5), entry_time=at))
        if scenario in {"foreign_managed_open", "foreign_managed_open_zero"}:
            session.add(OmsManagedPosition(strategy_code=foreign.code, broker_account_name=account,
                symbol=request.symbol, status="open", original_quantity=50,
                current_quantity=0 if scenario == "foreign_managed_open_zero" else 50,
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
            # An OPEN shares the opportunity, never the cancellation receipt identity.
            for key in ("clearwait_removal_token", "clearwait_opportunity_id", "clearwait_buy_only",
                        "resting_entry_cancel", "reason"):
                metadata.pop(key, None)
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
    if scenario == "manual_and_own_null_buy":
        assert Base.metadata.tables["broker_orders"].c.status.nullable is False
        with bot.session_factory() as session:
            # A separate committed read proves an actual SQL NULL row exists.
            assert session.scalar(select(BrokerOrder.id).where(
                BrokerOrder.id == order.id, BrokerOrder.status.is_(None))) == order.id
            assert session.scalar(select(BrokerOrder.status).where(BrokerOrder.id == order.id)) is None
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
def test_actual_ownership_callers_use_live_own_books_not_historical_or_foreign_balances(monkeypatch, site, account, scenario, clear):
    case = inventory_case(monkeypatch, account, scenario)
    try:
        before = case.bot._removed_wait_store.restore()
        proof = probe(case, site)
        assert proof.request == case.request and proof.clear is clear
        assert proof.reason
        assert case.bot._removed_wait_store.restore() == before
        assert case.bot.strategy._removed_wait_requests[case.request.symbol] == case.request
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("scenario", ["unreadable_account", "negative_own_virtual", "unknown_own_managed_status"])
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
def test_malformed_completeness_receipt_never_proves_readable_account(monkeypatch, account, count):
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
            # JSON equality treats 1 == True; persist the invalid typed input
            # rather than allowing change detection to retain a valid count.
            flag_modified(row, "payload")
        if count is True:
            with case.bot.session_factory() as session:
                saved = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.payload["broker_account_name"].as_string() == account))
                assert type(saved.payload["position_count"]) is bool
                assert saved.payload["position_count"] is True
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
            # Working/NULL never expires because of age or missing submitted_at;
            # R1 intentionally has no historical-fill cutoff at this gate.
            old = instant(1_700_000_000_000)
            row.submitted_at = None if no_submit else old
            row.updated_at = parent.created_at = parent.updated_at = old
        assert not probe(case, site).clear
        assert case.bot._removed_wait_store.restore()[case.request.symbol] == case.request
    finally:
        case.engine.dispose()


def add_exact_request(case, symbol, *, current_session=False):
    """Clone the controlled cancellation proof, never synthesize broker flatness."""
    request = replace(case.request, symbol=symbol, token="controlled-ownmix-" + symbol)
    if current_session:
        request = replace(request, opportunity_id=case.clock[0]-120000,
                          requested_at_ms=case.clock[0]-60000)
    with case.bot.session_factory.begin() as session:
        originals = session.scalars(select(TradeIntent).where(
            TradeIntent.symbol == case.request.symbol, TradeIntent.intent_type == "cancel")).all()
        assert len(originals) == 2
        for original in originals:
            payload = deepcopy(original.payload)
            md = payload["metadata"]
            md.update(clearwait_removal_token=request.token,
                      clearwait_opportunity_id=str(request.opportunity_id),
                      fanout_segment_id=str(request.opportunity_id), fanout_slot="resting",
                      fanout_slot_id=fanout_slot_id(strategy_code="schwab_1m_v2", symbol=symbol,
                          segment_id=request.opportunity_id, slot="resting"))
            session.add(TradeIntent(id=uuid4(), strategy_id=original.strategy_id,
                broker_account_id=original.broker_account_id, symbol=symbol, side=original.side,
                intent_type=original.intent_type, quantity=original.quantity, reason=original.reason,
                status=original.status, payload=payload,
                created_at=(instant(request.requested_at_ms+1000) if current_session else original.created_at),
                updated_at=(instant(case.clock[0]-10000) if current_session else original.updated_at)))
    case.bot._removed_wait_store.record(request, True)
    return request


@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("manual_quantity", [0, 1000])
def test_r1_saiq_buy60_unbooked_external_sale_does_not_resurrect_local_holding(monkeypatch, account, manual_quantity):
    # Controlled SAIQ shape from TOP15:47, NOT a vendor receipt replay. The
    # external sale is intentionally unbooked: no synthetic SELL/Fill is added.
    case = inventory_case(monkeypatch, account, "manual1000_own_flat")
    try:
        case.clock[0] = int(datetime(2026, 10, 20, 18, 21, 2, tzinfo=UTC).timestamp() * 1000)
        request = add_exact_request(case, "SAIQ", current_session=True)
        bought = datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
        with case.bot.session_factory.begin() as session:
            account_id = next(key for key, name in case.accounts.items() if name == account)
            parent = TradeIntent(id=uuid4(), strategy_id=case.strategy_id, broker_account_id=account_id,
                symbol="SAIQ", side="buy", intent_type="open", quantity=60, status="filled",
                reason="CONTROLLED SAIQ 08:01 historical BUY", payload={}, created_at=bought, updated_at=bought)
            session.add(parent)
            session.flush()
            order = BrokerOrder(id=uuid4(), intent_id=parent.id, strategy_id=case.strategy_id,
                broker_account_id=account_id, client_order_id="controlled-saiq-ownmix-buy60",
                broker_order_id="controlled-saiq-ownmix-native", symbol="SAIQ", side="buy",
                quantity=60, status="filled", order_type="limit", time_in_force="day", payload={},
                submitted_at=bought, updated_at=bought)
            session.add(order)
            session.flush()
            session.add(Fill(order_id=order.id, strategy_id=case.strategy_id, broker_account_id=account_id,
                broker_fill_id="controlled-saiq-ownmix-buy60-fill", symbol="SAIQ", side="buy",
                quantity=60, price=Decimal("5"), filled_at=bought, payload={"source": "CONTROLLED"}))
            session.add(VirtualPosition(strategy_id=case.strategy_id, broker_account_id=account_id,
                symbol="SAIQ", quantity=0, average_price=0))
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="SAIQ", status="closed", original_quantity=60, current_quantity=0,
                entry_price=Decimal("5"), entry_time=bought))
            session.add(AccountPosition(broker_account_id=account_id, symbol="SAIQ",
                quantity=manual_quantity, average_price=Decimal("5.4483"),
                source_updated_at=instant(case.clock[0])))
        receipt(case, position_count=2 if manual_quantity else 1)
        with case.bot.session_factory() as session:
            assert owned_position_present(session, symbol="SAIQ", accounts=case.accounts,
                strategy_id=case.strategy_id, now=instant(case.clock[0])) is False
            assert len(session.scalars(select(Fill).where(Fill.symbol == "SAIQ")).all()) == 1
            assert session.scalar(select(AccountPosition.quantity).where(AccountPosition.symbol == "SAIQ")) == manual_quantity
            assert session.scalar(select(VirtualPosition.quantity).where(VirtualPosition.symbol == "SAIQ")) == 0
            assert session.scalar(select(OmsManagedPosition.status).where(OmsManagedPosition.symbol == "SAIQ")) == "closed"
        # These original cancellation-only readers must still evaluate their
        # own exact proofs; the helper's false is not broker-flat authority.
        for method in (case.bot._removed_wait_store.proofs, case.bot._removed_wait_store.clear_rule_proofs):
            proof, = method((request,), {PRIMARY, MIRROR}, now=instant(case.clock[0]))
            assert proof.clear and proof.reason == "retry_leftovers_cancelled_owner_kept", proof.reason
        assert case.bot._fetch_fresh_sell_release_evidence((request,))[0].clear
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("site", ["fresh_sell", "removed_wait", "clear_rules"])
@pytest.mark.parametrize("error", [TypeError, ArithmeticError])
def test_r2_other_typed_unreadable_own_book_errors_are_request_local(monkeypatch, site, error):
    from project_mai_tai.services import schwab_1m_v2_bot as bot_module
    from project_mai_tai import v2_removed_wait as wait_module

    case = inventory_case(monkeypatch, PRIMARY, "manual1000_own_flat")
    good = add_exact_request(case, "GOOD")
    module = bot_module if site == "fresh_sell" else wait_module
    real_helper = module.owned_position_present

    def unreadable(session, **kwargs):
        if kwargs["symbol"] == case.request.symbol:
            raise error("CONTROLLED typed unreadable own book")
        return real_helper(session, **kwargs)

    monkeypatch.setattr(module, "owned_position_present", unreadable)
    try:
        if site == "fresh_sell":
            proofs = case.bot._fetch_fresh_sell_release_evidence((case.request, good))
        else:
            method = (case.bot._removed_wait_store.proofs if site == "removed_wait"
                      else case.bot._removed_wait_store.clear_rule_proofs)
            proofs = method((case.request, good), {PRIMARY, MIRROR}, now=instant(case.clock[0]))
        assert len(proofs) == 2
        assert proofs[0].request == case.request
        assert not proofs[0].clear and proofs[0].reason == "own_position_evidence_UNKNOWN"
        assert proofs[1].request == good and proofs[1].clear
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("site", ["fresh_sell", "removed_wait", "clear_rules"])
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
def test_r2_bad_own_book_is_unknown_for_one_request_and_next_symbol_still_clears(monkeypatch, site, account):
    case = inventory_case(monkeypatch, account, "negative_own_virtual")
    try:
        good = add_exact_request(case, "GOOD")
        requests = (case.request, good)
        before = case.bot._removed_wait_store.restore()
        if site == "fresh_sell":
            proofs = case.bot._fetch_fresh_sell_release_evidence(requests)
        else:
            method = (case.bot._removed_wait_store.proofs if site == "removed_wait"
                      else case.bot._removed_wait_store.clear_rule_proofs)
            proofs = method(requests, {PRIMARY, MIRROR}, now=instant(case.clock[0]))
        bad_proof, good_proof = proofs
        assert bad_proof.request == case.request
        assert not bad_proof.clear and bad_proof.reason == "own_position_evidence_UNKNOWN"
        assert good_proof.request == good and good_proof.clear, good_proof.reason
        assert case.bot._removed_wait_store.restore() == before
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["fresh_sell", "removed_wait", "clear_rules"])
async def test_actual_poll_dispatches_owned_book_reads_off_event_loop(monkeypatch, site):
    from project_mai_tai.services import schwab_1m_v2_bot as bot_module
    from project_mai_tai import v2_removed_wait as wait_module

    case = inventory_case(monkeypatch, PRIMARY, "manual1000_own_flat")
    loop_thread, seen = get_ident(), []
    module = bot_module if site == "fresh_sell" else wait_module
    real_helper = module.owned_position_present

    def observed(*args, **kwargs):
        seen.append(get_ident())
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(module, "owned_position_present", observed)
    try:
        if site == "fresh_sell":
            await case.bot._fresh_sell_owner_release_poll()
            assert case.bot.strategy._fresh_sell_release_evidence[case.request.symbol].clear
        else:
            case.bot.strategy._removed_wait_clear_rules_enabled = site == "clear_rules"
            await case.bot._removed_wait_poll()
        assert seen and all(thread != loop_thread for thread in seen)
        # Rules mode can persist its normal clear receipt; this is not a fresh
        # SELL acknowledgement or permission to replace the still-owned actor.
        assert case.bot.strategy._removed_wait_requests[case.request.symbol] == case.request
        assert case.state.flip_owner_phase == "bound"
        assert not case.bot.strategy.drain_pending_intents()
        assert not case.bot.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


def test_flat_local_gate_has_bounded_select_only_query_cost_without_fill_or_raw_reads(monkeypatch):
    case = inventory_case(monkeypatch, PRIMARY, "manual1000_own_flat")
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sql_event.listen(case.engine, "before_cursor_execute", capture)
    try:
        with case.bot.session_factory() as session:
            assert owned_position_present(session, symbol=case.request.symbol, accounts=case.accounts,
                strategy_id=case.strategy_id, now=instant(case.clock[0])) is False
        # Accounts, strategy, own VP/managed/BUY/intent, other VP/managed, receipt.
        assert len(statements) == 9
        assert all(statement.lstrip().lower().startswith("select") for statement in statements)
        assert not any("from fills" in statement.lower() or "from account_positions" in statement.lower()
                       for statement in statements)
    finally:
        sql_event.remove(case.engine, "before_cursor_execute", capture)
        case.engine.dispose()


@pytest.mark.asyncio
async def test_quote_rate_60s_includes_actual_ownership_poll_query_cost(monkeypatch):
    """AUTHOR ONLY until main's serial rate slot; same strict 60s/200/s/50ms gates."""
    from tests.unit.test_edgeprot1_lane_b import setup as oms_setup, current_settings, quote, drain
    from project_mai_tai.oms.service import OmsRiskService

    case = inventory_case(monkeypatch, PRIMARY, "manual1000_own_flat")
    initial, sf, broker = oms_setup(monkeypatch)
    svc = OmsRiskService(current_settings(), redis_client=initial.redis,
                         session_factory=sf, broker_adapter=broker)
    svc._rehydrate_managed_v2_symbols()
    monkeypatch.setattr(svc, "_market_is_fillable", lambda: True)
    original_db = svc._run_db

    async def slow_db(work, **kwargs):
        await asyncio.sleep(.05)
        return await original_db(work, **kwargs)

    monkeypatch.setattr(svc, "_run_db", slow_db)
    statements, polls, handlers = [], [], []
    loop_thread, max_stall = get_ident(), 0.0
    stop = asyncio.Event()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append((get_ident(), statement))

    async def meter():
        nonlocal max_stall
        previous = monotonic()
        while not stop.is_set():
            await asyncio.sleep(.001)
            now = monotonic()
            max_stall = max(max_stall, (now - previous - .001) * 1000)
            previous = now

    async def poll():
        # Existing worker entry points, both removed-wait modes, no provider or
        # new production cadence. Test load is one complete cycle per second.
        while not stop.is_set():
            await case.bot._fresh_sell_owner_release_poll()
            for rules in (False, True):
                case.bot.strategy._removed_wait_clear_rules_enabled = rules
                await case.bot._removed_wait_poll()
            polls.append(monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass

    sql_event.listen(case.engine, "before_cursor_execute", capture)
    meter_task = asyncio.create_task(meter())
    poll_task = asyncio.create_task(poll())
    start = monotonic()
    try:
        for index in range(13500):
            await asyncio.sleep(max(0, start + index / 225 - monotonic()))
            before = monotonic()
            await svc._handle_quote_tick_event(quote("2.52"))
            handlers.append((monotonic() - before) * 1000)
            assert len(svc._symbol_tick_work) <= 1
        await asyncio.sleep(max(0, start + 60 - monotonic()))
    finally:
        stop.set()
        try:
            # Do not dispose a SQLite engine beneath any actual DB worker.
            await asyncio.gather(meter_task, poll_task)
            await drain(svc)
        finally:
            sql_event.remove(case.engine, "before_cursor_execute", capture)
            case.engine.dispose()
            sf.kw["bind"].dispose()
    elapsed = monotonic() - start
    print(f"OWNMIX2 quotes=13500 seconds={elapsed:.6f} rate={13500 / elapsed:.3f} "
          f"stall_ms={max_stall:.3f} handler_ms={max(handlers):.3f} "
          f"polls={len(polls)} db_statements={len(statements)}")
    assert elapsed >= 60 and 13500 / elapsed >= 200
    assert max_stall < 50 and max(handlers) < 50
    assert len(polls) >= 50 and statements
    assert all(thread != loop_thread for thread, _statement in statements)
    # Include the real clear-rule receipt transaction cost too, rather than
    # replacing the store with a read-only/always-acknowledged fake.
    assert any(statement.lstrip().lower().startswith("select") for _thread, statement in statements)
    assert not broker.requests


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
            proof, = case.bot._removed_wait_store.proofs((case.request,), {account}, now=instant(case.clock[0]))
            assert not proof.clear and proof.reason == "own_position_evidence_UNKNOWN"
        else:
            proof, = case.bot._removed_wait_store.proofs((case.request,), {account}, now=instant(case.clock[0]))
            assert proof.request == case.request and proof.clear
            assert proof.reason == "retry_leftovers_cancelled_owner_kept"
        assert case.bot._removed_wait_store.restore()[case.request.symbol] == case.request
    finally:
        case.engine.dispose()
