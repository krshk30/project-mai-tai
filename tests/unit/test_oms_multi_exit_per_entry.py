"""A symbol exited TWICE in one segment must record BOTH exits (found 2026-07-28).

⭐ THE BUG. The synthetic exit-order row was keyed `f"{entry.client_order_id}-ocoexit"` -- derived
from the ENTRY alone. So when several broker exit legs mapped to one entry order they all collapsed
onto a single row, and `record_fill_if_needed` then rejected every one after the first at
`incremental_quantity <= 0`. Found while backfilling 07-27: BIYA had FOUR real exits and only ONE
was recordable; the backfill recovered 4 of 8 exits overall for this reason.

⛔ It bites hardest on RECLAIM -- a symbol entered twice in the same segment -- which is exactly the
population currently being judged on live fills. Under-recording those exits biases the very
first-vs-reclaim comparison the reclaim decision rests on.

THE FIX: the CHILD order id joins the key. Fills still dedupe on `broker_fill_id` ("<child>:<qty>"),
so widening the ORDER key cannot double-count -- it only stops the second exit being swallowed.
"""
from __future__ import annotations

# Import the PRODUCTION key builder -- a local mirror of the formula could stay green while the
# real code was wrong, which is the whole failure mode this file exists to catch.
from project_mai_tai.oms.service import oco_exit_client_order_id as _exit_coid


ENTRY = "schwab_1m_v2-BIYA-open-dcf4effcb170"


def test_two_exits_on_one_entry_get_two_distinct_rows() -> None:
    """THE REGRESSION. Same entry, different broker child legs -> must not collide."""
    a = _exit_coid(ENTRY, "MBI0V5BR0BCBEMU282GOI4QU7A")
    b = _exit_coid(ENTRY, "RQGIBTK6LV0Q6CFTTHE14G50")
    assert a != b, "both exits collapse onto one order row -- the second is silently dropped"


def test_the_same_child_is_still_stable_so_retries_dedupe() -> None:
    """Idempotency must survive: re-running the backfill must not mint a new row each pass."""
    child = "MBI0V5BR0BCBEMU282GOI4QU7A"
    assert _exit_coid(ENTRY, child) == _exit_coid(ENTRY, child)


def test_four_biya_exits_yield_four_keys() -> None:
    """The live 07-27 case, exactly: 4 real exits under one entry order."""
    children = [
        "MBI0V5BR0BCBEMU282GOI4QU7A",
        "HMG8FJP4KR1O1VKCA0O4TEJ6",
        "QDHK7I8N7253D0BSHA6VPG0C",
        "RQGIBTK6LV0Q6CFTTHE14G50",
    ]
    keys = {_exit_coid(ENTRY, c) for c in children}
    assert len(keys) == 4, f"only {len(keys)} of 4 BIYA exits are recordable"


def test_missing_child_id_falls_back_to_the_old_key() -> None:
    """Backwards compatible: a detail with no child id keeps the historical shape."""
    assert _exit_coid(ENTRY, "") == f"{ENTRY}-ocoexit"


def test_different_entries_never_collide_either() -> None:
    other = "schwab_1m_v2-BIYA-open-ffffffffffff"
    child = "MBI0V5BR0BCBEMU282GOI4QU7A"
    assert _exit_coid(ENTRY, child) != _exit_coid(other, child)
