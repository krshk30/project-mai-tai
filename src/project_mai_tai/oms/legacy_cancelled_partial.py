"""Negative-only legacy handover predicate; never an execution accounting writer."""

from decimal import Decimal

SOURCE = "legacy_handover_unaccounted_cancelled_child"


def has_cancelled_child_execution(reports):
    """Replay requires genuine reports; missing history is not a zero trigger."""
    return any(
        report.event_type == "cancelled"
        and isinstance(report.filled_quantity, Decimal)
        and report.filled_quantity.is_finite()
        and report.filled_quantity > 0
        for report in reports
    )
