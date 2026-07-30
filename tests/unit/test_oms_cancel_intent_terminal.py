"""A cancel intent must reach a terminal status — it tracks the REQUEST, not the order's fate.

⭐⭐ WHY (2026-07-30). `_process_cancel_intent` copied `report.event_type` straight onto the intent.
For a cancel, the report describes the TARGET ORDER — and Schwab answers a just-issued DELETE with
`PENDING_CANCEL`, which maps into ACCEPTED_STATUSES. So the intent was marked "accepted":
non-terminal, and nothing ever polls a cancel intent again.

Measured: 11 stuck cancel intents by mid-session, oldest 209 minutes, all `accepted` with zero
broker orders of their own — while every one of their TARGET ORDERS had already reached
`cancelled`/`filled`. The cancels all SUCCEEDED; only the bookkeeping was abandoned. Each produced
a `stuck_intent` reconciler finding every 30s: 3,954 warnings in a day, burying everything else.

⛔ This also gates #625: that change deliberately reports `accepted` for an UNCONFIRMED cancel so
the ORDER stays open for the reconcile sweep. Correct for the order — and it feeds this same intent
leak unless the intent is terminalized independently.
"""
from __future__ import annotations

from project_mai_tai.oms import service as svc


def test_the_service_actually_CALLS_the_resolver() -> None:
    """Pins the wiring, not just the rule. The helper being correct is worthless if
    `_process_cancel_intent` still copies `report.event_type` across directly."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._process_cancel_intent)
    assert "resolve_cancel_intent_status(" in src
    assert "mark_intent_status(intent, report.event_type)" not in src


def test_the_terminal_set_is_pinned() -> None:
    """Pin the VALUES. A status quietly dropped from this set silently re-opens the leak."""
    assert set(svc._TERMINAL_INTENT_STATUSES) == {"filled", "rejected", "cancelled"}


def test_pending_cancel_is_NOT_terminal_which_is_why_it_leaked() -> None:
    """`PENDING_CANCEL` maps to `accepted`, and `accepted` is not terminal. That gap is the bug."""
    assert "accepted" not in svc._TERMINAL_INTENT_STATUSES
    assert "PENDING_CANCEL" in svc_accepted_statuses()


def svc_accepted_statuses() -> set[str]:
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter

    return SchwabBrokerAdapter.ACCEPTED_STATUSES


# ⛔ Bind to the REAL function. An earlier version of this file re-implemented the rule locally,
# so reverting the fix left every test green — the exact "tests a copy, not the code" trap.
_resolve = svc.resolve_cancel_intent_status


def test_a_pending_cancel_report_terminalizes_the_cancel_intent() -> None:
    """THE REGRESSION: this is the exact live shape."""
    assert _resolve("cancel", "accepted") == "cancelled"
    assert _resolve("cancel", "accepted") in svc._TERMINAL_INTENT_STATUSES


def test_a_terminal_report_still_wins_including_FILLED() -> None:
    """⛔ Do not paper over a lost race. If the order FILLED instead of cancelling, the intent must
    say so — that is a real outcome an operator needs to see, not a tidy 'cancelled'."""
    assert _resolve("cancel", "filled") == "filled"
    assert _resolve("cancel", "rejected") == "rejected"
    assert _resolve("cancel", "cancelled") == "cancelled"


def test_NON_cancel_intents_are_untouched() -> None:
    """⛔ An OPEN intent legitimately sits `accepted`/`submitted` while its resting order waits at
    the broker — for its entire life. Terminalizing those would break the in-flight position union
    the entry gates depend on (#580)."""
    for et in ("accepted", "submitted", "pending", "partially_filled"):
        assert _resolve("open", et) == et
        assert _resolve("close", et) == et
