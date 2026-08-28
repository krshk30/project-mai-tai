from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from project_mai_tai.oms.store import OmsStore


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, virtual_position) -> None:
        self.virtual_position = virtual_position
        self.flushes = 0

    def scalars(self, _query):
        return _ScalarRows([self.virtual_position])

    def scalar(self, _query):
        return None

    def flush(self) -> None:
        self.flushes += 1


def _position(*, updated_at):
    return SimpleNamespace(
        broker_account_id=uuid4(),
        symbol="YYGH",
        quantity=Decimal("2"),
        average_price=Decimal("1.25"),
        opened_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        updated_at=updated_at,
    )


def test_missing_updated_at_defers_instead_of_clearing_unknown_age() -> None:
    """Unknown recency is not evidence that the clear-age bound elapsed."""
    virtual_position = _position(updated_at=None)
    session = _Session(virtual_position)
    deferred = []

    cleared = OmsStore().clear_virtual_positions_without_account_backing(
        session,
        minimum_age_seconds=24.119,
        observed_at=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        deferred_out=deferred,
    )

    assert cleared == []
    assert deferred == [
        (
            virtual_position.broker_account_id,
            "YYGH",
            Decimal("2"),
            0.0,
        )
    ]
    assert virtual_position.quantity == Decimal("2")
    assert virtual_position.opened_at is not None


def test_known_old_updated_at_still_reaches_the_clear_path() -> None:
    """The fail-closed unknown case must not make every stale row immortal."""
    observed_at = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
    virtual_position = _position(updated_at=observed_at - timedelta(seconds=25))
    session = _Session(virtual_position)

    cleared = OmsStore().clear_virtual_positions_without_account_backing(
        session,
        minimum_age_seconds=24.119,
        observed_at=observed_at,
    )

    assert cleared == [
        (
            virtual_position.broker_account_id,
            "YYGH",
            Decimal("2"),
        )
    ]
    assert virtual_position.quantity == Decimal("0")
    assert virtual_position.opened_at is None
