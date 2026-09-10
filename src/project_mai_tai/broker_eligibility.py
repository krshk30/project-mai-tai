"""Shared broker eligibility classifications used by live and offline paths."""

from __future__ import annotations


SCHWAB_OPENING_INELIGIBLE_REASON_SUBSTRINGS = (
    "opening transactions for this security must be placed with a broker",
    "not eligible for electronic entry",
)


def is_schwab_opening_ineligible_reason(reason: str | None) -> bool:
    """Return whether Schwab says the symbol cannot be opened electronically."""
    normalized = str(reason or "").strip().lower()
    return any(
        fragment in normalized for fragment in SCHWAB_OPENING_INELIGIBLE_REASON_SUBSTRINGS
    )
