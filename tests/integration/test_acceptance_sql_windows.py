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
from uuid import UUID

import pytest

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent
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
BUY_ORDER_ID = UUID("30000000-0000-0000-0000-000000000001")
SELL_INSIDE_ID = UUID("30000000-0000-0000-0000-000000000002")
SELL_OUTSIDE_ID = UUID("30000000-0000-0000-0000-000000000003")
INTENT_INSIDE_ID = UUID("40000000-0000-0000-0000-000000000001")
INTENT_OUTSIDE_ID = UUID("40000000-0000-0000-0000-000000000002")


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
    )


def _outcome_seed_rows() -> tuple[SeedRow, ...]:
    segment_id = "9001"
    slot = "resting"
    slot_id = fanout_slot_id(
        strategy_code="schwab_1m_v2",
        symbol="OUTSIDE",
        segment_id=segment_id,
        slot=slot,
    )
    rows = list(_common_rows())
    for order_id, symbol, side, at, payload in (
        (
            BUY_ORDER_ID,
            "OUTSIDE",
            "buy",
            OUTSIDE_AT,
            {
                "fanout_source": "rth_resting_mirror",
                "fanout_segment_id": segment_id,
                "fanout_slot": slot,
                "fanout_slot_id": slot_id,
            },
        ),
        (SELL_INSIDE_ID, "INSIDE", "sell", INSIDE_AT, {}),
        (SELL_OUTSIDE_ID, "OUTSIDE", "sell", OUTSIDE_AT, {}),
    ):
        rows.append(
            SeedRow(
                BrokerOrder.__table__,
                {
                    "id": order_id,
                    "intent_id": None,
                    "strategy_id": STRATEGY_ID,
                    "broker_account_id": WEBULL_ID,
                    "client_order_id": f"sql-harness-{order_id}",
                    "broker_order_id": f"broker-{order_id}",
                    "symbol": symbol,
                    "side": side,
                    "order_type": "STOP_LIMIT" if side == "buy" else "MARKET",
                    "time_in_force": "DAY",
                    "quantity": Decimal("1"),
                    "status": "filled",
                    "payload": payload,
                    "submitted_at": at,
                    "updated_at": at,
                },
            )
        )
        rows.append(
            SeedRow(
                Fill.__table__,
                {
                    "order_id": order_id,
                    "strategy_id": STRATEGY_ID,
                    "broker_account_id": WEBULL_ID,
                    "broker_fill_id": f"fill-{order_id}",
                    "symbol": symbol,
                    "side": side,
                    "quantity": Decimal("1"),
                    "price": Decimal("2.00"),
                    "filled_at": at,
                    "payload": {},
                },
            )
        )
    return tuple(rows)


def _identity_seed_rows() -> tuple[SeedRow, ...]:
    rows = list(_common_rows())
    for intent_id, at, segment_id in (
        (INTENT_INSIDE_ID, INSIDE_AT, "9101"),
        (INTENT_OUTSIDE_ID, OUTSIDE_AT, "9102"),
    ):
        slot_id = fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol="IDENT",
            segment_id=segment_id,
            slot="resting",
        )
        rows.append(
            SeedRow(
                TradeIntent.__table__,
                {
                    "id": intent_id,
                    "strategy_id": STRATEGY_ID,
                    "broker_account_id": WEBULL_ID,
                    "symbol": "IDENT",
                    "side": "buy",
                    "intent_type": "open",
                    "quantity": Decimal("1"),
                    "reason": "SQL harness identity",
                    "status": "queued",
                    "payload": {
                        "metadata": {
                            "fanout_segment_id": segment_id,
                            "fanout_slot": "resting",
                            "fanout_slot_id": slot_id,
                            "fanout_attempt_id": f"attempt-{segment_id}",
                            "fanout_source": "rth_resting_mirror",
                        }
                    },
                    "created_at": at,
                    "updated_at": at,
                },
            )
        )
    return tuple(rows)


def _d6_control_rows(module: ModuleType) -> tuple[object, ...]:
    blank = ("",) * 9
    rows = [
        module.RawRow(
            "CONTROL_PAIR",
            tuple(
                map(
                    str,
                    (
                        module.BASE_PAIR_TOTAL,
                        module.BASE_PAIR_USABLE,
                        module.BASE_PAIR_COULD_NOT_TELL,
                        module.BASE_PAIR_PAIRED,
                        module.BASE_PAIR_WEBULL_ONLY,
                    ),
                )
            )
            + blank[5:],
        ),
        module.RawRow(
            "CONTROL_FILL",
            tuple(
                map(
                    str,
                    (
                        module.BASE_MIRROR_ORDERS,
                        module.BASE_MIRROR_FILLS,
                        module.BASE_SCHWAB_ORDERS,
                        module.BASE_SCHWAB_FILLS,
                        12,
                    ),
                )
            )
            + blank[5:],
        ),
        module.RawRow(
            "CONTROL_DUP",
            (
                str(module.BASE_DUPLICATE_LEGS),
                str(module.BASE_DUPLICATE_WORSE),
                str(module.BASE_DUPLICATE_MEDIAN_PCT),
            )
            + blank[3:],
        ),
    ]
    rows.extend(
        module.RawRow(
            "CONTROL_REFUSED",
            (day, *(str(value) for value in counts), "", "", "", ""),
        )
        for day, counts in module.BASE_REFUSED.items()
    )
    return tuple(rows)


def _d6_refusal_observation(module: ModuleType, raw: str, window: SqlWindow) -> str:
    target_rows = tuple(row for row in module.parse_rows(raw) if not row.kind.startswith("CONTROL_"))
    report = module.evaluate(
        (*_d6_control_rows(module), *target_rows),
        since=window.since,
        until=window.until,
    )
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
            "verdict=PASS",
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


@pytest.mark.parametrize(
    ("mutator", "kind"),
    (
        pytest.param(_launder_target_buy_fill_time, "FANOUT_FILL", id="target-buy-derived-table"),
        pytest.param(_launder_sell_fill_time, "EXIT_EPISODE", id="sell-fill-derived-table"),
        pytest.param(_add_unbounded_rows_union, "EXIT_EPISODE", id="rows-unbounded-union"),
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


def test_unavailable_postgres_is_a_failure_not_a_skip() -> None:
    unavailable = PostgresAcceptanceHarness(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:1/unavailable?connect_timeout=1"
    )

    with pytest.raises(PostgresHarnessError, match="required PostgreSQL service is unavailable"):
        unavailable.assert_available()
