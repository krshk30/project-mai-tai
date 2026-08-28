"""Differential acceptance-query controls against a real PostgreSQL server.

A textual guard has to enumerate every possible spelling of a window leak.  A
differential database control asks the runtime property directly: the same
seeded evidence must produce different verdicts when it is inside versus
outside the requested window.  The SQL always comes from the acceptance
module's live ``SQL`` constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import importlib.util
import os
import sys
from types import ModuleType
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    Fill,
    Strategy,
    TradeIntent,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from tests.integration.acceptance_sql_harness import (
    AcceptanceSqlCase,
    PostgresAcceptanceHarness,
    PostgresHarnessError,
    SeedRow,
    SqlWindow,
    repository_root,
)


ROOT = repository_root()
LIVE_WINDOW = SqlWindow(
    datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
)
EMPTY_WINDOW = SqlWindow(
    datetime(2026, 8, 29, 13, 0, tzinfo=UTC),
    datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
)
INSIDE_AT = LIVE_WINDOW.since + timedelta(minutes=30)
OUTSIDE_AT = EMPTY_WINDOW.until + timedelta(days=1)

STRATEGY_ID = UUID("10000000-0000-0000-0000-000000000001")
WEBULL_ID = UUID("20000000-0000-0000-0000-000000000001")
SCHWAB_ID = UUID("20000000-0000-0000-0000-000000000002")
BUY_ORDER_ID = UUID("30000000-0000-0000-0000-000000000001")
SELL_INSIDE_ID = UUID("30000000-0000-0000-0000-000000000002")
SELL_OUTSIDE_ID = UUID("30000000-0000-0000-0000-000000000003")
INTENT_INSIDE_ID = UUID("40000000-0000-0000-0000-000000000001")
INTENT_OUTSIDE_ID = UUID("40000000-0000-0000-0000-000000000002")


def _id(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"project-mai-tai/acceptance-sql/{label}")


def _order_row(
    *,
    label: str,
    account_id: UUID,
    symbol: str,
    side: str,
    at: datetime,
    status: str,
    payload: dict[str, object],
    order_type: str = "STOP_LIMIT",
    intent_id: UUID | None = None,
) -> SeedRow:
    order_id = _id(f"order/{label}")
    return SeedRow(
        BrokerOrder.__table__,
        {
            "id": order_id,
            "intent_id": intent_id,
            "strategy_id": STRATEGY_ID,
            "broker_account_id": account_id,
            "client_order_id": f"sql-harness-{order_id}",
            "broker_order_id": f"broker-{order_id}",
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "time_in_force": "DAY",
            "quantity": Decimal("1"),
            "status": status,
            "payload": payload,
            "submitted_at": at,
            "updated_at": at,
        },
    )


def _fill_row(
    *,
    label: str,
    order_id: UUID,
    account_id: UUID,
    symbol: str,
    side: str,
    at: datetime,
    price: Decimal = Decimal("2.00"),
) -> SeedRow:
    return SeedRow(
        Fill.__table__,
        {
            "id": _id(f"fill/{label}"),
            "order_id": order_id,
            "strategy_id": STRATEGY_ID,
            "broker_account_id": account_id,
            "broker_fill_id": f"fill-{_id(f'fill/{label}')}",
            "symbol": symbol,
            "side": side,
            "quantity": Decimal("1"),
            "price": price,
            "filled_at": at,
            "payload": {},
        },
    )


def _event_row(*, label: str, order_id: UUID, at: datetime) -> SeedRow:
    return SeedRow(
        BrokerOrderEvent.__table__,
        {
            "id": _id(f"event/{label}"),
            "order_id": order_id,
            "event_type": "rejected",
            "event_at": at,
            "event_source": "broker",
            "payload": {"reason": "NEW_NO_POSITION_SQL_CAN_NOT_SELL_SHORT"},
        },
    )


def _load_module(name: str, relative_path: str) -> ModuleType:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OUTCOME = _load_module(
    "integration_fanout_outcome_acceptance",
    "ops/health/fanout_outcome_acceptance.py",
)
IDENTITY = _load_module(
    "integration_fanout_identity_acceptance",
    "ops/health/fanout_identity_acceptance.py",
)


@pytest.fixture(scope="session")
def postgres_harness() -> PostgresAcceptanceHarness:
    database_url = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not database_url or "sqlite" in database_url:
        pytest.fail("MAI_TAI_DATABASE_URL must name the required PostgreSQL CI service")
    harness = PostgresAcceptanceHarness(database_url)
    harness.assert_available()
    return harness


def _common_rows() -> tuple[SeedRow, ...]:
    return (
        SeedRow(
            Strategy.__table__,
            {
                "id": STRATEGY_ID,
                "code": "schwab_1m_v2",
                "name": "SQL harness strategy",
                "execution_mode": "live",
                "metadata": {},
            },
        ),
        SeedRow(
            BrokerAccount.__table__,
            {
                "id": WEBULL_ID,
                "name": "live:orb",
                "provider": "webull",
                "environment": "live",
                "external_account_id": "sql-harness-webull",
            },
        ),
        SeedRow(
            BrokerAccount.__table__,
            {
                "id": SCHWAB_ID,
                "name": "live:schwab_1m_v2",
                "provider": "schwab",
                "environment": "live",
                "external_account_id": "sql-harness-schwab",
            },
        ),
    )


def _outcome_seed_rows() -> tuple[SeedRow, ...]:
    orders: list[SeedRow] = []
    fills: list[SeedRow] = []
    events: list[SeedRow] = []

    def add_order(
        *,
        label: str,
        account_id: UUID,
        symbol: str,
        side: str,
        at: datetime,
        status: str,
        payload: dict[str, object],
        order_type: str = "STOP_LIMIT",
        filled: bool = False,
        price: Decimal = Decimal("2.00"),
    ) -> UUID:
        row = _order_row(
            label=label,
            account_id=account_id,
            symbol=symbol,
            side=side,
            at=at,
            status=status,
            payload=payload,
            order_type=order_type,
        )
        order_id = row.values["id"]
        assert isinstance(order_id, UUID)
        orders.append(row)
        if filled:
            fills.append(
                _fill_row(
                    label=label,
                    order_id=order_id,
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    at=at,
                    price=price,
                )
            )
        return order_id

    # Reproduce the fixed matched-order and paired-leg controls with one overlapping population.
    # The 18 Webull fills below are part of the 53 filled fan-out legs: 16 have a usable arm id,
    # seven of those have a Schwab counterpart, and the remaining two are CTT. Another 35 CTT
    # fills after the fill-rate control's upper bound complete the measured 53/16/37/7/9 shape.
    fill_control_at = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)
    for index in range(292):
        symbol = f"MF{index % 12:02d}"
        payload: dict[str, object] = {"fanout_source": "rth_resting_mirror"}
        if index < 16:
            payload["cw_arm_bar_ts"] = f"arm-{index:02d}"
        add_order(
            label=f"base-fill-webull-{index}",
            account_id=WEBULL_ID,
            symbol=symbol,
            side="buy",
            at=fill_control_at + timedelta(seconds=index),
            status="filled" if index < 18 else "cancelled",
            payload=payload,
            filled=index < 18,
        )
    for index in range(368):
        symbol = f"MF{index % 12:02d}"
        payload = {"cw_arm_bar_ts": f"arm-{index:02d}"} if index < 7 else {}
        add_order(
            label=f"base-fill-schwab-{index}",
            account_id=SCHWAB_ID,
            symbol=symbol,
            side="buy",
            at=fill_control_at + timedelta(seconds=index),
            status="filled" if index < 34 else "cancelled",
            payload=payload,
            filled=index < 34,
        )
    pair_tail_at = datetime(2026, 8, 26, 16, 0, tzinfo=UTC)
    for index in range(35):
        add_order(
            label=f"base-pair-tail-{index}",
            account_id=WEBULL_ID,
            symbol=f"PT{index:02d}",
            side="buy",
            at=pair_tail_at + timedelta(seconds=index),
            status="filled",
            payload={"fanout_source": "rth_resting_mirror"},
            filled=True,
        )

    # Twenty-two two-leg Webull segments reproduce 22 extras, every one 4.58% worse. Keeping the
    # entire fixture relational makes base_dup_control itself part of the proof.
    duplicate_at = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    for segment in range(22):
        for leg, price in ((0, Decimal("100.00")), (1, Decimal("104.58"))):
            add_order(
                label=f"base-duplicate-{segment}-{leg}",
                account_id=WEBULL_ID,
                symbol=f"DP{segment:02d}",
                side="buy",
                at=duplicate_at + timedelta(minutes=segment, seconds=leg),
                status="filled",
                payload={
                    "fanout_source": "rth_resting_mirror",
                    "fanout_segment_id": f"dup-segment-{segment}",
                    "cw_entry_n": "1",
                },
                filled=True,
                price=price,
            )

    # Reproduce all three refused-exit controls. Multiple refusals may classify to one preceding
    # fill; the distinct-fill episode denominator is therefore seeded independently of the event
    # count, matching the production query rather than a fabricated CONTROL row.
    refused_shapes = {
        "2026-08-24": (37, 9, 2),
        "2026-08-25": (25, 9, 2),
        "2026-08-26": (49, 49, 11),
    }
    for day, (refused, classified, episodes) in refused_shapes.items():
        day_at = datetime.fromisoformat(f"{day}T16:00:00+00:00")
        classified_orders: list[UUID] = []
        for episode in range(episodes):
            symbol = f"R{day[-2:]}{episode:02d}"
            add_order(
                label=f"base-refused-fill-{day}-{episode}",
                account_id=WEBULL_ID,
                symbol=symbol,
                side="sell",
                at=day_at,
                status="filled",
                payload={},
                order_type="MARKET",
                filled=True,
            )
            classified_orders.append(
                add_order(
                    label=f"base-refused-order-{day}-{episode}",
                    account_id=WEBULL_ID,
                    symbol=symbol,
                    side="sell",
                    at=day_at + timedelta(minutes=1),
                    status="rejected",
                    payload={},
                    order_type="MARKET",
                )
            )
        for index in range(classified):
            events.append(
                _event_row(
                    label=f"base-refused-classified-{day}-{index}",
                    order_id=classified_orders[index % episodes],
                    at=day_at + timedelta(minutes=2, seconds=index),
                )
            )
        no_prior = refused - classified
        if no_prior:
            unclassified_order = add_order(
                label=f"base-refused-unclassified-{day}",
                account_id=WEBULL_ID,
                symbol=f"U{day[-2:]}",
                side="sell",
                at=day_at + timedelta(minutes=1),
                status="rejected",
                payload={},
                order_type="MARKET",
            )
            for index in range(no_prior):
                events.append(
                    _event_row(
                        label=f"base-refused-unclassified-{day}-{index}",
                        order_id=unclassified_order,
                        at=day_at + timedelta(minutes=2, seconds=classified + index),
                    )
                )

    # Requested-window target rows. The refused event exercises the numerator, while the earlier
    # confirmed sell fill supplies its episode denominator. The OUTSIDE rows let the differential
    # control prove both disappear together.
    for label, symbol, at, segment_id in (
        ("target-buy-outside", "OUTSIDE", OUTSIDE_AT, "9201"),
        ("target-buy-since", "SINCE_BOUND", LIVE_WINDOW.since, "9202"),
        ("target-buy-until", "UNTIL_BOUND", LIVE_WINDOW.until, "9203"),
    ):
        add_order(
            label=label,
            account_id=WEBULL_ID,
            symbol=symbol,
            side="buy",
            at=at,
            status="filled",
            payload={
                "fanout_source": "rth_resting_mirror",
                "fanout_segment_id": segment_id,
                "fanout_slot": "resting",
                "fanout_slot_id": fanout_slot_id(
                    strategy_code="schwab_1m_v2",
                    symbol=symbol,
                    segment_id=segment_id,
                    slot="resting",
                ),
            },
            filled=True,
        )
    for label, symbol, at in (
        ("target-sell-inside", "INSIDE", INSIDE_AT),
        ("target-sell-outside", "OUTSIDE", OUTSIDE_AT),
    ):
        add_order(
            label=label,
            account_id=WEBULL_ID,
            symbol=symbol,
            side="sell",
            at=at,
            status="filled",
            payload={},
            order_type="MARKET",
            filled=True,
        )
        rejected_order = add_order(
            label=f"{label}-refusal",
            account_id=WEBULL_ID,
            symbol=symbol,
            side="sell",
            at=at + timedelta(seconds=1),
            status="rejected",
            payload={},
            order_type="MARKET",
        )
        events.append(
            _event_row(
                label=f"{label}-refusal",
                order_id=rejected_order,
                at=at + timedelta(seconds=2),
            )
        )

    return (*_common_rows(), *orders, *fills, *events)


def _identity_seed_rows() -> tuple[SeedRow, ...]:
    intents: list[SeedRow] = []
    orders: list[SeedRow] = []
    for intent_id, symbol, at, segment_id in (
        (INTENT_INSIDE_ID, "IDENT_INSIDE", INSIDE_AT, "9101"),
        (INTENT_OUTSIDE_ID, "IDENT_OUTSIDE", OUTSIDE_AT, "9102"),
        (_id("intent/identity-since"), "IDENT_SINCE", LIVE_WINDOW.since, "9103"),
        (_id("intent/identity-until"), "IDENT_UNTIL", LIVE_WINDOW.until, "9104"),
    ):
        slot_id = fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol=symbol,
            segment_id=segment_id,
            slot="resting",
        )
        order_label = f"identity-{segment_id}"
        order_id = _id(f"order/{order_label}")
        attempt_id = f"sql-harness-{order_id}"
        metadata = {
            "fanout_segment_id": segment_id,
            "fanout_slot": "resting",
            "fanout_slot_id": slot_id,
            "fanout_attempt_id": attempt_id,
            "fanout_source": "rth_resting_mirror",
        }
        intents.append(
            SeedRow(
                TradeIntent.__table__,
                {
                    "id": intent_id,
                    "strategy_id": STRATEGY_ID,
                    "broker_account_id": WEBULL_ID,
                    "symbol": symbol,
                    "side": "buy",
                    "intent_type": "open",
                    "quantity": Decimal("1"),
                    "reason": "SQL harness identity",
                    "status": "queued",
                    "payload": {"metadata": metadata},
                    "created_at": at,
                    "updated_at": at,
                },
            )
        )
        order = _order_row(
            label=order_label,
            account_id=WEBULL_ID,
            symbol=symbol,
            side="buy",
            at=at,
            status="submitted",
            payload=metadata,
            intent_id=intent_id,
        )
        assert order.values["id"] == order_id
        assert order.values["client_order_id"] == attempt_id
        orders.append(order)
    return (*_common_rows(), *intents, *orders)


def _d6_report(module: ModuleType, raw: str, window: SqlWindow):  # type: ignore[no-untyped-def]
    return module.evaluate(module.parse_rows(raw), since=window.since, until=window.until)


def _d6_refusal_observation(module: ModuleType, raw: str, window: SqlWindow) -> str:
    report = _d6_report(module, raw, window)
    return next(line for line in report.lines if line.startswith("metric=refused_exits "))


def _identity_observation(module: ModuleType, raw: str, window: SqlWindow) -> str:
    intents, attempts = module.parse_database_rows(raw)
    report = module.evaluate(
        intents=intents,
        attempts=attempts,
        starts=[module.ProcessStart(at=window.since - timedelta(minutes=1), pid=4242)],
        since=window.since,
        until=window.until,
    )
    return report.verdict


@pytest.mark.parametrize(
    ("module", "seed_rows", "observer", "live_expected", "empty_expected"),
    (
        pytest.param(
            OUTCOME,
            _outcome_seed_rows(),
            _d6_refusal_observation,
            "verdict=FAIL",
            "verdict=UNEXERCISED",
            id="fanout-outcome-refused-exits",
        ),
        pytest.param(
            IDENTITY,
            _identity_seed_rows(),
            _identity_observation,
            "PASS",
            "UNEXERCISED",
            id="fanout-identity",
        ),
    ),
)
def test_real_sql_flips_when_evidence_moves_outside_the_window(
    postgres_harness: PostgresAcceptanceHarness,
    module: ModuleType,
    seed_rows: tuple[SeedRow, ...],
    observer: Callable[[ModuleType, str, SqlWindow], str],
    live_expected: str,
    empty_expected: str,
) -> None:
    live_case = AcceptanceSqlCase(module, seed_rows, LIVE_WINDOW)
    empty_case = AcceptanceSqlCase(module, seed_rows, EMPTY_WINDOW)

    live = observer(module, postgres_harness.execute(live_case), LIVE_WINDOW)
    empty = observer(module, postgres_harness.execute(empty_case), EMPTY_WINDOW)

    assert live_expected in live
    assert empty_expected in empty
    assert live != empty


def _replace_once(sql: str, old: str, new: str) -> str:
    assert sql.count(old) == 1, "mutation site must remain singular and reachable"
    return sql.replace(old, new, 1)


def _launder_target_buy_fill_time(sql: str) -> str:
    old = "JOIN fill_by_order fbo ON fbo.order_id = bo.id\n    CROSS JOIN target_bounds b"
    new = """JOIN (
        SELECT fbo.order_id, b.since_at AS first_fill_at, fbo.average_price
        FROM fill_by_order fbo
        CROSS JOIN target_bounds b
    ) fbo ON fbo.order_id = bo.id
    CROSS JOIN target_bounds b"""
    return _replace_once(sql, old, new)


def _launder_sell_fill_time(sql: str) -> str:
    old = """sell_fill_by_order AS (
    SELECT f.order_id, min(f.filled_at) AS first_fill_at
    FROM fills f
    WHERE f.side = 'sell'
    GROUP BY f.order_id
),"""
    new = """sell_fill_by_order AS (
    SELECT sfbo.order_id, b.since_at AS first_fill_at
    FROM (
        SELECT f.order_id, min(f.filled_at) AS first_fill_at
        FROM fills f
        WHERE f.side = 'sell'
        GROUP BY f.order_id
    ) sfbo
    CROSS JOIN target_bounds b
),"""
    return _replace_once(sql, old, new)


def _add_unbounded_rows_union(sql: str) -> str:
    old = """    SELECT 'EXIT_EPISODE', order_id, session_date_et, '', '', '', '', '', '', ''
    FROM target_exit_episodes
)"""
    new = """    SELECT 'EXIT_EPISODE', order_id, session_date_et, '', '', '', '', '', '', ''
    FROM target_exit_episodes
    UNION ALL
    SELECT 'EXIT_EPISODE', bo.id::text,
           (sfbo.first_fill_at AT TIME ZONE 'America/New_York')::date::text,
           '', '', '', '', '', '', ''
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    JOIN sell_fill_by_order sfbo ON sfbo.order_id = bo.id
    WHERE ba.name = 'live:orb' AND bo.side = 'sell'
)"""
    return _replace_once(sql, old, new)


def _unbound_target_refused(sql: str) -> str:
    old = """      AND e.event_at >= b.since_at AND e.event_at < b.until_at
),
target_refused_classified AS ("""
    new = """),
target_refused_classified AS ("""
    return _replace_once(sql, old, new)


@pytest.mark.parametrize(
    ("mutator", "kind"),
    (
        pytest.param(_launder_target_buy_fill_time, "FANOUT_FILL", id="target-buy-derived-table"),
        pytest.param(_launder_sell_fill_time, "EXIT_EPISODE", id="sell-fill-derived-table"),
        pytest.param(_add_unbounded_rows_union, "EXIT_EPISODE", id="rows-unbounded-union"),
        pytest.param(_unbound_target_refused, "REFUSAL", id="target-refused-numerator"),
    ),
)
def test_d6_real_postgres_kills_textually_invisible_window_leaks(
    postgres_harness: PostgresAcceptanceHarness,
    mutator: Callable[[str], str],
    kind: str,
) -> None:
    case = AcceptanceSqlCase(OUTCOME, _outcome_seed_rows(), EMPTY_WINDOW)
    correct = OUTCOME.parse_rows(postgres_harness.execute(case))
    mutated = OUTCOME.parse_rows(postgres_harness.execute(case, sql=mutator(OUTCOME.SQL)))

    correct_count = sum(row.kind == kind for row in correct)
    mutated_count = sum(row.kind == kind for row in mutated)
    assert correct_count == 0
    assert mutated_count > correct_count


def test_d6_uses_real_control_rows_and_fails_when_the_control_moves(
    postgres_harness: PostgresAcceptanceHarness,
) -> None:
    case = AcceptanceSqlCase(OUTCOME, _outcome_seed_rows(), LIVE_WINDOW)
    correct = _d6_report(OUTCOME, postgres_harness.execute(case), LIVE_WINDOW)
    assert "control=PASS known-bad populations reproduced from durable rows" in correct.lines

    mutated_sql = _replace_once(
        OUTCOME.SQL,
        "AND fbo.first_fill_at >= timestamptz '2026-08-21 00:00 America/New_York'",
        "AND fbo.first_fill_at >= timestamptz '2026-08-27 00:00 America/New_York'",
    )
    mutated = _d6_report(
        OUTCOME,
        postgres_harness.execute(case, sql=mutated_sql),
        LIVE_WINDOW,
    )
    assert mutated.verdict == "COULD_NOT_TELL"
    assert any(line.startswith("control=FAILED") for line in mutated.lines)


def test_d6_window_is_since_inclusive_and_until_exclusive(
    postgres_harness: PostgresAcceptanceHarness,
) -> None:
    case = AcceptanceSqlCase(OUTCOME, _outcome_seed_rows(), LIVE_WINDOW)
    correct = OUTCOME.parse_rows(postgres_harness.execute(case))
    correct_symbols = {row.values[2] for row in correct if row.kind == "FANOUT_FILL"}
    assert "SINCE_BOUND" in correct_symbols
    assert "UNTIL_BOUND" not in correct_symbols

    mutated_sql = _replace_once(
        OUTCOME.SQL,
        "AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at < b.until_at",
        "AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at <= b.until_at",
    )
    mutated = OUTCOME.parse_rows(postgres_harness.execute(case, sql=mutated_sql))
    mutated_symbols = {row.values[2] for row in mutated if row.kind == "FANOUT_FILL"}
    assert "UNTIL_BOUND" in mutated_symbols


def test_identity_window_is_since_inclusive_and_until_exclusive_for_orders(
    postgres_harness: PostgresAcceptanceHarness,
) -> None:
    case = AcceptanceSqlCase(IDENTITY, _identity_seed_rows(), LIVE_WINDOW)
    correct_intents, correct_orders = IDENTITY.parse_database_rows(
        postgres_harness.execute(case)
    )
    assert "IDENT_SINCE" in {row.symbol for row in correct_intents}
    assert "IDENT_SINCE" in {row.symbol for row in correct_orders}
    assert "IDENT_UNTIL" not in {row.symbol for row in correct_intents}
    assert "IDENT_UNTIL" not in {row.symbol for row in correct_orders}

    ti_bound = "AND ti.created_at < :'window_until'::timestamptz"
    bo_bound = "AND bo.submitted_at < :'window_until'::timestamptz"
    assert IDENTITY.SQL.count(ti_bound) == 2
    assert IDENTITY.SQL.count(bo_bound) == 1
    mutated_sql = IDENTITY.SQL.replace(ti_bound, ti_bound.replace(" < ", " <= "))
    mutated_sql = mutated_sql.replace(bo_bound, bo_bound.replace(" < ", " <= "))
    mutated_intents, mutated_orders = IDENTITY.parse_database_rows(
        postgres_harness.execute(case, sql=mutated_sql)
    )
    assert "IDENT_UNTIL" in {row.symbol for row in mutated_intents}
    assert "IDENT_UNTIL" in {row.symbol for row in mutated_orders}


def test_unavailable_postgres_is_a_failure_not_a_skip() -> None:
    unavailable = PostgresAcceptanceHarness(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:1/unavailable?connect_timeout=1"
    )

    with pytest.raises(PostgresHarnessError, match="required PostgreSQL service is unavailable"):
        unavailable.assert_available()
