"""10:0x: one job-start positions read per live account; no orders endpoint/hook."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
import stat

from ops.weekmaint.policy import ET, Refused, aware, number

LIMIT = 200_000


def private_json(path, *, owner_uid=0):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or info.st_mode & 0o077 or info.st_size > 32 * 1024 * 1024):
            raise Refused("private_evidence_file_permissions_or_bound")
        with os.fdopen(descriptor, "r") as stream:
            descriptor = -1
            return json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class ExistingReadCollector:
    """Caller supplies the existing configured broker multiplexer, never a new API."""

    def __init__(self, adapter, accounts, *, clock=None):
        self.adapter, self.accounts = adapter, dict(accounts)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.attempted_accounts = set()

    async def collect(self, inventory, facts):
        now = aware(self.clock())
        if facts.get("live_accounts") != self.accounts:
            raise Refused("configured_and_DB_live_account_coverage_conflict")
        if now.astimezone(ET).weekday() != 5:
            raise Refused("DAY_expiry_policy_only_applies_Saturday")
        vp, managed = {}, {}
        for item in inventory:
            row, table = item["row"], item["table"]
            key = item["account"], row["symbol"]
            if table == "virtual_positions":
                vp[key] = vp.get(key, Decimal(0)) + abs(number(row.get("quantity")))
            elif table == "oms_managed_positions":
                managed[key] = managed.get(key, Decimal(0)) + abs(number(row.get("current_quantity")))
        owned = {key: max(vp.get(key, Decimal(0)), managed.get(key, Decimal(0))) for key in vp.keys() | managed.keys()}
        result = {}
        for account, provider in self.accounts.items():
            if account in self.attempted_accounts:
                raise Refused("one_start_positions_read_already_attempted_no_retry")
            self.attempted_accounts.add(account)
            before = aware(self.clock())
            positions = await asyncio.wait_for(self.adapter.list_account_positions(account), timeout=15)
            after = aware(self.clock())
            # Schwab's adapter has no cache; use request start as a conservative
            # lower bound. Webull MUST retain its actual cached-wire age.
            observed = before
            if provider == "webull":
                target = self.adapter
                resolver = getattr(target, "_adapter_for_account", None)
                if resolver is not None:
                    target = resolver(account)
                age_reader = getattr(target, "positions_wire_read_age_seconds", None)
                age = age_reader(account) if age_reader is not None else None
                if age is None or not 0 <= float(age) <= 30:
                    raise Refused("Webull_original_positions_wire_age_UNKNOWN")
                observed = min(before, after - timedelta(seconds=float(age)))
            if not isinstance(positions, list) or len(positions) > LIMIT:
                raise Refused("positions_bound_or_shape_UNKNOWN")
            rows, seen = [], set()
            for position in positions:
                symbol = position.symbol
                if position.broker_account_name != account or not symbol or symbol in seen:
                    raise Refused("positions_account_symbol_conflict")
                seen.add(symbol)
                total = number(position.quantity)
                bot = min(abs(total), owned.get((account, symbol), Decimal(0)))
                rows.append({"symbol": symbol, "quantity": str(total), "bot_owned_quantity": str(bot),
                             "ownership_complete": True})
            result[account] = {"account": account, "provider": provider,
                "positions": {"source": "existing_positions_read", "success": True, "complete": True,
                              "read_at": observed.isoformat(), "rows": rows},
                "bot_orders": {"source": "Saturday_bot_DAY_expiry", "success": True,
                    "complete": True, "checked_at": aware(self.clock()).isoformat(), "rows": [],
                    "all_bot_orders_DAY": True, "working_bot_orders": 0}}
        return result
