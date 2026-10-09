"""Canonical wire representation preserves the exact receipt instant/revision."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from project_mai_tai.v2_removed_wait import _utc


@pytest.mark.parametrize("offset", [-4, 0, 5.5])
def test_aware_receipt_wire_time_is_utc_without_rounding(offset):
    canonical = datetime(2026, 10, 8, 11, 45, 0, 123456, tzinfo=UTC)
    local = canonical.astimezone(timezone(timedelta(hours=offset)))
    converted = _utc(local)
    assert converted == canonical and converted.timestamp() == local.timestamp()
    assert converted.isoformat() == "2026-10-08T11:45:00.123456+00:00"
    assert _utc(local + timedelta(microseconds=1)).isoformat() != converted.isoformat()
    assert _utc(local + timedelta(milliseconds=1)).timestamp() == converted.timestamp() + .001


def test_existing_naive_sqlite_receipt_contract_remains_utc():
    raw = datetime(2026, 10, 8, 11, 45, 0, 123456)
    assert _utc(raw) == raw.replace(tzinfo=UTC)
