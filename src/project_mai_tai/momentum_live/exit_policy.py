"""MOMEXIT-B stop policy shared by software and both native broker legs.

The legacy ``hard_stop_15pct`` reason now denotes this 10% stop. Its name stays
stable for mandatory-exit dispatch, UNKNOWN-read handling and durable retries.
Native callers retain their existing tick rounding and representability checks.
"""

from decimal import Decimal


STOP_LOSS_PERCENT = Decimal("10")
STOP_LOSS_MULTIPLIER = Decimal("0.90")


def stop_loss_level(entry_price: Decimal) -> Decimal:
    """Return the unrounded 10% stop level without I/O or session policy."""
    return entry_price * STOP_LOSS_MULTIPLIER
