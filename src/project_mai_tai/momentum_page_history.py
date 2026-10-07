from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import MomentumPaperEvent


MAX_TERMINAL_ROWS = 4096


def load_completed_today(
    session_factory: sessionmaker[Session], strategy_code: str, session_date: date,
) -> tuple[list[dict[str, Any]], Decimal]:
    if strategy_code not in {"momentum_30s", "momentum_60s"}:
        raise ValueError("Momentum page history is limited to the two Momentum strategies")
    with session_factory() as session:
        rows = session.scalars(
            select(MomentumPaperEvent)
            .where(
                MomentumPaperEvent.session_date == session_date,
                MomentumPaperEvent.strategy_code == strategy_code,
                MomentumPaperEvent.event_type.in_(("FILLED", "EXITED", "FINAL")),
            )
            .order_by(MomentumPaperEvent.observed_at, MomentumPaperEvent.created_at, MomentumPaperEvent.event_key)
            .limit(MAX_TERMINAL_ROWS + 1)
        ).all()
        if len(rows) > MAX_TERMINAL_ROWS:
            raise RuntimeError("Momentum page terminal-row bound exceeded; refusing partial P&L")
        # FILLED, EXITED and FINAL describe one trade, not three completed trades.
        trades: dict[str, dict[str, Any]] = {}
        for row in rows:
            trade = trades.setdefault(row.logical_id, {"symbol": row.symbol})
            trade.update(dict(row.payload or {}))

    closed: list[dict[str, Any]] = []
    total = Decimal("0")
    for trade in trades.values():
        fill, exit_ = trade.get("fill"), trade.get("exit")
        if not fill or not exit_:
            continue
        quantity = Decimal(str(trade["quantity"]))
        entry_price, exit_price = Decimal(str(fill["price"])), Decimal(str(exit_["price"]))
        pnl = Decimal(str(trade["pnl"])) if trade.get("pnl") is not None else (exit_price - entry_price) * quantity
        pnl_pct = (
            Decimal(str(trade["pnl_pct"])) if trade.get("pnl_pct") is not None
            else (exit_price - entry_price) / entry_price * 100
        )
        total += pnl
        closed.append({
            "ticker": trade["symbol"], "symbol": trade["symbol"], "quantity": float(quantity),
            "entry_price": str(entry_price), "exit_price": str(exit_price),
            "entry_time": datetime.fromtimestamp(int(fill["sip_ts_ms"]) / 1000, tz=UTC).isoformat(),
            "exit_time": datetime.fromtimestamp(int(exit_["sip_ts_ms"]) / 1000, tz=UTC).isoformat(),
            "pnl": str(pnl), "pnl_pct": str(pnl_pct), "exit_reason": trade.get("exit_reason"),
        })
    return closed, total
