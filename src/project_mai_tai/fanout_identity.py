"""Durable identity helpers for the Section 82 Webull fan-out lifecycle.

This module assigns names only. Nothing here releases a claim, suppresses an
entry, changes quantity, or calls a venue.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5


FANOUT_IDENTITY_KEYS = (
    "fanout_segment_id",
    "fanout_slot",
    "fanout_slot_id",
    "fanout_attempt_id",
    "fanout_predecessor_attempt_id",
)

_SLOT_BY_SOURCE = {
    "rth_resting": "resting",
    "rth_resting_mirror": "resting",
    "eh_resting": "resting",
    "reactive": "reclaim",
}


def fanout_slot_for_source(source: str) -> str:
    """Return the design-approved economic slot for a fan-out source."""

    normalized = str(source).strip().lower()
    try:
        return _SLOT_BY_SOURCE[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown fan-out source: {source!r}") from exc


def fanout_slot_id(
    *, strategy_code: str, symbol: str, segment_id: int | str, slot: str
) -> str:
    """Derive the stable slot id from the canonical Section 82 tuple."""

    strategy = str(strategy_code).strip().lower()
    normalized_symbol = str(symbol).strip().upper()
    normalized_slot = str(slot).strip().lower()
    try:
        segment = int(segment_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("fan-out segment id must be a positive integer") from exc
    if not strategy or not normalized_symbol or segment <= 0:
        raise ValueError("strategy, symbol, and a positive segment id are required")
    if normalized_slot not in {"resting", "reclaim"}:
        raise ValueError(f"unknown fan-out slot: {slot!r}")
    key = f"mai-tai:fanout-slot:v1:{strategy}:{normalized_symbol}:{segment}:{normalized_slot}"
    return str(uuid5(NAMESPACE_URL, key))


def carry_fanout_identity(
    base: Mapping[str, object], authoritative: Mapping[str, object]
) -> dict[str, str]:
    """Copy only lifecycle identity keys, with ``authoritative`` winning.

    Broker SDK reports do not consistently echo request metadata. This helper
    preserves the locally committed identity without broadening the report
    payload with unrelated request fields.
    """

    merged = {str(key): str(value) for key, value in base.items()}
    for key in FANOUT_IDENTITY_KEYS:
        value = str(authoritative.get(key, "") or "").strip()
        if value:
            merged[key] = value
    return merged
