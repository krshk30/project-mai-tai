"""Source-authored ORB STOP owner controls; run only after C6 composition/grant."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Fill, Strategy, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.oms.bot_control_orb import OrbStopContext, _cancel_entry, recover_exit, trusted_census
from tests.unit.test_orbroute1_oms_chain import SCHWAB, WEBULL, seed_filled_webull, setup as setup


class ControlSettings:
    bot_control_enabled = True

    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)


class Journal:
    """Controlled journal boundary; real fencing/PG proof belongs to main tests."""

    def __init__(self, lease):
        self.lease, self.rows, self.calls = lease, {}, []
        self.store = SimpleNamespace(_leased=self.leased)
        self.active = True

    def leased(self, _session, lease, _now):
        if not self.active or lease != self.lease:
            raise ValueError("lease_fenced")
        return SimpleNamespace(account_scope={"account_ids": [str(v) for v in lease.account_ids]})

    def prepare_native(self, lease, **kwargs):
        assert lease == self.lease and kwargs["managed_id"] is None
        assert kwargs["binding_key"] == "entry-native"
        return self._prepare("native", kwargs)

    def prepare(self, lease, **kwargs):
        assert lease == self.lease
        return self._prepare(kwargs["operation"], kwargs)

    def _prepare(self, key, kwargs):
        self.calls.append(("prepare", key, kwargs))
        if key not in self.rows:
            self.rows[key] = SimpleNamespace(attempt_id=uuid4(), status="PREPARED")
        return self.rows[key]

    def dispatch(self, _lease, identity, **_kwargs):
        row = next(row for row in self.rows.values() if row.attempt_id == identity)
        assert row.status == "PREPARED"
        row.status = "MAY_HAVE_SENT"
        self.calls.append(("dispatch", identity))

    def record_native_outcome(self, _lease, _identity, **kwargs):
        self.calls.append(("native_outcome", kwargs["outcome"]))
        self.rows["native"].status = "TERMINAL" if kwargs["outcome"] == "released" else "UNKNOWN"

    def reconcile(self, _lease, _identity, **kwargs):
        self.calls.append(("read", kwargs["status"]))
        row = next(row for row in self.rows.values() if row.attempt_id == _identity)
        row.status = "TERMINAL" if kwargs["status"] in {"filled", "cancelled", "rejected", "expired"} else "WORKING"
        self.calls.append(("observation", kwargs["observed_at"]))


def prepared(setup, monkeypatch, provider="webull", hour=14):
    entry_id, filled_at = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    # Attach Webull's real owned pair while RTH, before advancing to the STOP time.
    if provider == "webull":
        from project_mai_tai.oms.orb_country import reconcile_one
        asyncio.run(reconcile_one(service, entry_id))
    current = [filled_at.replace(hour=hour, minute=0)]
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        strategy = session.get(Strategy, entry.strategy_id)
        account = session.get(BrokerAccount, entry.broker_account_id)
        strategy.execution_mode, account.environment = "live", "production"
        session.get(TradeIntent, entry.intent_id).status = "filled"
        if provider == "schwab":
            account.name, account.provider = SCHWAB, "schwab"
            entry.payload = {**entry.payload, "orb_country_broker": "schwab"}
        lease = SimpleNamespace(command_id=uuid4(), strategy_id=strategy.id, generation=1,
            executor_id="controlled", fence=1, account_ids=(account.id,), scope_complete=True)
        target = owner._Target(entry.id, strategy.id, account.id, account.name, entry.symbol,
            entry.broker_order_id, entry.client_order_id, Decimal(10), Decimal(10), True)
    service.settings = ControlSettings(service.settings)
    # These controlled owner cases represent entries opened AFTER empty boot capture.
    service._bot_control_runtime = SimpleNamespace(boot_orb_entry_ids=frozenset())
    journal = Journal(lease)
    context = OrbStopContext(service, lease, journal, lambda: current[0])
    service._latest_quotes_by_symbol["ABC"] = {"bid": "5.647", "received_at": current[0]}
    original = broker.fetch_order_update

    async def details(request):
        if request.side == "buy":
            return ExecutionReport("filled", request.client_order_id, broker_order_id="entry-native",
                broker_fill_id="entry-execution", symbol="ABC", side="buy", intent_type="open",
                quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal("5.53"),
                reported_at=current[0], origin="broker")
        return await original(request)

    broker.fetch_order_update = details
    for suffix in ("T", "S"):
        broker.children[suffix] = ExecutionReport("cancelled", "rebound-exact-owned-child",
            broker_order_id=f"native-{suffix}", symbol="ABC", side="sell", intent_type="close",
            quantity=Decimal(10), filled_quantity=Decimal(0), reported_at=filled_at, origin="broker")

    async def release(account, parent):
        assert account == SCHWAB and parent == "entry-native"
        assert journal.rows["native"].status == "MAY_HAVE_SENT"
        broker.cancels.append((account, parent))
        return "released"

    broker.release_native_oco_for_close = release

    async def sync(*, account_names):
        from project_mai_tai.positions_read_receipt import record_receipts
        assert account_names == [target.account]
        current[0] += timedelta(milliseconds=1)
        with factory.begin() as session:
            row = session.scalar(select(AccountPosition).where(AccountPosition.broker_account_id == target.account_id))
            if row is None:
                row = AccountPosition(broker_account_id=target.account_id, symbol="ABC",
                                      quantity=Decimal(10), average_price=Decimal("5.53"))
                session.add(row)
            record_receipts(session, [(target.account, current[0], 1)], recorded_at=current[0])
        service._latest_quotes_by_symbol["ABC"]["received_at"] = current[0]

    service.sync_broker_positions = sync
    original_submit = broker.submit_order

    async def submit(request):
        if request.reason == owner.STOP_REASON and request.side == "sell":
            assert journal.rows["SUBMIT_EXIT"].status == "MAY_HAVE_SENT"
            assert journal.rows["native"].status == "TERMINAL"
        reports = await original_submit(request)
        return [replace(report, reported_at=current[0], origin="broker") for report in reports]

    broker.submit_order = submit
    return service, factory, broker, target, context, current


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("inventory", ["held", "unknown"])
def test_direct_stop_boot_guard_keeps_protection_and_normal_owner_untouched(setup, monkeypatch, provider, inventory):
    service, factory, broker, target, context, _ = prepared(setup, monkeypatch, provider)
    service._bot_control_runtime.boot_orb_entry_ids = (
        frozenset({str(target.entry_id)}) if inventory == "held" else None)
    with factory() as session:
        before = dict(session.get(BrokerOrder, target.entry_id).payload)
    for _ in range(2):
        assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context)) is False
        assert context.boot_blocked
    assert not broker.cancels and not context.journal.calls
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]
    assert len(service._orb_stop_boot_alerts) == 1
    with factory() as session:
        assert session.get(BrokerOrder, target.entry_id).payload == before
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].severity == "critical"
        assert incidents[0].payload["source"] == owner._STATE
        assert incidents[0].payload["command_id"] == str(context.lease.command_id)
        assert incidents[0].payload["entry_order_id"] == str(target.entry_id)
        if inventory == "held":
            assert incidents[0].title == "STOP: held across restart — sell manually or wait for normal exit"


@pytest.mark.parametrize("mode,name,provider,active,allowed", [
    ("live", WEBULL, "webull", True, True),
    ("paper", WEBULL, "webull", True, False),
    ("shadow", WEBULL, "webull", True, False),
    ("UNKNOWN", WEBULL, "webull", True, False),
    ("live", "paper:orb", "webull", True, False),
    ("live", "unknown:orb", "webull", True, False),
    ("live", WEBULL, "none", True, False),
    ("live", WEBULL, "webull", False, False),
])
def test_orb_claim_binds_strategy_mode_logical_account_not_production_environment(
    setup, monkeypatch, mode, name, provider, active, allowed,
):
    service, factory, broker, target, context, _ = prepared(setup, monkeypatch)
    with factory.begin() as session:
        strategy = session.get(Strategy, target.strategy_id)
        account = session.get(BrokerAccount, target.account_id)
        strategy.execution_mode = mode
        account.name, account.provider, account.is_active = name, provider, active
        assert account.environment == "production"
    bound = replace(target, account=name)
    if allowed:
        assert asyncio.run(context.claim(bound, uuid4())) is True
    else:
        with pytest.raises(ValueError, match="owner_not_live"):
            asyncio.run(context.claim(bound, uuid4()))
    assert not broker.cancels and not context.journal.calls
    assert not [request for request in broker.sent if request.reason == owner.STOP_REASON]


@pytest.mark.parametrize("strategy_mode,allowed", [("live", True), ("paper", False), ("shadow", False)])
def test_orb_native_snapshot_binds_real_strategy_mode_with_production_account(setup, monkeypatch, strategy_mode, allowed):
    from project_mai_tai.oms.orb_country import _snapshot
    service, factory, _, target, _, _ = prepared(setup, monkeypatch)
    with factory.begin() as session:
        session.get(Strategy, target.strategy_id).execution_mode = strategy_mode
    with factory() as session:
        assert session.get(BrokerAccount, target.account_id).environment == "production"
        if allowed:
            assert _snapshot(service, session, target.entry_id)[0].id == target.entry_id
        else:
            with pytest.raises(ValueError, match="identity_mismatch"):
                _snapshot(service, session, target.entry_id)


@pytest.mark.parametrize("held_at_boot", [False, True])
def test_real_runtime_frozen_boot_capture_distinguishes_postboot_entry(setup, monkeypatch, held_at_boot):
    from project_mai_tai.oms.bot_control_runtime import BotControlRuntime
    service, factory, _ = setup
    runtime = BotControlRuntime(factory)
    if not held_at_boot:
        asyncio.run(runtime.refresh())
        assert runtime.boot_orb_entry_ids == frozenset()
    service, factory, broker, target, context, _ = prepared(setup, monkeypatch)
    runtime.store.seed(target.strategy_id)
    asyncio.run(runtime.refresh())
    service._bot_control_runtime = runtime
    assert runtime.boot_orb_entry_ids == (frozenset({str(target.entry_id)}) if held_at_boot else frozenset())
    if held_at_boot:
        assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context)) is False
        assert not broker.cancels and not context.journal.calls
    else:
        assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context)) is True
        assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1
    frozen = runtime.boot_orb_entry_ids
    asyncio.run(runtime.refresh())
    assert runtime.boot_orb_entry_ids is frozen


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour,order_type,session", [(12, "limit", "AM"), (14, "market", "NORMAL"), (21, "limit", "PM")])
def test_stop_reuses_exact_orb_owner_both_brokers_sessions(setup, monkeypatch, provider, hour, order_type, session):
    service, factory, broker, target, context, current = prepared(setup, monkeypatch, provider, hour)
    assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context)) is True
    sells = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    assert len(sells) == 1
    request = sells[0]
    assert (request.strategy_code, request.broker_account_name, request.quantity) == ("orb_schwab", target.account, Decimal(10))
    assert request.order_type == order_type and request.metadata["session"] == session
    if order_type == "limit":
        assert request.metadata["limit_price"] == "5.64"
    assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context)) is False
    assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1
    with factory() as db:
        assert db.scalar(select(VirtualPosition)).quantity == 10  # Accepted is not a fill.


def test_stop_defers_without_db_or_second_wire_while_normal_owner_is_in_flight(setup, monkeypatch):
    service, _factory, broker, target, context, _current = prepared(setup, monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def normal_body(*_args, **_kwargs):
            entered.set()
            await release.wait()

        monkeypatch.setattr(owner, "_attempt_owned", normal_body)
        normal = asyncio.create_task(owner._attempt(service, target, uuid4(), context.clock))
        await entered.wait()
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("claim defer must occur before any DB access")
        monkeypatch.setattr(service, "_run_db", forbidden)
        assert await owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context) is False
        release.set()
        await normal

    asyncio.run(scenario())
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("reason", [owner.STOP_REASON, "ORB_END_OF_DAY_CLOSE"])
def test_contended_claim_mutex_refuses_before_sql_claim_or_owner(setup, monkeypatch, provider, reason):
    service, _, broker, target, context, _ = prepared(setup, monkeypatch, provider)
    mutex = Lock()
    mutex.acquire()

    class AdmissionMutex:
        def acquire(self, *, blocking):
            assert blocking is False
            return mutex.acquire(blocking=False)

        def release(self):
            raise AssertionError("non-acquirer must not release another owner's mutex")

        def __enter__(self):
            # Fail the old blocking implementation deterministically, rather than hang the host.
            raise AssertionError("blocking claim admission is forbidden")

        def __exit__(self, *_args):
            raise AssertionError("non-acquirer must not clean up another owner's mutex")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("contended admission must not reach SQL, claim or exit owner")

    service._orb_exit_claim_lock = AdmissionMutex()
    existing = (target.account, "OTHER", "other-episode")
    service._orb_exit_claims = {existing}
    monkeypatch.setattr(service, "_run_db", forbidden)
    monkeypatch.setattr(context, "claim", forbidden)
    monkeypatch.setattr(owner, "_attempt_owned", forbidden)
    try:
        assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=reason, stop_context=context if reason == owner.STOP_REASON else None)) is False
        assert mutex.locked() and service._orb_exit_claims == {existing}
        assert not context.journal.calls and not broker.cancels
        assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]
    finally:
        mutex.release()


@pytest.mark.parametrize("mutation", ["stale", "missing", "future", "nan", "before_open", "after_close"])
def test_stop_invalid_session_or_bid_keeps_protection(setup, monkeypatch, mutation):
    service, _factory, broker, target, context, current = prepared(setup, monkeypatch, hour=21)
    if mutation == "stale":
        service._latest_quotes_by_symbol["ABC"]["received_at"] -= timedelta(seconds=3)
    elif mutation == "missing":
        service._latest_quotes_by_symbol.clear()
    elif mutation == "future":
        service._latest_quotes_by_symbol["ABC"]["received_at"] += timedelta(seconds=1)
    elif mutation == "nan":
        service._latest_quotes_by_symbol["ABC"]["bid"] = "NaN"
    else:
        current[0] = current[0].replace(hour=7 if mutation == "before_open" else 0)
    with pytest.raises(ValueError):
        asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
    assert not broker.cancels
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]


def test_refused_stop_sell_never_retries_on_same_or_fresh_context(setup, monkeypatch):
    service, _factory, broker, target, context, current = prepared(setup, monkeypatch)
    original = broker.submit_order

    async def rejected(request):
        reports = await original(request)
        if request.reason == owner.STOP_REASON:
            return [replace(reports[0], event_type="rejected", reason="insufficient_available",
                            reported_at=current[0])]
        return reports

    broker.submit_order = rejected
    with pytest.raises(ValueError, match="close_rejected"):
        asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
    fresh = OrbStopContext(service, context.lease, context.journal, context.clock)
    assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=fresh)) is False
    assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1


def test_entry_cancel_ack_does_not_close_working_entry_or_repeat_cancel(setup, monkeypatch):
    service, factory, broker, target, context, current = prepared(setup, monkeypatch)
    with factory.begin() as session:
        entry = session.get(BrokerOrder, target.entry_id)
        entry.status = "accepted"
        entry.payload = {key: value for key, value in entry.payload.items() if key != "orbroute1_protection"}
        for fill in session.scalars(select(Fill).where(Fill.order_id == entry.id)).all():
            session.delete(fill)
        session.scalar(select(VirtualPosition)).quantity = Decimal(0)
    async def working(request):
        return ExecutionReport("accepted", request.client_order_id, broker_order_id="entry-native",
            symbol="ABC", side="buy", intent_type="open", quantity=Decimal(10),
            reported_at=current[0], origin="broker")
    broker.fetch_order_update = working
    for _ in range(2):
        asyncio.run(_cancel_entry(service, context.lease, context.journal, target.entry_id, context.clock))
    assert len([r for r in broker.sent if r.intent_type == "cancel"]) == 1
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON and r.side == "sell"]
    with factory() as session:
        assert session.get(BrokerOrder, target.entry_id).status == "accepted"
    assert context.journal.rows["CANCEL_ENTRY"].status == "WORKING"


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_stop_broker_ceiling_cannot_sell_more_than_owned_or_total(setup, monkeypatch, provider):
    service, factory, broker, target, context, _current = prepared(setup, monkeypatch, provider)
    with factory.begin() as session:
        session.add(AccountPosition(broker_account_id=target.account_id, symbol="ABC",
            quantity=Decimal(6), average_price=Decimal("5.53")))
    asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context))
    sells = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    assert len(sells) == 1 and sells[0].quantity == 6


def test_stop_prior_uncertain_normal_exit_is_not_retried_after_restart(setup, monkeypatch):
    service, factory, broker, target, context, _current = prepared(setup, monkeypatch)
    with factory.begin() as session:
        entry = session.get(BrokerOrder, target.entry_id)
        entry.payload = {**entry.payload, owner._STATE: {"phase": "submitting", "event_id": str(uuid4())}}
    assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context)) is False
    assert context.prior_unresolved is True
    assert not broker.cancels
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]


def test_filled_schwab_sibling_is_not_synthetic_whole_tree_terminal(setup, monkeypatch):
    service, _factory, broker, target, context, _current = prepared(setup, monkeypatch, "schwab")
    async def unresolved_sibling(*_args):
        broker.cancels.append("exact-parent-reader")
        return "resolved_by_fill"
    broker.release_native_oco_for_close = unresolved_sibling
    with pytest.raises(ValueError, match="remaining_tree_proof"):
        asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
    assert context.journal.rows["native"].status == "UNKNOWN"
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]


def test_stale_positions_receipt_never_grants_stop_sell(setup, monkeypatch):
    service, _factory, broker, target, context, _current = prepared(setup, monkeypatch)
    # Actual receipt helper remains unpatched. No successful OMS receipt exists.
    async def no_receipt(*, account_names):
        assert account_names == [target.account]
    service.sync_broker_positions = no_receipt
    with pytest.raises(ValueError, match="positions_wire_receipt_unknown"):
        asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]


def test_zero_vp_without_sell_fills_does_not_settle_stop(setup, monkeypatch):
    service, factory, _broker, _target, context, current = prepared(setup, monkeypatch)
    with factory.begin() as session:
        session.scalar(select(VirtualPosition)).quantity = Decimal(0)
    with factory() as session:
        with pytest.raises(ValueError, match="fill_conservation_unknown"):
            trusted_census(session, context.lease, context.journal, current[0])


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("foreign_vp", [False, True])
def test_stop_own_ten_leaves_manual_or_foreign_hundred_and_orders_untouched(setup, monkeypatch, provider, foreign_vp):
    service, factory, broker, target, context, current = prepared(setup, monkeypatch, provider)
    with factory.begin() as session:
        session.add(AccountPosition(broker_account_id=target.account_id, symbol="ABC",
            quantity=Decimal(110), average_price=Decimal("5.53")))
        foreign = service.store.ensure_strategy(session, "other_owner", name="other", execution_mode="live")
        if foreign_vp:
            session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=target.account_id,
                symbol="ABC", quantity=Decimal(100), average_price=Decimal(5)))
        from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
        event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
            strategy_code=foreign.code, broker_account_name=target.account, symbol="ABC",
            side="sell", intent_type="close", quantity=Decimal(100), reason="foreign-owner"))
        intent = service.store.create_trade_intent(session, strategy=foreign,
            broker_account=session.get(BrokerAccount, target.account_id), event=event)
        other_order = service.store.get_or_create_order(session, intent=intent, strategy_id=foreign.id,
            broker_account_id=target.account_id, client_order_id="foreign-hundred", broker_order_id="foreign-native",
            symbol="ABC", side="sell", quantity=Decimal(100), status="accepted", metadata={})
        other_id = other_order.id
    original_submit = broker.submit_order
    async def filled(request):
        reports = await original_submit(request)
        if request.reason == owner.STOP_REASON:
            return [replace(reports[0], event_type="filled", broker_fill_id="own-stop-ten",
                filled_quantity=Decimal(10), fill_price=Decimal(6), reported_at=current[0])]
        return reports
    broker.submit_order = filled
    # Do not patch the exact parent release, _snapshot, _probe, _phase or attribution.
    asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context))
    sells = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    assert len(sells) == 1 and sells[0].quantity == 10
    with factory() as session:
        assert session.scalar(select(AccountPosition)).quantity == 100
        assert session.scalar(select(VirtualPosition).where(VirtualPosition.strategy_id == target.strategy_id)).quantity == 0
        assert session.get(BrokerOrder, other_id).status == "accepted"
        if foreign_vp:
            assert session.scalar(select(VirtualPosition).where(VirtualPosition.strategy_id != target.strategy_id)).quantity == 100
    assert len(broker.cancels) == 1
    assert "foreign-native" not in str(broker.cancels)


def test_normal_webull_release_keeps_existing_foreign_vp_refusal(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import release_protection
    _service, factory, broker, target, context, _current = prepared(setup, monkeypatch)
    with factory.begin() as session:
        foreign = context.service.store.ensure_strategy(session, "other_owner", name="other", execution_mode="live")
        session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=target.account_id,
            symbol="ABC", quantity=Decimal(100), average_price=Decimal(5)))
    with pytest.raises(ValueError, match="foreign_owner"):
        asyncio.run(release_protection(context.service, target.entry_id))
    assert broker.cancels == []


def test_stop_context_foreign_lot_exception_rejects_changed_native_tree(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import release_protection
    service, factory, broker, target, context, _current = prepared(setup, monkeypatch)
    assert asyncio.run(context.claim(target, uuid4())) is True
    with factory.begin() as session:
        entry = session.get(BrokerOrder, target.entry_id)
        state = entry.payload["orbroute1_protection"]
        entry.payload = {**entry.payload, "orbroute1_protection": {**state, "base": "foreign-tree"}}
    with pytest.raises(ValueError, match="native_binding_changed"):
        asyncio.run(release_protection(service, target.entry_id, stop_context=context))
    assert not broker.cancels


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_accepted_stop_exit_later_fill_recovers_same_order_after_restart_no_sell(setup, monkeypatch, provider):
    service, factory, broker, target, context, current = prepared(setup, monkeypatch, provider)
    asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context))
    request, = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    with factory() as session:
        row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        order_id, native_id = row.id, row.broker_order_id
    original_detail = broker.fetch_order_update
    execution_at = current[0]
    current[0] += timedelta(seconds=30)
    async def filled(requested):
        if requested.client_order_id == request.client_order_id:
            return ExecutionReport("filled", request.client_order_id, broker_order_id=native_id,
                broker_fill_id="actual-stop-execution", symbol="ABC", side="sell", intent_type="close",
                quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal(6),
                reported_at=execution_at, origin="broker")
        return await original_detail(requested)
    broker.fetch_order_update = filled
    from project_mai_tai.oms.service import OmsRiskService
    restarted = OmsRiskService(settings=service.settings, redis_client=service.redis,
        session_factory=factory, broker_adapter=broker)
    attempt = context.journal.rows["SUBMIT_EXIT"]
    assert attempt.status == "WORKING"
    for _ in range(2):
        assert asyncio.run(recover_exit(restarted, context.lease, context.journal,
            order_id, attempt, context.clock)) == "PENDING_CENSUS"
    assert attempt.status == "TERMINAL"
    assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1
    with factory() as session:
        position = session.scalar(select(VirtualPosition))
        assert position.quantity == 0 and position.realized_pnl == Decimal("4.70")
        fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(fills) == 1 and fills[0].quantity == 10
        assert fills[0].filled_at.replace(tzinfo=execution_at.tzinfo) == execution_at
        assert session.get(BrokerOrder, target.entry_id).payload[owner._STATE]["phase"] == "resolved_by_stop_fill"
    assert ("observation", current[0]) in context.journal.calls


@pytest.mark.parametrize("changed", ["stale_receipt", "broker_decreased", "lease_lost", "resume_lag", "unchanged"])
def test_final_wire_proof_rechecked_after_journal_await_barrier(setup, monkeypatch, changed):
    service, factory, broker, target, context, current = prepared(setup, monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = context.before_submit

        async def paused(target, request):
            attempt = await original(target, request)
            entered.set()
            await release.wait()
            return attempt

        monkeypatch.setattr(context, "before_submit", paused)
        task = asyncio.create_task(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
        await entered.wait()
        assert context.journal.rows["SUBMIT_EXIT"].status == "MAY_HAVE_SENT"
        if changed == "stale_receipt":
            current[0] += timedelta(seconds=31)
        elif changed == "broker_decreased":
            with factory.begin() as session:
                session.scalar(select(AccountPosition)).quantity = Decimal(3)
        elif changed == "lease_lost":
            context.journal.active = False
        elif changed == "resume_lag":
            original_db = service._run_db
            async def delayed_return(work, **kwargs):
                result = await original_db(work, **kwargs)
                if work.__name__ == "validate":
                    current[0] += timedelta(seconds=31)
                return result
            monkeypatch.setattr(service, "_run_db", delayed_return)
        release.set()
        if changed == "unchanged":
            assert await task is True
        else:
            with pytest.raises(ValueError):
                await task

    asyncio.run(scenario())
    sells = [request for request in broker.sent if request.reason == owner.STOP_REASON]
    assert len(sells) == (1 if changed == "unchanged" else 0)
    if changed != "unchanged":
        assert context.journal.rows["SUBMIT_EXIT"].status == "MAY_HAVE_SENT"
        with factory() as session:
            state = session.get(BrokerOrder, target.entry_id).payload[owner._STATE]
            assert state["phase"] == "submitting"
            assert state["command_id"] == str(context.lease.command_id)


def prepared_real_journal(setup, monkeypatch, provider="webull"):
    from project_mai_tai.db import models
    from project_mai_tai.oms import bot_control
    from project_mai_tai.oms.bot_control import BotControlStore
    from project_mai_tai.oms.bot_control_journal import CleanupJournal
    service, factory, broker, target, _, current = prepared(setup, monkeypatch, provider)
    with factory.begin() as session:
        session.get(BrokerOrder, target.entry_id).submitted_at = current[0] - timedelta(minutes=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)
    monkeypatch.setattr(bot_control, "datetime", Clock)
    # Command.accepted_at's ORM default calls models.utcnow, not bot_control.datetime.
    # Freeze BOTH clocks before creating any control command; never backdate a persisted row.
    monkeypatch.setattr(models, "datetime", Clock)
    store = BotControlStore(factory)
    store.seed(target.strategy_id)
    command = uuid4()
    store.request(strategy_id=target.strategy_id, command_id=command, request_id=uuid4(),
        expected_generation=0, action="STOP", account_ids=(target.account_id,),
        actor_reference="controlled-local", reason="source-authored recovery control", scope_complete=True)
    lease = store.claim_stop(command, "controlled-local", now=current[0])
    assert lease is not None
    journal = CleanupJournal(store)
    context = OrbStopContext(service, lease, journal, lambda: current[0])

    async def submit(request):
        reports = await type(broker).submit_order(broker, request)
        return [replace(report, reported_at=current[0], origin="broker") for report in reports]

    async def release(account, parent):
        assert account == SCHWAB and parent == target.broker_id
        broker.cancels.append((account, parent))
        return "released"

    broker.submit_order, broker.release_native_oco_for_close = submit, release
    return service, factory, broker, target, context, current


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_boot_held_first_target_does_not_preempt_postboot_sell_or_mutate_normal_owner(setup, monkeypatch, provider):
    from project_mai_tai.oms.bot_control_orb import drive_orb_stop
    service, factory, _ = setup
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "orb_schwab", name="ORB", execution_mode="live")
        account = service.store.ensure_broker_account(session, WEBULL, provider="webull", environment="test")
        event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(strategy_code="orb_schwab",
            broker_account_name=WEBULL, symbol="OLD", side="buy", intent_type="open", quantity=Decimal(10), reason="old"))
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.status = "filled"
        old = service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="old-boot-entry", broker_order_id="old-boot-native",
            symbol="OLD", side="buy", quantity=Decimal(10), status="filled",
            metadata={"orb_country_route": "true", owner._STATE: {"phase": "checking", "reason": "ORB_END_OF_DAY_CLOSE"}})
        session.add(Fill(order_id=old.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="old-buy", symbol="OLD", side="buy", quantity=Decimal(10), price=Decimal(5), payload={}))
        # Insert the held VP BEFORE the new entry fixture, not by reordering the driver in the test.
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="OLD", quantity=Decimal(10), average_price=Decimal(5)))
        old_id, old_payload = old.id, dict(old.payload)
    service, factory, broker, target, context, _ = prepared_real_journal(setup, monkeypatch, provider)
    service._bot_control_runtime.boot_orb_entry_ids = frozenset({str(old_id)})
    result = asyncio.run(drive_orb_stop(service, context.lease, context.journal, clock=context.clock))
    assert result.phase == "BLOCKED_UNKNOWN"
    sells = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    assert len(sells) == 1 and sells[0].symbol == "ABC" and sells[0].quantity == 10
    assert "old-boot-native" not in str(broker.cancels)
    with factory() as session:
        assert session.get(BrokerOrder, old_id).payload == old_payload
        assert session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "OLD")).quantity == 10


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("new_episode", [False, True])
@pytest.mark.parametrize("damage", [None, "cash", "quantity", "journal_coid", "old_command", "missing_seal", "root_binding"])
def test_historical_settled_stop_is_exact_proven_history_not_latest_episode(setup, monkeypatch, provider, new_episode, damage):
    from project_mai_tai.db.models import BotControlAttempt, BotControlCommand
    from project_mai_tai.oms.bot_control_orb import _recover_known_exits, drive_orb_stop
    service, factory, broker, target, context, current = prepared_real_journal(setup, monkeypatch, provider)
    asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
        reason=owner.STOP_REASON, stop_context=context))
    request, = [r for r in broker.sent if r.reason == owner.STOP_REASON]
    with factory() as session:
        row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        order_id, native_id = row.id, row.broker_order_id
        attempt = session.scalar(select(BotControlAttempt).where(BotControlAttempt.target_order_id == row.id))
    async def filled(original):
        assert original.client_order_id == request.client_order_id
        return ExecutionReport("filled", request.client_order_id, broker_order_id=native_id,
            broker_fill_id="real-journal-stop-fill", symbol="ABC", side="sell", intent_type="close",
            quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal(6),
            reported_at=current[0], origin="broker")
    broker.fetch_order_update = filled
    assert asyncio.run(recover_exit(service, context.lease, context.journal,
        order_id, attempt, context.clock)) == "PENDING_CENSUS"
    with factory() as session:
        census = trusted_census(session, context.lease, context.journal, current[0])
    context.journal.store.settle_stop(context.lease.command_id, census=census, lease=context.lease)
    context.journal.store.request(strategy_id=target.strategy_id, command_id=uuid4(), request_id=uuid4(),
        expected_generation=1, action="ON", account_ids=(target.account_id,),
        actor_reference="controlled-local", reason="new run", scope_complete=True)
    next_target = None
    if new_episode:
        current[0] += timedelta(seconds=1)
        with factory.begin() as session:
            old = session.get(BrokerOrder, target.entry_id)
            event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
                    strategy_code="orb_schwab", broker_account_name=target.account, symbol="ABC",
                    side="buy", intent_type="open", quantity=Decimal(10), reason="new own episode"))
            intent = service.store.create_trade_intent(session, strategy=session.get(Strategy, target.strategy_id),
                broker_account=session.get(BrokerAccount, target.account_id), event=event)
            intent.status = "filled"
            new = service.store.get_or_create_order(session, intent=intent, strategy_id=target.strategy_id,
                broker_account_id=target.account_id, client_order_id="orb-new-episode", broker_order_id="orb-new-native",
                symbol="ABC", side="buy", quantity=Decimal(10), metadata={k: v for k, v in old.payload.items()
                    if k not in {owner._STATE, "orbroute1_protection"}}, status="filled")
            new.submitted_at = current[0]
            session.add(Fill(order_id=new.id, strategy_id=target.strategy_id, broker_account_id=target.account_id,
                broker_fill_id="new-own-buy", symbol="ABC", side="buy", quantity=Decimal(10), price=Decimal(7),
                filled_at=current[0], payload={}))
            session.scalar(select(VirtualPosition)).quantity = Decimal(10)
            next_target = replace(target, entry_id=new.id, client_id=new.client_order_id, broker_id=new.broker_order_id)
    command2 = uuid4()
    store = context.journal.store
    store.request(strategy_id=target.strategy_id, command_id=command2, request_id=uuid4(),
        expected_generation=2, action="STOP", account_ids=(target.account_id,),
        actor_reference="controlled-local", reason="STOP2", scope_complete=True)
    lease2 = store.claim_stop(command2, "controlled-local", now=current[0])
    assert lease2 is not None
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("settled historical exit must not read, cancel or resubmit")
    broker.fetch_order_update = broker.submit_order = broker.cancel_exit_pair = forbidden
    assert asyncio.run(_recover_known_exits(service, lease2, context.journal, context.clock)) is None
    if damage is not None:
        with factory.begin() as session:
            entry = session.get(BrokerOrder, target.entry_id)
            if damage in {"cash", "quantity"}:
                fill = session.scalar(select(Fill).where(Fill.broker_fill_id == "real-journal-stop-fill"))
                if damage == "cash":
                    fill.price += Decimal(1)
                else:
                    fill.quantity -= Decimal(1)
            elif damage == "journal_coid":
                session.get(BotControlAttempt, attempt.attempt_id).client_order_id = "foreign-exit"
            elif damage == "old_command":
                session.get(BotControlCommand, context.lease.command_id).phase = "BLOCKED_UNKNOWN"
            elif damage == "root_binding":
                entry.broker_order_id = "foreign-native-tree"
            else:
                state = dict(entry.payload[owner._STATE])
                state.pop("terminal_episode_proof")
                entry.payload = {**entry.payload, owner._STATE: state}
        assert asyncio.run(_recover_known_exits(service, lease2, context.journal, context.clock)) == "BLOCKED_UNKNOWN"
        assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1
        return
    if next_target is None:
        result = asyncio.run(drive_orb_stop(service, lease2, context.journal, clock=context.clock))
        assert result.phase == "SETTLED"
        store.settle_stop(command2, census=result.census, lease=lease2)
    else:
        fresh = OrbStopContext(service, lease2, context.journal, context.clock)
        assert asyncio.run(fresh.claim(next_target, uuid4())) is True
    assert len([r for r in broker.sent if r.reason == owner.STOP_REASON]) == 1


@pytest.mark.parametrize("stop_later", ["cancelled", "accepted", "unknown", "changed_binding"])
def test_flat_native_unknown_recovery_reads_same_tree_after_driver_restart_no_more_wires(setup, monkeypatch, stop_later):
    from project_mai_tai.db.models import BotControlAttempt
    from project_mai_tai.oms.bot_control_orb import drive_orb_stop
    service, factory, broker, target, context, current = prepared_real_journal(setup, monkeypatch)
    broker.children["T"] = replace(broker.children["T"], event_type="filled", filled_quantity=Decimal(10),
        fill_price=Decimal(6), broker_fill_id="native-target-execution")
    broker.children["S"] = replace(broker.children["S"], event_type="accepted")
    with pytest.raises(ValueError, match="release_unconfirmed"):
        asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context))
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert session.scalar(select(BotControlAttempt)).status == "UNKNOWN"
    before_cancel, before_sell = len(broker.cancels), len(broker.sent)
    assert asyncio.run(drive_orb_stop(service, context.lease, context.journal,
        clock=context.clock)).phase == "BLOCKED_UNKNOWN"
    if stop_later == "unknown":
        broker.children.pop("S")
    elif stop_later == "changed_binding":
        with factory.begin() as session:
            entry = session.get(BrokerOrder, target.entry_id)
            state = entry.payload["orbroute1_protection"]
            entry.payload = {**entry.payload, "orbroute1_protection": {**state, "base": "foreign-new-tree"}}
    else:
        broker.children["S"] = replace(broker.children["S"], event_type=stop_later)
    from project_mai_tai.oms.service import OmsRiskService
    restarted = OmsRiskService(settings=service.settings, redis_client=service.redis,
        session_factory=factory, broker_adapter=broker)
    # No old in-memory context/claim is reused; only the journal and original native binding survive.
    result = asyncio.run(drive_orb_stop(restarted, context.lease, context.journal, clock=context.clock))
    assert result.phase == ("SETTLED" if stop_later == "cancelled" else "BLOCKED_UNKNOWN")
    assert len(broker.cancels) == before_cancel == 1 and len(broker.sent) == before_sell
    assert not [r for r in broker.sent if r.reason == owner.STOP_REASON]
    with factory() as session:
        fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(fills) == 1 and fills[0].quantity == 10 and fills[0].price == 6
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert session.scalar(select(BotControlAttempt)).status == ("TERMINAL" if stop_later == "cancelled" else "UNKNOWN")


def flat_schwab_original_reader(setup, monkeypatch):
    service, factory, broker, target, context, current = prepared_real_journal(setup, monkeypatch, "schwab")
    assert asyncio.run(context.claim(target, uuid4())) is True
    attempt = asyncio.run(context.before_release(target))
    asyncio.run(context.after_release(attempt, "unanswerable"))

    async def original_child(account, symbol, coid, *, entry_broker_order_id):
        assert (account, symbol, coid, entry_broker_order_id) == (
            target.account, target.symbol, target.client_id, target.broker_id)
        return {"broker_order_id": "schwab-target-leg", "quantity": Decimal(10), "price": Decimal(6),
                "filled_at": current[0], "entry_broker_order_id": target.broker_id}
    broker.fetch_oco_exit_fill = original_child
    # Preserve the REAL original attribution/writer; do not manufacture a flat VP or SELL row.
    asyncio.run(service._poll_orb_schwab_child_exits(entry_id=target.entry_id))
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        assert sum(session.scalars(select(Fill.quantity).where(Fill.side == "sell")).all(), Decimal(0)) == 10
    return service, factory, broker, target, context, current


@pytest.mark.parametrize("shape", ["terminal", "working", "unsafe", "unknown", "missing", "raises"])
def test_flat_schwab_original_exact_tree_reader_shapes_never_cancel_or_sell(setup, monkeypatch, shape):
    from project_mai_tai.db.models import BotControlAttempt
    from project_mai_tai.oms.bot_control_orb import drive_orb_stop
    service, factory, broker, target, context, _ = flat_schwab_original_reader(setup, monkeypatch)
    reads = []

    async def exact_tree(account, root):
        assert (account, root) == (target.account, target.broker_id)
        reads.append((account, root))
        if shape == "raises":
            raise RuntimeError("controlled original reader unavailable")
        if shape == "unknown":
            return None
        if shape == "missing":
            return {"working": [], "filled": True}  # Missing unsafe is not positive proof.
        return {"working": ["schwab-stop-leg"] if shape == "working" else [],
                "filled": True, "unsafe": shape == "unsafe"}

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("flat native recovery must not cancel, release again or SELL")
    broker.fetch_exit_legs_for_entry = exact_tree
    broker.submit_order = broker.cancel_exit_pair = broker.release_native_oco_for_close = forbidden
    result = asyncio.run(drive_orb_stop(service, context.lease, context.journal, clock=context.clock))
    assert result.phase == ("SETTLED" if shape == "terminal" else "BLOCKED_UNKNOWN")
    assert reads == [(target.account, target.broker_id)] and not broker.cancels
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 0
        fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(fills) == 1 and fills[0].quantity == 10 and fills[0].price == 6
        assert session.scalar(select(BotControlAttempt)).status == ("TERMINAL" if shape == "terminal" else "UNKNOWN")


def test_flat_native_contended_mutex_defers_before_binding_sql_or_broker_read(setup, monkeypatch):
    from project_mai_tai.oms.bot_control_orb import _recover_flat_native
    service, _, broker, _, context, _ = flat_schwab_original_reader(setup, monkeypatch)
    actual = Lock()
    actual.acquire()
    operations = []

    class BusyMutex:
        def acquire(self, *, blocking):
            assert blocking is False
            return actual.acquire(blocking=False)
        def release(self):
            raise AssertionError("non-acquirer cannot release another mutex")
        def __enter__(self):
            raise AssertionError("flat-native admission must never queue")
        def __exit__(self, *_args):
            raise AssertionError("unacquired mutex must remain owned")

    async def guarded_db(operation, **kwargs):
        operations.append(operation.__name__)
        raise AssertionError("contended admission reached inventory/binding/write SQL")
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("contended recovery reached a provider boundary")
    service._orb_exit_claim_lock = BusyMutex()
    other = ("other-account", "OTHER", "other-episode")
    service._orb_exit_claims = {other}
    monkeypatch.setattr(service, "_run_db", guarded_db)
    broker.fetch_exit_legs_for_entry = broker.fetch_order_update = broker.submit_order = forbidden
    try:
        assert asyncio.run(_recover_flat_native(service, context.lease, context.journal,
            context.clock)) == "RECONCILING_PROTECTION"
        assert operations == [] and service._orb_exit_claims == {other} and actual.locked()
    finally:
        actual.release()


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_boot_block_incident_reaches_existing_orb_pager_source_without_owner_state_write(setup, monkeypatch, tmp_path, provider):
    import json
    from tests.unit.test_unexercised_watch_pager_hours_close import watch
    service, factory, broker, target, context, current = prepared(setup, monkeypatch, provider)
    service._bot_control_runtime.boot_orb_entry_ids = frozenset({str(target.entry_id)})
    with factory() as session:
        before = dict(session.get(BrokerOrder, target.entry_id).payload)
    for _ in range(2):
        assert asyncio.run(owner._attempt(service, target, uuid4(), context.clock,
            reason=owner.STOP_REASON, stop_context=context)) is False
    with factory() as session:
        incident, = session.scalars(select(SystemIncident)).all()
        assert incident.severity == "critical" and incident.status == "open"
        assert incident.title == "STOP: held across restart — sell manually or wait for normal exit"
        assert session.get(BrokerOrder, target.entry_id).payload == before
        payload = dict(incident.payload)
        projection = {"id": str(incident.id), "title": incident.title, "opened_at": current[0].isoformat(),
            "account": payload["broker_account_name"], "symbol": payload["symbol"],
            "entry_order_id": payload["entry_order_id"], "source": payload["source"], "reason": payload["reason"]}
    def incident_sql(query):
        assert "from system_incidents where status != 'closed'" in query
        assert "'orb_schwab_eod_close'" in query and projection["source"] == "orb_schwab_eod_close"
        return [json.dumps(projection)]
    delivered = []
    monkeypatch.setattr(watch, "_psql", incident_sql)
    monkeypatch.setattr(watch, "page", lambda title, body: delivered.append((title, body)) or True)
    for _ in range(2):
        assert watch._run_inc1_pager_unlocked(state_path=tmp_path / "pager-state.json",
            status_path=tmp_path / "pager-status.txt", no_page=False, now=current[0]) == 0
    assert len(delivered) == 1 and delivered[0][0] == incident.title
    assert target.account in delivered[0][1] and str(target.entry_id) in delivered[0][1]
    assert payload["reason"] in delivered[0][1]
    assert not broker.cancels and not context.journal.calls
