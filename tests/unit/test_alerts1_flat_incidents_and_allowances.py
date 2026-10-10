from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    Fill,
    OmsManagedPosition,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.oms import service as oms_service
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.positions_read_receipt import (
    QUEUE_LIMIT,
    SNAPSHOT_TYPE,
    PositionsReadReceiptWriter,
    load_receipts,
    record_receipts,
)
from project_mai_tai.reconciliation.flat_incidents import resolve_flat_exposure_incidents
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory

NOW = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)


FRESH = object()


def _receipt(session, account_name, read_at):
    record_receipts(session, [(account_name, read_at, 0)], recorded_at=read_at)


def _accounts(session, *, schwab_stamp=FRESH, webull_stamp=FRESH, now=NOW):
    """Both live accounts, each with a positions-read receipt as the OMS sync writes it.

    A stamp of None means "no receipt" (no successful read was ever recorded) for that account.
    """
    strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
    schwab = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="production")
    webull = BrokerAccount(name="live:orb", provider="webull", environment="production")
    session.add_all([strategy, schwab, webull])
    session.flush()
    for account, stamp in ((schwab, schwab_stamp), (webull, webull_stamp)):
        if stamp is None:
            continue
        _receipt(session, account.name, now - timedelta(seconds=5) if stamp is FRESH else stamp)
    return strategy, schwab, webull


def _refused_incident(symbol="RETO", session_date="2026-10-05", opened_at=None, status="open"):
    return SystemIncident(
        service_name="oms-risk",
        severity="critical",
        title=f"SCHWAB OPEN REFUSED: {symbol} on live:schwab_1m_v2; check Webull-only exposure",
        status=status,
        payload={
            "source": "schwab_opening_policy_reject",
            "broker_account_name": "live:schwab_1m_v2",
            "symbol": symbol,
            "session_date": session_date,
            "client_order_id": f"schwab_1m_v2-{symbol}-open-x",
            "reason": "Opening transactions for this security must be placed with a broker.",
        },
        opened_at=opened_at or datetime(2026, 10, 5, 15, 21, tzinfo=UTC),
    )


def _orb_incident(symbol="MI"):
    return SystemIncident(
        service_name="orb-schwab",
        severity="critical",
        status="open",
        title=f"ORB strategy-exit evidence unavailable: {symbol}",
        payload={
            "source": "orb_schwab_exit_evidence",
            "entry_order_id": "2ade5ec6-a660-4a46-b4ee-d46fe354d80f",
            "broker_account_name": "live:schwab_1m_v2",
            "symbol": symbol,
        },
        opened_at=datetime(2026, 10, 5, 13, 37, tzinfo=UTC),
    )


def _run(session_factory, *incidents, seed=None, **account_kwargs):
    with session_factory() as session:
        strategy, schwab, webull = _accounts(session, **account_kwargs)
        session.add_all(list(incidents))
        if seed is not None:
            seed(session, strategy, schwab, webull)
        session.commit()
        ids = [incident.id for incident in incidents]
    with session_factory() as session:
        resolved = resolve_flat_exposure_incidents(session, now=NOW)
        session.commit()
    with session_factory() as session:
        rows = [session.get(SystemIncident, incident_id) for incident_id in ids]
    return resolved, rows


def test_flat_symbol_resolves_both_incident_kinds_with_a_reason() -> None:
    resolved, rows = _run(build_test_session_factory(), _refused_incident(), _orb_incident())
    assert len(resolved) == 2
    for row in rows:
        assert row.status == "closed"
        assert row.closed_at is not None
        assert row.payload["resolution"]["reason"] == "auto_resolved_flat_both_brokers"
        assert row.payload["resolution"]["checked_accounts"] == ["live:schwab_1m_v2", "live:orb"]
        evidence = row.payload["resolution"]["position_evidence"]  # receipts
        assert evidence["live:schwab_1m_v2"]["ok"] and evidence["live:orb"]["ok"]
        assert row.payload["source"] in {"schwab_opening_policy_reject", "orb_schwab_exit_evidence"}


def test_webull_only_position_keeps_the_incident_open() -> None:
    def seed(session, strategy, schwab, webull):
        session.add(
            AccountPosition(
                broker_account_id=webull.id,
                symbol="RETO",
                quantity=Decimal("90"),
                average_price=Decimal("1.5"),
                market_value=Decimal("135"),
                source_updated_at=NOW,
            )
        )

    resolved, rows = _run(build_test_session_factory(), _refused_incident(), seed=seed)
    assert resolved == []
    assert rows[0].status == "open"
    assert "resolution" not in rows[0].payload


@pytest.mark.parametrize("status", ["submitted", "accepted", "partially_filled", "pending", "weird"])
def test_working_order_keeps_the_incident_open(status) -> None:
    def seed(session, strategy, schwab, webull):
        session.add(
            BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=webull.id,
                client_order_id="schwab_1m_v2-RETO-open-webull",
                symbol="RETO",
                side="buy",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal("90"),
                status=status,
                payload={},
                submitted_at=NOW,
                updated_at=NOW,
            )
        )

    resolved, rows = _run(build_test_session_factory(), _refused_incident(), seed=seed)
    assert resolved == []
    assert rows[0].status == "open"


def test_live_book_rows_and_active_intent_keep_the_incident_open() -> None:
    def virtual(session, strategy, schwab, webull):
        session.add(
            VirtualPosition(
                strategy_id=strategy.id,
                broker_account_id=webull.id,
                symbol="RETO",
                quantity=Decimal("90"),
                average_price=Decimal("1.5"),
                realized_pnl=Decimal("0"),
                opened_at=NOW,
            )
        )

    def managed(session, strategy, schwab, webull):
        session.add(
            OmsManagedPosition(
                strategy_code="schwab_1m_v2",
                broker_account_name="live:orb",
                symbol="RETO",
                entry_price=Decimal("1.5"),
                original_quantity=90,
                current_quantity=90,
                entry_time=NOW,
                current_profit_pct=Decimal("0"),
                peak_profit_pct=Decimal("0"),
                status="open",
            )
        )

    def intent(session, strategy, schwab, webull):
        session.add(
            TradeIntent(
                strategy_id=strategy.id,
                broker_account_id=webull.id,
                symbol="RETO",
                side="buy",
                intent_type="open",
                quantity=Decimal("90"),
                reason="x",
                status="pending",
                payload={},
            )
        )

    for seed in (virtual, managed, intent):
        resolved, rows = _run(build_test_session_factory(), _refused_incident(), seed=seed)
        assert resolved == [], seed.__name__
        assert rows[0].status == "open", seed.__name__


def test_terminal_orders_and_other_symbols_do_not_block() -> None:
    def seed(session, strategy, schwab, webull):
        for index, status in enumerate(("filled", "cancelled", "rejected", "aborted")):
            session.add(
                BrokerOrder(
                    strategy_id=strategy.id,
                    broker_account_id=webull.id,
                    client_order_id=f"done-{index}",
                    symbol="RETO",
                    side="buy",
                    order_type="limit",
                    time_in_force="day",
                    quantity=Decimal("1"),
                    status=status,
                    payload={},
                    submitted_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add(
            AccountPosition(
                broker_account_id=webull.id,
                symbol="OTHER",
                quantity=Decimal("5"),
                average_price=Decimal("1"),
                market_value=Decimal("5"),
                source_updated_at=NOW,
            )
        )

    resolved, rows = _run(build_test_session_factory(), _refused_incident(), seed=seed)
    assert len(resolved) == 1
    assert rows[0].status == "closed"


def test_same_day_incident_waits_for_the_webull_leg_window() -> None:
    young = _refused_incident(
        symbol="INHD", session_date="2026-10-09", opened_at=NOW - timedelta(minutes=5)
    )
    aged = _refused_incident(
        symbol="AIXI", session_date="2026-10-09", opened_at=NOW - timedelta(minutes=45)
    )
    resolved, rows = _run(build_test_session_factory(), young, aged)
    assert [row.status for row in rows] == ["open", "closed"]
    assert len(resolved) == 1


def test_other_incident_sources_and_closed_rows_are_untouched() -> None:
    other = SystemIncident(
        service_name="oms-risk",
        severity="critical",
        status="open",
        title="UNCOVERED: BENF on live:orb has NO broker stop (31s); check now",
        payload={"source": "oms_v2_webull_uncovered_share", "symbol": "BENF"},
        opened_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    already = _refused_incident(status="closed")
    resolved, rows = _run(build_test_session_factory(), other, already)
    assert resolved == []
    assert rows[0].status == "open"
    assert "resolution" not in rows[1].payload


def test_missing_live_account_never_counts_as_flat() -> None:
    session_factory = build_test_session_factory()
    with session_factory() as session:
        session.add(BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="x"))
        session.add(_refused_incident())
        session.commit()
    with session_factory() as session:
        assert resolve_flat_exposure_incidents(session, now=NOW) == []


def test_reconciler_cycle_resolves_and_flag_off_leaves_it_open() -> None:
    for enabled, expected in ((True, "closed"), (False, "open")):
        session_factory = build_test_session_factory()
        with session_factory() as session:
            _accounts(session, now=datetime.now(UTC))
            session.add(_refused_incident())
            session.commit()
        settings = Settings(reconciliation_auto_resolve_flat_exposure_incidents=enabled)
        ReconciliationService(
            settings=settings, redis_client=FakeRedis(), session_factory=session_factory
        ).run_reconciliation_cycle()
        with session_factory() as session:
            incident = session.scalar(
                select(SystemIncident).where(SystemIncident.service_name == "oms-risk")
            )
            assert incident.status == expected


# ------------------------------------------- A1 evidence gate (Codex #1147 review, P1)


def test_missing_schwab_receipt_keeps_the_incident_open() -> None:
    """Codex repro #1: both accounts configured, ZERO account_positions rows, no read receipt."""
    resolved, rows = _run(build_test_session_factory(), _refused_incident(), schwab_stamp=None)
    assert resolved == []
    assert rows[0].status == "open"
    resolved, rows = _run(
        build_test_session_factory(), _refused_incident(), schwab_stamp=None, webull_stamp=None
    )
    assert resolved == []
    assert rows[0].status == "open"


def test_stale_webull_receipt_keeps_the_incident_open() -> None:
    resolved, rows = _run(
        build_test_session_factory(),
        _refused_incident(),
        webull_stamp=NOW - timedelta(minutes=10),
    )
    assert resolved == []
    assert rows[0].status == "open"


def test_fill_only_fresh_stamps_without_a_sync_keep_the_incident_open() -> None:
    """Codex repro #2 (16e41c75): old snapshot rows, then ONLY _apply_position_fill(sell) makes
    them zero with fresh source_updated_at; no positions sync ran. Must stay OPEN."""

    def seed(session, strategy, schwab, webull):
        store = OmsStore()
        for account in (schwab, webull):
            row = AccountPosition(
                broker_account_id=account.id,
                symbol="SOLD",
                quantity=Decimal("5"),
                average_price=Decimal("1"),
                market_value=Decimal("5"),
                source_updated_at=NOW - timedelta(minutes=10),
            )
            session.add(row)
            session.flush()
            store._apply_position_fill(
                quantity=Decimal("5"),
                price=Decimal("1"),
                side="sell",
                position=row,
                track_realized_pnl=False,
                reported_at=NOW,
            )
            assert row.quantity == 0 and row.source_updated_at == NOW

    for stamp in (NOW - timedelta(minutes=10), None):
        resolved, rows = _run(
            build_test_session_factory(),
            _refused_incident(),
            seed=seed,
            schwab_stamp=stamp,
            webull_stamp=stamp,
        )
        assert resolved == [], stamp
        assert rows[0].status == "open", stamp


def test_unparseable_receipt_keeps_the_incident_open() -> None:
    def seed(session, strategy, schwab, webull):
        for row in session.scalars(
            select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)
        ).all():
            if row.payload["broker_account_name"] == "live:orb":
                row.payload = {**row.payload, "read_at": "garbage"}

    resolved, rows = _run(build_test_session_factory(), _refused_incident(), seed=seed)
    assert resolved == []
    assert rows[0].status == "open"


def test_evidence_read_error_keeps_the_incident_open(monkeypatch, caplog) -> None:
    from project_mai_tai.reconciliation import flat_incidents

    session_factory = build_test_session_factory()
    with session_factory() as session:
        _accounts(session)
        incident = _refused_incident()
        session.add(incident)
        session.commit()
        incident_id = incident.id

    def failing_load(session):
        raise RuntimeError("receipt read failed")

    monkeypatch.setattr(flat_incidents, "load_receipts", failing_load)
    with caplog.at_level("INFO", logger="reconciler"), session_factory() as session:
        assert flat_incidents.resolve_flat_exposure_incidents(session, now=NOW) == []
        session.commit()
    assert "evidence_read_error" in caplog.text
    with session_factory() as session:
        assert session.get(SystemIncident, incident_id).status == "open"


def test_both_receipts_fresh_and_flat_closes() -> None:
    resolved, rows = _run(
        build_test_session_factory(),
        _refused_incident(),
        schwab_stamp=NOW - timedelta(seconds=119),
        webull_stamp=NOW - timedelta(seconds=3),
    )
    assert len(resolved) == 1
    assert rows[0].status == "closed"


# ------------------------------------------- OMS sync pass writes the receipt (and only there)

SCHWAB_ID, WEBULL_ID = uuid4(), uuid4()


class _SyncStore:
    def list_active_broker_accounts(self, session):
        return [
            SimpleNamespace(id=SCHWAB_ID, name="live:schwab_1m_v2"),
            SimpleNamespace(id=WEBULL_ID, name="live:orb"),
        ]

    def list_named_broker_accounts(self, session, names):
        return [a for a in self.list_active_broker_accounts(session) if a.name in names]

    def sync_account_positions(self, session, *, broker_account_id, snapshots):
        return len(snapshots)

    def clear_virtual_positions_without_account_backing(self, session, **kwargs):
        return []

    def restore_virtual_positions_from_managed(self, session, **kwargs):
        return []


class _SyncAdapter:
    def __init__(self, *, failing=(), wire_age=None):
        self.failing = set(failing)
        self.wire_age = dict(wire_age or {})

    async def list_account_positions(self, name):
        if name in self.failing:
            raise RuntimeError("positions read failed / incomplete")
        return [SimpleNamespace(symbol="X", quantity=1)]

    def positions_wire_read_age_seconds(self, name):
        return self.wire_age.get(name)


def _oms(session_factory, adapter, *, writer=FRESH, persist_fails=False):
    s = object.__new__(oms_service.OmsRiskService)
    s.store = _SyncStore()
    s.broker_adapter = adapter
    s.logger = logging.getLogger("test-alerts1-receipt")
    s.settings = SimpleNamespace()
    if writer is FRESH:
        writer = PositionsReadReceiptWriter(session_factory)
    if writer is not None:
        s._positions_read_receipt_writer = writer

    async def _run_db(fn, *, commit=True):
        with session_factory() as session:
            result = fn(session)
            if persist_fails:
                raise RuntimeError("sync save failed")
            if commit:
                session.commit()
            return result

    s._run_db = _run_db
    s._observe_settlement = lambda *a, **k: None
    return s


def _sync_and_drain(oms):
    """One sync pass, then let the writer drain exactly what was offered (as its worker would)."""

    async def go():
        result = await oms.sync_broker_positions()
        writer = oms._positions_read_receipt_writer
        worker = asyncio.create_task(writer.run())
        await writer.queue.join()
        worker.cancel()
        return result

    return asyncio.run(go())


def _receipts(session_factory):
    with session_factory() as session:
        return load_receipts(session)


def test_successful_sync_writes_one_receipt_per_account_and_upserts() -> None:
    session_factory = build_test_session_factory()
    oms = _oms(session_factory, _SyncAdapter())
    _sync_and_drain(oms)
    first = _receipts(session_factory)
    # asyncio.Queue binds to its event loop; production has one loop, each asyncio.run is new.
    oms._positions_read_receipt_writer = PositionsReadReceiptWriter(session_factory)
    _sync_and_drain(oms)
    second = _receipts(session_factory)
    assert set(second) == {"live:schwab_1m_v2", "live:orb"}
    assert second["live:orb"]["read_at"] >= first["live:orb"]["read_at"]
    assert second["live:orb"]["writer"] == "oms.sync_broker_positions"
    with session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE
            )
        )
    assert count == 2, "one upserted row per account, not one per pass"


def test_partial_read_writes_no_receipt_for_the_failed_account() -> None:
    session_factory = build_test_session_factory()
    _sync_and_drain(_oms(session_factory, _SyncAdapter(failing={"live:orb"})))
    assert set(_receipts(session_factory)) == {"live:schwab_1m_v2"}


def test_all_reads_failed_offers_nothing() -> None:
    session_factory = build_test_session_factory()
    oms = _oms(session_factory, _SyncAdapter(failing={"live:orb", "live:schwab_1m_v2"}))
    asyncio.run(oms.sync_broker_positions())
    assert oms._positions_read_receipt_writer.queue.empty()
    assert _receipts(session_factory) == {}


def test_failed_sync_save_offers_no_receipt() -> None:
    """A receipt requires the read AND the snapshot save to have committed."""
    session_factory = build_test_session_factory()
    oms = _oms(session_factory, _SyncAdapter(), persist_fails=True)
    with pytest.raises(RuntimeError):
        asyncio.run(oms.sync_broker_positions())
    assert oms._positions_read_receipt_writer.queue.empty()


def test_sync_pass_does_not_await_the_writer() -> None:
    """No worker running and a writer that would block: the sync pass still returns at once,
    the receipt sits in the bounded queue, and nothing was written in the sync transaction."""
    session_factory = build_test_session_factory()
    writer = PositionsReadReceiptWriter(session_factory)
    writer._persist = lambda batches: time.sleep(30)  # would block if it were awaited
    oms = _oms(session_factory, _SyncAdapter(), writer=writer)
    started = time.monotonic()
    summary = asyncio.run(oms.sync_broker_positions())
    assert time.monotonic() - started < 2.0
    assert summary == {"accounts": 2, "positions": 2}
    assert writer.queue.qsize() == 1
    assert _receipts(session_factory) == {}, "receipt must not ride the sync-pass transaction"


def test_writer_failure_does_not_affect_the_sync_pass(caplog) -> None:
    session_factory = build_test_session_factory()

    class _ExplodingWriter:
        def offer(self, receipts):
            raise RuntimeError("writer exploded")

    oms = _oms(session_factory, _SyncAdapter(), writer=_ExplodingWriter())
    assert asyncio.run(oms.sync_broker_positions()) == {"accounts": 2, "positions": 2}

    failing = PositionsReadReceiptWriter(session_factory)

    def boom(batches):
        raise RuntimeError("db down")

    failing._persist = boom
    oms = _oms(session_factory, _SyncAdapter(), writer=failing)
    with caplog.at_level("WARNING", logger="oms-risk"):
        assert _sync_and_drain(oms) == {"accounts": 2, "positions": 2}
    assert "write_failed" in caplog.text
    assert _receipts(session_factory) == {}


def test_full_queue_drops_and_logs_without_blocking(caplog) -> None:
    writer = PositionsReadReceiptWriter(None)

    async def go():
        with caplog.at_level("WARNING", logger="oms-risk"):
            results = [writer.offer([("live:orb", NOW, 0)]) for _ in range(QUEUE_LIMIT + 3)]
        return results

    results = asyncio.run(go())
    assert results.count(True) == QUEUE_LIMIT and results.count(False) == 3
    assert "queue_full" in caplog.text


def test_failed_sync_after_an_old_good_receipt_retains_it_and_incident_stays_open() -> None:
    session_factory = build_test_session_factory()
    old = datetime.now(UTC) - timedelta(minutes=10)
    with session_factory() as session:
        _accounts(session, schwab_stamp=old, webull_stamp=old, now=datetime.now(UTC))
        session.add(_refused_incident())
        session.commit()
    _sync_and_drain(_oms(session_factory, _SyncAdapter(failing={"live:orb"})))
    receipts = _receipts(session_factory)
    assert receipts["live:orb"]["read_at"] == old, "a failed read must leave the receipt unchanged"
    assert datetime.now(UTC) - receipts["live:schwab_1m_v2"]["read_at"] < timedelta(seconds=30)
    with session_factory() as session:
        assert resolve_flat_exposure_incidents(session, now=datetime.now(UTC)) == []


def test_cached_webull_snapshot_carries_its_real_read_time() -> None:
    """Webull throttle/429 backoff serves a cache; the receipt must not look fresh."""
    session_factory = build_test_session_factory()
    _sync_and_drain(_oms(session_factory, _SyncAdapter(wire_age={"live:orb": 600.0})))
    age = datetime.now(UTC) - _receipts(session_factory)["live:orb"]["read_at"]
    assert age >= timedelta(seconds=599)


def test_webull_adapter_reports_the_age_of_its_last_real_read() -> None:
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    adapter = object.__new__(WebullBrokerAdapter)
    adapter._positions_lock = threading.Lock()
    adapter._positions_cache = {}
    assert adapter.positions_wire_read_age_seconds("live:orb") is None
    adapter._positions_cache["live:orb"] = (time.monotonic() - 42.0, [])
    assert 41.0 <= adapter.positions_wire_read_age_seconds("live:orb") < 60.0


# ---------------------------------------------------------------- A2: operator-closed allowance


def _position_findings(*, symbol, fills, account_name="live:schwab_1m_v2", broker_qty=0):
    """fills: list of (side, qty, filled_at)."""
    session_factory = build_test_session_factory()
    with session_factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
        account = BrokerAccount(name=account_name, provider="schwab", environment="production")
        session.add_all([strategy, account])
        session.flush()
        for index, (side, qty, filled_at) in enumerate(fills):
            order = BrokerOrder(
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"{symbol}-{index}",
                symbol=symbol,
                side=side,
                order_type="limit",
                time_in_force="day",
                quantity=Decimal(str(qty)),
                status="filled",
                payload={},
                submitted_at=filled_at,
                updated_at=filled_at,
            )
            session.add(order)
            session.flush()
            session.add(
                Fill(
                    order_id=order.id,
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    broker_fill_id=f"{symbol}-fill-{index}",
                    symbol=symbol,
                    side=side,
                    quantity=Decimal(str(qty)),
                    price=Decimal("3"),
                    filled_at=filled_at,
                    payload={},
                )
            )
        if broker_qty:
            session.add(
                AccountPosition(
                    broker_account_id=account.id,
                    symbol=symbol,
                    quantity=Decimal(str(broker_qty)),
                    average_price=Decimal("3"),
                    market_value=Decimal("3"),
                    source_updated_at=NOW,
                )
            )
        session.commit()
    service = ReconciliationService(
        settings=Settings(), redis_client=FakeRedis(), session_factory=session_factory
    )
    with session_factory() as session:
        return [
            f
            for f in service._build_position_findings(session)
            if f.finding_type == "position_quantity_mismatch"
        ]


MI_FILL = ("buy", 180, datetime(2026, 10, 5, 13, 34, 24, tzinfo=UTC))
NXL_FILLS = [
    ("buy", 2, datetime(2026, 10, 1, 13, 16, 50, tzinfo=UTC)),
    ("sell", 2, datetime(2026, 10, 1, 13, 22, 23, tzinfo=UTC)),
    ("buy", 2, datetime(2026, 10, 1, 17, 46, 30, tzinfo=UTC)),
]


def test_mi_and_nxl_ruled_items_are_info_not_critical() -> None:
    for symbol, fills, allowance_id in (
        ("MI", [MI_FILL], "ALERTS1-MI-20261005"),
        ("NXL", NXL_FILLS, "ALERTS1-NXL-20261001"),
    ):
        findings = _position_findings(symbol=symbol, fills=fills)
        assert len(findings) == 1, symbol
        finding = findings[0]
        assert finding.severity == "info", symbol
        assert finding.payload["direction"] == "broker_missing_owned_position"
        assert finding.payload["operator_allowance"]["allowance_id"] == allowance_id
        assert "operator-closed" in finding.title


@pytest.mark.parametrize(
    ("symbol", "fills", "account_name", "broker_qty"),
    [
        # same shape, a different symbol
        ("ZZZ", [MI_FILL], "live:schwab_1m_v2", 0),
        # MI on a different account
        ("MI", [MI_FILL], "live:orb", 0),
        # MI with a different quantity
        ("MI", [("buy", 182, MI_FILL[2])], "live:schwab_1m_v2", 0),
        # MI with a new fill on a later date (new mismatch)
        ("MI", [MI_FILL, ("buy", 2, datetime(2026, 10, 9, 14, 0, tzinfo=UTC)),
                ("sell", 2, datetime(2026, 10, 9, 14, 5, tzinfo=UTC))], "live:schwab_1m_v2", 0),
        # NXL with a different quantity
        ("NXL", NXL_FILLS[:2] + [("buy", 4, NXL_FILLS[2][2])], "live:schwab_1m_v2", 0),
        # NXL now has a broker position -> a different finding entirely
        ("NXL", NXL_FILLS, "live:schwab_1m_v2", 5),
    ],
)
def test_any_other_or_new_mismatch_stays_critical(symbol, fills, account_name, broker_qty) -> None:
    findings = _position_findings(
        symbol=symbol, fills=fills, account_name=account_name, broker_qty=broker_qty
    )
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].payload["operator_allowance"] is None
