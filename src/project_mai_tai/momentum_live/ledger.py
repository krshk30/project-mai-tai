from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.persistence import LivePersistence

ET = ZoneInfo("America/New_York")


def _recorded_fill_time(value, now):
    if not isinstance(value, datetime):
        return None
    # SQLite returns naive timestamps for the same UTC-backed column.
    stamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return stamp if datetime(1970, 1, 1, tzinfo=UTC) < stamp <= now else None


def load_live_ledger(session_factory, day) -> dict:
    """Read only the new strategy's broker fills; no order-price cycle projection."""
    with session_factory() as session:
        now = datetime.now(UTC)
        rows = session.execute(select(Fill, BrokerOrder, BrokerAccount.name)
            .join(BrokerOrder, BrokerOrder.id == Fill.order_id)
            .join(BrokerAccount, BrokerAccount.id == Fill.broker_account_id)
            .join(Strategy, Strategy.id == Fill.strategy_id)
            .where(Strategy.code == STRATEGY_CODE)
            .order_by(Fill.id).limit(8193)).all()
        if len(rows) > 8192:
            raise Refusal("live_fill_ledger_history_limit_UNKNOWN")
        episodes = {}
        for fill, order, account in rows:
            metadata = (fill.payload or {}).get("metadata") or order.payload or {}
            identity = str(metadata.get("episode_id", ""))
            try:
                UUID(identity)
            except ValueError as exc:
                raise Refusal("live_fill_episode_identity_UNKNOWN") from exc
            if (fill.strategy_id != order.strategy_id or fill.broker_account_id != order.broker_account_id
                    or fill.symbol != order.symbol or fill.side != order.side
                    or (fill.payload or {}).get("momlive_fill_source") != "broker"
                    or fill.quantity <= 0 or fill.price <= 0 or not account.startswith("live:")):
                raise Refusal("live_fill_provenance_UNKNOWN")
            time_key = "momlive_first_fill_ns" if fill.side == "buy" else "momlive_last_exit_fill_ns"
            trusted_stamp = (order.payload or {}).get(time_key)
            if trusted_stamp is None and fill.side != "buy":
                raise Refusal("live_fill_execution_time_UNKNOWN")
            stamp = datetime.fromtimestamp(int(trusted_stamp)/1e9, UTC) if trusted_stamp is not None else None
            value = LivePersistence.fill_value(fill)
            if not value.is_finite() or value <= 0:
                raise Refusal("live_fill_value_UNKNOWN")
            group = episodes.setdefault(identity, {"symbol": fill.symbol, "account": account,
                "buy_qty": Decimal(0), "buy_value": Decimal(0), "sell_qty": Decimal(0),
                "sell_value": Decimal(0), "entry": None, "entry_time_unknown": False,
                "recorded_entry": None, "recorded_entry_unknown": False,
                "exit": None, "exit_reason": ""})
            if group["symbol"] != fill.symbol or group["account"] != account:
                raise Refusal("live_episode_binding_CHANGED")
            if fill.side == "buy":
                group["buy_qty"] += fill.quantity
                group["buy_value"] += value
                recorded = _recorded_fill_time(fill.filled_at, now)
                if recorded is None:
                    group["recorded_entry_unknown"] = True
                else:
                    group["recorded_entry"] = min(group["recorded_entry"], recorded) if group["recorded_entry"] else recorded
                if stamp is None:
                    group["entry_time_unknown"] = True
                else:
                    group["entry"] = min(group["entry"], stamp) if group["entry"] else stamp
            elif fill.side == "sell":
                group["sell_qty"] += fill.quantity
                group["sell_value"] += value
                group["exit"] = max(group["exit"], stamp) if group["exit"] else stamp
                group["exit_reason"] = str(metadata.get("exit_reason") or "broker_fill")
            else:
                raise Refusal("live_fill_side_UNKNOWN")
        closed, positions = [], []
        for identity, group in episodes.items():
            bought, sold = group["buy_qty"], group["sell_qty"]
            if (bought <= 0 or sold > bought
                    or group["exit"] and group["entry"] and group["exit"] < group["entry"]):
                raise Refusal("live_fill_balance_UNKNOWN")
            row = {"episode_id": identity, "ticker": group["symbol"], "symbol": group["symbol"],
                "account_name": group["account"], "quantity": str(bought),
                "entry_time": (group["recorded_entry"].astimezone(ET).isoformat()
                               if group["recorded_entry"] and not group["recorded_entry_unknown"] else "UNKNOWN"),
                "entry_time_status": "UNKNOWN" if group["recorded_entry_unknown"] else "RECORDED_FILL_TIME",
                "entry_time_source": "owned_buy_fill.filled_at",
                "first_execution_time": (group["entry"].astimezone(ET).isoformat()
                                         if group["entry"] and not group["entry_time_unknown"] else "UNKNOWN"),
                "first_execution_time_status": "UNKNOWN" if group["entry_time_unknown"] else "BROKER_EXECUTION",
                "entry_price": str(group["buy_value"]/bought), "remaining_quantity": str(bought-sold)}
            if sold == bought:
                if group["exit"].astimezone(ET).date() == day:
                    closed.append({**row, "exit_time": group["exit"].astimezone(ET).isoformat(),
                        "exit_price": str(group["sell_value"]/sold),
                        "pnl": str(group["sell_value"]-group["buy_value"]),
                        "pnl_pct": str((group["sell_value"]-group["buy_value"])/group["buy_value"]*Decimal(100)),
                        "exit_reason": group["exit_reason"]})
            else:
                positions.append(row)
        return {"strategy_code": STRATEGY_CODE, "status": "READABLE", "positions": positions,
            "closed_today": closed, "daily_pnl": str(sum((Decimal(row["pnl"]) for row in closed), Decimal(0))),
            "source": "strategy_owned_broker_fills", "fills_read": len(rows),
            "unknown_entry_time_episodes": sum(group["recorded_entry_unknown"] for group in episodes.values()),
            "unknown_first_execution_time_episodes": sum(group["entry_time_unknown"] for group in episodes.values())}
