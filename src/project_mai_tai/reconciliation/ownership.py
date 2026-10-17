"""Alert classification only: account surplus is never authority to sell it."""

from decimal import Decimal
from typing import Mapping


def coherent_owned_quantity(
    *, virtual: Mapping[str, Decimal], managed: Mapping[str, Decimal],
    fills: Mapping[str, Decimal], tolerance: Decimal, require_positive_virtual: bool = False,
) -> Decimal | None:
    """Require independent fill coverage for EACH strategy, not just a net sum.

    Preserve the established false-zero VP allowance using the managed book,
    but never let another strategy's surplus conceal an uncovered lot.
    """
    total = Decimal(0)
    for code in virtual.keys() | managed.keys() | fills.keys():
        quantities = (virtual.get(code, Decimal(0)), managed.get(code, Decimal(0)),
                      fills.get(code, Decimal(0)))
        if not code or any(not q.is_finite() or q < 0 for q in quantities):
            return None
        vp, ladder, balance = quantities
        owned = max(vp, ladder)
        # Managed fallback is not independent proof for a manual-surplus downgrade.
        if require_positive_virtual and owned > tolerance and vp <= tolerance:
            return None
        if abs(owned - balance) > tolerance:
            return None
        if vp > tolerance and ladder > tolerance and abs(vp - ladder) > tolerance:
            return None
        total += owned
    return total
