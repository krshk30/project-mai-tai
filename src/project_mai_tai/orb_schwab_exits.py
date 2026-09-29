"""ORB exits: fill-time gateway body, completed Schwab ATR, exact broker fill.

The paper calculation helpers are shared; paper positions and paper fills are not.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, Fill, Strategy, StrategyBarHistory, SystemIncident, VirtualPosition,
)
from project_mai_tai.orb_paper_lifecycle import (
    PAPER_MIN_BREAK_BODY_PCT, compute_paper_atr_trail, forming_bar_body_pct,
)
from project_mai_tai.strategy_core.orb_intrabar import OrbBar

CONTEXT_KEY = "orb_schwab_strategy_exit"
BODY_REASON = "BREAK_BAR_BODY_UNDER_45_PCT"
ATR_REASON = "ATR_TURNED_PURPLE_AT_BAR_CLOSE"
BODY_SOURCE = "ORB_GATEWAY_TRADE_TICKS"
ATR_SOURCE = "SCHWAB_1M_V2_COMPLETED_BARS"
_ET = ZoneInfo("America/New_York")


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def confirmed_entry_time(fill: Fill | None) -> datetime | None:
    if fill is None or not fill.broker_fill_id:
        return None
    metadata = (fill.payload or {}).get("metadata") or {}
    if metadata.get("orb_entry_fill_time_source") != "execution_leg":
        return None
    try:
        at = datetime.fromisoformat(metadata["orb_entry_first_fill_at"])
        return at.astimezone(UTC) if at.tzinfo is not None else None
    except (KeyError, ValueError, TypeError):
        return None


def bar_payload(bar: OrbBar) -> dict:
    return {"at": bar.timestamp.isoformat(), "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": bar.volume}


def decode_bar(row: dict) -> OrbBar:
    at = datetime.fromisoformat(row["at"])
    values = [float(row[key]) for key in ("open", "high", "low", "close", "volume")]
    if at.tzinfo is None or any(not isfinite(value) for value in values):
        raise ValueError("invalid_bar_evidence")
    o, h, low, c, v = values
    if not 0 < low <= min(o, c) <= max(o, c) <= h or v < 0:
        raise ValueError("invalid_bar_prices")
    return OrbBar(timestamp=at, open=o, high=h, low=low, close=c, volume=v)


@dataclass
class OrbExitTape:
    started_at: datetime
    ticks: deque = field(default_factory=deque)
    truncated_through: datetime | None = None
    latest_trade_at: datetime | None = None
    # Bounded intraminute history is needed only until the entry fill is observed.
    max_ticks: int = 20000

    def trade(self, at: datetime, price: float, size: float) -> None:
        if at.tzinfo is None or not isfinite(price) or price <= 0 or not isfinite(size) or size < 0:
            return
        self.ticks.append((at, price, size))
        newest = max(at, self.latest_trade_at or at)
        self.latest_trade_at = newest
        while self.ticks and (len(self.ticks) > self.max_ticks or self.ticks[0][0] < newest - timedelta(minutes=2)):
            removed = self.ticks.popleft()[0]
            self.truncated_through = max(removed, self.truncated_through or removed)

    def body_at(self, fill_at: datetime) -> dict | None:
        minute = fill_at.replace(second=0, microsecond=0)
        if self.latest_trade_at is None or self.latest_trade_at < fill_at:
            return None
        if self.started_at > minute or (
            self.truncated_through is not None and self.truncated_through >= minute
        ):
            return None
        prefix = [(at, price, size) for at, price, size in self.ticks if minute <= at <= fill_at]
        if not prefix:
            return None
        prefix.sort(key=lambda item: item[0])
        prices = [price for _at, price, _size in prefix]
        return {"open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1],
                "last_trade_at": prefix[-1][0].isoformat(), "fill_at": fill_at.isoformat(),
                "trade_count": len(prefix)}

    def evidence(
        self, fill_id: str, fill_at: datetime, now: datetime, *, atr_bars: list[OrbBar],
        atr_status: str, prior: dict | None = None,
    ) -> dict:
        prior = prior or {}
        body = prior.get("body") if prior.get("fill_id") == fill_id else None
        body = body or self.body_at(fill_at)
        result = {"body_source": BODY_SOURCE, "atr_source": ATR_SOURCE,
                  "fill_id": fill_id, "fill_at": fill_at.isoformat(), "body": body,
                  "atr_bars": [bar_payload(bar) for bar in atr_bars],
                  "atr_status": atr_status, "atr_asof": now.isoformat()}
        reason, decision_at = exit_signal(result, now)
        result.update(reason=reason, decision_at=decision_at.isoformat() if decision_at else None)
        return result


def exit_signal(context: dict, now: datetime) -> tuple[str | None, datetime | None]:
    """Recompute the two paper rules; never trust a caller's proposed reason."""
    if context.get("body_source") != BODY_SOURCE:
        raise ValueError("wrong_exit_evidence_source")
    fill_at = datetime.fromisoformat(context["fill_at"])
    if fill_at.tzinfo is None or fill_at > now:
        raise ValueError("invalid_fill_time")
    body = context.get("body")
    if body is not None:
        last_at = datetime.fromisoformat(body["last_trade_at"])
        if not fill_at.replace(second=0, microsecond=0) <= last_at <= fill_at:
            raise ValueError("body_contains_wrong_minute_or_post_fill_trade")
        if datetime.fromisoformat(body["fill_at"]) != fill_at:
            raise ValueError("body_fill_time_mismatch")
        bar = decode_bar({**body, "at": fill_at.isoformat(), "volume": 0})
        percentage = forming_bar_body_pct(open_price=bar.open, high=bar.high, low=bar.low, close=bar.close)
        if percentage < PAPER_MIN_BREAK_BODY_PCT:
            return BODY_REASON, fill_at
    if context.get("atr_status") != "complete":
        return None, None
    if context.get("atr_source") != ATR_SOURCE:
        raise ValueError("wrong_atr_source")
    asof = datetime.fromisoformat(context["atr_asof"])
    if asof.tzinfo is None or asof > now:
        raise ValueError("invalid_atr_evaluation_time")
    bars = [decode_bar(row) for row in context.get("atr_bars", [])]
    if any(bar.timestamp + timedelta(minutes=1) > asof for bar in bars):
        raise ValueError("atr_requires_completed_bars")
    if not bars or bars[-1].timestamp != asof.replace(second=0, microsecond=0) - timedelta(minutes=1):
        raise ValueError("atr_last_closed_bar_missing")
    if any(right.timestamp <= left.timestamp for left, right in zip(bars, bars[1:])):
        raise ValueError("atr_bars_not_ordered")
    for bar, result in zip(bars, compute_paper_atr_trail(bars), strict=True):
        decision_at = bar.timestamp + timedelta(minutes=1)
        if result["flip"] == "SELL" and fill_at <= decision_at:
            return ATR_REASON, decision_at
    return None, None


def open_entries(factory, account: str) -> list[dict]:
    with factory() as session:
        entries = session.scalars(select(BrokerOrder).join(Strategy).join(
            BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
        ).join(VirtualPosition, (
            (VirtualPosition.strategy_id == BrokerOrder.strategy_id)
            & (VirtualPosition.broker_account_id == BrokerOrder.broker_account_id)
            & (VirtualPosition.symbol == BrokerOrder.symbol)
        )).where(Strategy.code == "orb_schwab", BrokerAccount.name == account,
                 BrokerOrder.side == "buy", VirtualPosition.quantity > 0).order_by(
                     BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc(),
                 )).all()
        result = []
        seen = set()
        for entry in entries:
            # A virtual position is symbol-scoped, not entry-scoped. Never attach
            # yesterday's closed trip to today's positive position.
            if entry.symbol in seen:
                continue
            seen.add(entry.symbol)
            fill = session.scalar(select(Fill).where(
                Fill.order_id == entry.id, Fill.side == "buy", Fill.broker_fill_id.is_not(None),
                Fill.quantity > 0,
            ).order_by(Fill.filled_at, Fill.id).limit(1))
            result.append({"entry_id": str(entry.id), "symbol": entry.symbol,
                           "fill_id": str(fill.id) if fill else None,
                           "fill_at": confirmed_entry_time(fill),
                           "close_claimed": bool((entry.payload or {}).get("orb_schwab_eod_close")),
                           "context": dict((entry.payload or {}).get(CONTEXT_KEY) or {})})
        return result


def schwab_completed_atr_bars(factory, symbol: str, now: datetime) -> tuple[list[OrbBar], str]:
    """Read only v2's Schwab bars, same daily 07:00 history boundary as MACD.

    No gateway/REST call is made here. Illiquid gaps retain the shared Wilder's
    gap treatment; a missing newest minute is UNKNOWN, not a purple signal.
    """
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    start = now.astimezone(_ET).replace(hour=7, minute=0, second=0, microsecond=0).astimezone(UTC)
    try:
        with factory() as session:
            rows = session.scalars(select(StrategyBarHistory).where(
                StrategyBarHistory.strategy_code == "schwab_1m_v2",
                StrategyBarHistory.symbol == symbol, StrategyBarHistory.interval_secs == 60,
                StrategyBarHistory.bar_time >= start, StrategyBarHistory.bar_time <= last,
            ).order_by(StrategyBarHistory.bar_time).limit(650)).all()
            bars = [decode_bar({"at": aware(row.bar_time).isoformat(), "open": row.open_price,
                                "high": row.high_price, "low": row.low_price,
                                "close": row.close_price, "volume": row.volume}) for row in rows]
            if any(row.source not in {"live", "rest"} for row in rows):
                return [], "unrecognized_schwab_bar_provenance"
    except Exception:
        return [], "schwab_bar_read_unavailable"
    if not bars or bars[-1].timestamp != last:
        return [], "missing_last_closed_schwab_bar"
    if compute_paper_atr_trail(bars)[-1]["state"] is None:
        return [], "insufficient_schwab_atr_history"
    return bars, "complete"


def save_context(factory, entry_id: str, context: dict) -> None:
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.id == UUID(entry_id)).with_for_update())
        if entry is None:
            raise ValueError("owned_entry_missing")
        previous = dict((entry.payload or {}).get(CONTEXT_KEY) or {})
        if previous.get("reason"):
            return
        entry.payload = {**(entry.payload or {}), CONTEXT_KEY: context}
        if context.get("body") is None or context.get("atr_status") != "complete":
            exists = session.scalar(select(SystemIncident.id).where(
                SystemIncident.payload["source"].as_string() == "orb_schwab_exit_evidence",
                SystemIncident.payload["entry_order_id"].as_string() == entry_id,
            ))
            if exists is None:
                session.add(SystemIncident(
                    service_name="orb-schwab", severity="critical", status="open",
                    title=f"ORB strategy-exit evidence unavailable: {entry.symbol}",
                    payload={"source": "orb_schwab_exit_evidence", "entry_order_id": entry_id,
                             "symbol": entry.symbol,
                             "reason": "BODY_AT_BROKER_FILL_UNKNOWN" if context.get("body") is None else context.get("atr_status"),
                             "native_protection": "not_cancelled"},
                ))
