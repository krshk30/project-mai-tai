"""Both polarities for the repository-owned #644 composition detector."""
from __future__ import annotations

from ops.health.v2_entry_fix_watch import CAP_ACCT, grade_composition


def _fill(slot: str, *, account: str = CAP_ACCT) -> dict[str, str]:
    return {"acct": account, "entry_slot": slot}


def test_one_first_plus_one_reclaim_is_legal_even_when_both_orders_rest() -> None:
    verdict, first, reclaim, unknown, total = grade_composition([
        _fill("first"),
        _fill("reclaim"),
    ])
    assert (verdict, first, reclaim, unknown, total) == ("OK", 1, 1, 0, 2)


def test_two_first_slots_are_a_known_breach() -> None:
    verdict, first, reclaim, unknown, total = grade_composition([
        _fill("first"),
        _fill("first"),
    ])
    assert (verdict, first, reclaim, unknown, total) == ("BREACH", 2, 0, 0, 2)


def test_missing_or_invalid_slot_fails_closed() -> None:
    assert grade_composition([_fill("")])[0] == "COULD_NOT_TELL"
    assert grade_composition([_fill("resting")])[0] == "COULD_NOT_TELL"


def test_known_breach_is_not_downgraded_by_an_unattributed_fill() -> None:
    assert grade_composition([
        _fill("reclaim"),
        _fill("reclaim"),
        _fill(""),
    ])[0] == "BREACH"


def test_no_schwab_fill_is_unexercised_not_clean() -> None:
    assert grade_composition([
        _fill("first", account="live:orb"),
    ])[0] == "UNEXERCISED"
