"""Controls for the regression watch.

⛔ The watch exists because a MARKER LIST would page on correct behaviour. These controls pin the
polarity: benign guard activity must stay silent, a stated recurrence must fire, and an unread
source must be CANNOT_TELL rather than OK.
"""

from __future__ import annotations

import pytest

from ops.health.regression_watch import (
    CANNOT_TELL,
    OK,
    RECURRENCE,
    REGISTRY,
    UNARMED,
    Row,
    evaluate,
    exit_code,
    main,
)

_ARMED = [row for row in REGISTRY if row.armed]


def _r(row, **kw):
    return evaluate(row, {row.id: kw})


@pytest.mark.parametrize("row", _ARMED, ids=[r.id for r in _ARMED])
def test_benign_guard_activity_is_silent(row: Row) -> None:
    """The 2026-09-11 case: three old-defect markers fired and ALL were the guard working."""
    assert _r(row, readable=True, recurred=False, detail="guard fired").verdict == OK


@pytest.mark.parametrize("row", _ARMED, ids=[r.id for r in _ARMED])
def test_a_stated_recurrence_fires(row: Row) -> None:
    assert _r(row, readable=True, recurred=True, detail="defect shape seen").verdict == RECURRENCE


@pytest.mark.parametrize("row", _ARMED, ids=[r.id for r in _ARMED])
def test_an_unreadable_source_is_cannot_tell_not_ok(row: Row) -> None:
    """⛔ Absence of evidence was the original defect. It must never read as clean."""
    assert _r(row, readable=False, detail="log unreadable").verdict == CANNOT_TELL


@pytest.mark.parametrize("row", _ARMED, ids=[r.id for r in _ARMED])
def test_a_missing_fact_is_cannot_tell_not_ok(row: Row) -> None:
    assert evaluate(row, {}).verdict == CANNOT_TELL


def test_every_row_states_both_polarities() -> None:
    """⛔ A row without both shapes is the marker list this watch exists to avoid."""
    for row in REGISTRY:
        assert row.marker and row.defect
        assert row.recurrence, f"{row.id} states no recurrence shape"
        if row.armed:
            assert row.benign and row.benign != "n/a", f"{row.id} armed without a benign shape"


def test_an_unarmed_row_is_reported_not_silently_dropped() -> None:
    unarmed = [row for row in REGISTRY if not row.armed]
    assert unarmed, "the registry should keep at least one honest UNARMED row"
    for row in unarmed:
        assert evaluate(row, {}).verdict == UNARMED


def test_recurrence_outranks_cannot_tell_in_the_exit_code() -> None:
    """⛔ A found defect must not be masked by some other row being unreadable."""
    readings = [
        evaluate(_ARMED[0], {_ARMED[0].id: {"readable": True, "recurred": True}}),
        evaluate(_ARMED[1], {}),
    ]
    assert exit_code(readings) == 1


def test_clean_reading_exits_zero_and_unreadable_exits_two() -> None:
    clean = [evaluate(r, {r.id: {"readable": True, "recurred": False}}) for r in _ARMED]
    assert exit_code(clean) == 0
    assert exit_code([evaluate(_ARMED[0], {})]) == 2


def test_unparseable_facts_refuse_rather_than_pass(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: "{not json")})())
    assert main([]) == 2
    assert "CANNOT TELL" in capsys.readouterr().out
