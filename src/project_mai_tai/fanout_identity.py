"""Durable identity helpers for the Section 82 Webull fan-out lifecycle.

This module assigns names only. Nothing here releases a claim, suppresses an
entry, changes quantity, or calls a venue.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

ATR_COUNTRY_ROUTE_KEYS = (
    "atr_country_schema", "atr_country_symbol", "atr_country_segment_id",
    "atr_country_broker", "atr_country_account", "atr_country_notional_usd",
    "atr_country_reason", "atr_country_status", "atr_country_as_of",
    "atr_country_sealed_at",
)


FANOUT_IDENTITY_KEYS = (
    "clearwait_dispatch_token",
    "fanout_segment_id",
    "fanout_slot",
    "fanout_slot_id",
    "fanout_attempt_id",
    "fanout_predecessor_attempt_id",
    "fanout_identity_schema",
) + ATR_COUNTRY_ROUTE_KEYS

DISPATCH_BINDING_KEYS = ("clearwait_dispatch_token", "fanout_segment_id")


def replace_broker_order_payload(
    previous: Mapping[str, object] | None, metadata: Mapping[str, object]
) -> dict[str, object]:
    """Keep legacy report overwrite, except exact original dispatch bindings.

    Missing original bindings stay missing; a report/cancel is not authority to
    repair them. Nonbinding fields absent from the new metadata are discarded.
    """
    existing = previous or {}
    payload = dict(metadata)
    for key in DISPATCH_BINDING_KEYS:
        if key in existing:
            payload[key] = existing[key]
        else:
            payload.pop(key, None)
    return payload


def merge_broker_order_payload(
    previous: Mapping[str, object] | None, metadata: Mapping[str, object]
) -> dict[str, object]:
    """Merge report/reprice fields without changing an existing order's identity.

    The order is the original placement, even when the incoming metadata belongs
    to a cancel request. Keep exact committed identity values (including invalid
    values that must remain UNKNOWN). Reports/cancels/reprices cannot introduce
    or repair a missing/null/empty original dispatch binding. New placements
    initialize their payload separately; no parent proof is inferred here.
    Other payload fields retain their JSON types and incoming updates win.
    """
    existing = dict(previous or {})
    merged = {**existing, **metadata}
    for key in FANOUT_IDENTITY_KEYS:
        if key in DISPATCH_BINDING_KEYS:
            if key in existing:
                merged[key] = existing[key]
            else:
                merged.pop(key, None)
            continue
        if key in existing and existing[key] is not None and existing[key] != "":
            merged[key] = existing[key]
    return merged


_SLOT_BY_SOURCE = {
    "rth_resting": "resting",
    "rth_resting_mirror": "resting",
    "eh_resting": "resting",
    "reactive": "reclaim",
}


def fanout_slot_for_source(source: str) -> str:
    """Return the design-approved economic slot for a fan-out source.

    Unknown sources fail closed before a draft can be queued.  Emitting a
    live-money leg without a defined economic slot would recreate the
    unattributable population this increment exists to eliminate.  This is a
    developer invariant, not a trading-policy decision; every production
    source is enumerated and pinned by tests.
    """

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

    # A cancel/report's own identity is not the original placement identity.
    # Do not stringify malformed dispatch values into valid-looking metadata.
    merged = {str(key): str(value) for key, value in base.items()
              if key not in DISPATCH_BINDING_KEYS}
    for key in FANOUT_IDENTITY_KEYS:
        if key in DISPATCH_BINDING_KEYS:
            value = authoritative.get(key)
            if key == "clearwait_dispatch_token":
                if (type(value) is str and 0 < len(value) <= 128 and value == value.strip()):
                    merged[key] = value
            elif (type(value) is int and value > 0
                    or type(value) is str and 0 < len(value) <= 128
                    and value.isascii() and value.isdecimal() and value[0] != "0"):
                merged[key] = str(value)
            continue
        value = str(authoritative.get(key, "") or "").strip()
        if value:
            merged[key] = value
    return merged
