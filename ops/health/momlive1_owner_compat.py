"""Read-only validation of the exact MOMLIVE quote owner, not a health waiver."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
from uuid import UUID

OWNER = "momentum-live"
STRATEGY = "momentum_warn_live"
ACTIVE = frozenset({"claimed", "submitting", "working", "partial", "held",
                    "cancel_requested", "exiting", "unknown"})
MAX_BYTES = 16_384


def decode_proof(raw: str | bytes) -> dict:
    if not isinstance(raw, (str, bytes)):
        raise ValueError("live owner proof missing or oversized")
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except UnicodeError as exc:
        raise ValueError("live owner proof unreadable") from exc
    if size > MAX_BYTES:
        raise ValueError("live owner proof missing or oversized")
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("live owner proof unreadable") from exc
    if not isinstance(row, dict):
        raise ValueError("live owner proof is not an object")
    return row


def validate_live_owner(symbols: set[str], proof: dict, *, enabled: bool,
                        now_ns: int, max_age_ns: int, expected_instance: str,
                        expected_accounts: set[str]) -> dict:
    if type(enabled) is not bool or type(now_ns) is not int or type(max_age_ns) is not int:
        raise ValueError("live owner configuration unreadable")
    if (now_ns <= 0 or max_age_ns <= 0 or not isinstance(expected_instance, str)
            or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", expected_instance)
            or type(expected_accounts) is not set or len(expected_accounts) != 2
            or any(not isinstance(x, str) or not x for x in expected_accounts)):
        raise ValueError("live owner expected identity unreadable")
    if (type(symbols) is not set or len(symbols) > 2
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Z]{1,5}", x) for x in symbols)):
        raise ValueError("live quote union invalid or exceeds two")
    if (not isinstance(proof, dict) or type(proof.get("version")) is not int
            or proof["version"] != 1 or proof.get("source_service") != "oms"
            or proof.get("producer_instance_id") != expected_instance
            or proof.get("strategy_code") != STRATEGY
            or proof.get("restore_ready") is not True
            or type(proof.get("entry_admission_enabled")) is not bool
            or proof["entry_admission_enabled"] != enabled):
        raise ValueError("live owner producer or restore identity unproven")
    stamp = proof.get("observed_ns")
    if type(stamp) is not int or not 0 <= now_ns - stamp <= max_age_ns:
        raise ValueError("live owner proof stale or future")
    accounts = proof.get("account_names")
    if (not isinstance(accounts, list) or len(accounts) != 2
            or any(not isinstance(x, str) for x in accounts)
            or set(accounts) != expected_accounts):
        raise ValueError("live owner account binding unproven")
    rows = proof.get("episodes")
    declared = proof.get("quote_symbols")
    if (not isinstance(rows, list) or len(rows) > 2 or not isinstance(declared, list)
            or len(declared) > 2 or any(not isinstance(x, str) for x in declared)
            or len(set(declared)) != len(declared) or set(declared) != symbols):
        raise ValueError("live owner episode or quote union mismatch")
    ids: set[str] = set()
    owned_symbols: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("live owner episode malformed")
        identity = row.get("episode_id")
        try:
            valid_id = isinstance(identity, str) and str(UUID(identity)) == identity
        except ValueError:
            valid_id = False
        if (not valid_id or identity in ids or row.get("symbol") not in symbols
                or row.get("account_name") not in expected_accounts
                or row.get("state") not in ACTIVE):
            raise ValueError("live owner episode identity unproven")
        quantities = []
        for key in ("filled_quantity", "exited_quantity"):
            raw = row.get(key)
            if not isinstance(raw, str) or len(raw) > 80:
                raise ValueError("live owner quantity missing")
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError("live owner quantity invalid") from exc
            if not value.is_finite() or value < 0:
                raise ValueError("live owner quantity invalid")
            quantities.append(value)
        if quantities[1] > quantities[0]:
            raise ValueError("live owner exit exceeds own fill")
        ids.add(identity)
        owned_symbols.add(row["symbol"])
    if owned_symbols != symbols:
        raise ValueError("live quote symbol has no owned episode")
    return {"status": "ADMITTED_OWNER_SCOPE_ONLY", "owner": OWNER,
            "episode_count": len(rows), "quote_symbol_count": len(symbols),
            "mode": "opens_enabled" if enabled else "owned_exit_or_idle_only"}
