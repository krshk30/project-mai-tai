"""Shared, decision-time-only metrics used by validated research instruments."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal


def kaufman_efficiency_ratio(values: Sequence[Decimal]) -> Decimal | None:
    """Return net displacement divided by total path travel.

    The input is ordered oldest to newest. ``None`` means there are not enough
    observations; a stationary series has a measured ratio of zero.
    """

    if len(values) < 2:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    travel = sum((abs(change) for change in changes), Decimal("0"))
    if travel == 0:
        return Decimal("0")
    return abs(values[-1] - values[0]) / travel
