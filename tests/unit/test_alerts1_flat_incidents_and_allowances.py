from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    OmsManagedPosition,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.reconciliation.flat_incidents import resolve_flat_exposure_incidents
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory

NOW = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)


def _accounts(session):
    strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
    schwab = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="production")
    webull = BrokerAccount(name="live:orb", provider="webull", environment="production")
    session.add_all([strategy, schwab, webull])
    session.flush()
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


def _run(session_factory, *incidents, seed=None):
    with session_factory() as session:
        strategy, schwab, webull = _accounts(session)
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
            _accounts(session)
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
