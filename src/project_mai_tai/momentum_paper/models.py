from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo


MOMENTUM_ACCOUNT_NAME = "paper:momentum"
_ET = ZoneInfo("America/New_York")
MOMENTUM_STRATEGIES: dict[str, int] = {
    "momentum_30s": 30,
    "momentum_60s": 60,
}


@dataclass(frozen=True)
class TradePrint:
    symbol: str
    sip_ts_ms: int
    price: Decimal
    size: int
    trade_id: str = ""
    participant_ts_ms: int | None = None
    conditions: tuple[int, ...] = ()
    eligible: bool = True
    exclusion_reason: str = ""

    @property
    def observed_at(self) -> datetime:
        return datetime.fromtimestamp(self.sip_ts_ms / 1000, tz=UTC)

    def payload(self) -> dict[str, Any]:
        sip_at = self.observed_at
        participant_at = (
            datetime.fromtimestamp(self.participant_ts_ms / 1000, tz=UTC)
            if self.participant_ts_ms is not None
            else None
        )
        return {
            "symbol": self.symbol,
            "sip_ts_ms": self.sip_ts_ms,
            "sip_at_utc": sip_at.isoformat(),
            "sip_at_et": sip_at.astimezone(_ET).isoformat(),
            "participant_ts_ms": self.participant_ts_ms,
            "participant_at_utc": participant_at.isoformat() if participant_at else None,
            "participant_at_et": (
                participant_at.astimezone(_ET).isoformat() if participant_at else None
            ),
            "price": str(self.price),
            "size": self.size,
            "trade_id": self.trade_id,
            "conditions": list(self.conditions),
            "eligible": self.eligible,
            "exclusion_reason": self.exclusion_reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TradePrint:
        return cls(
            symbol=str(payload["symbol"]),
            sip_ts_ms=int(payload["sip_ts_ms"]),
            participant_ts_ms=(
                int(payload["participant_ts_ms"])
                if payload.get("participant_ts_ms") is not None
                else None
            ),
            price=Decimal(str(payload["price"])),
            size=int(payload.get("size", 0)),
            trade_id=str(payload.get("trade_id", "")),
            conditions=tuple(int(value) for value in payload.get("conditions", [])),
            eligible=bool(payload.get("eligible", False)),
            exclusion_reason=str(payload.get("exclusion_reason", "")),
        )


@dataclass(frozen=True)
class MomentumTapeRecord:
    event_key: str
    logical_id: str
    strategy_code: str
    event_type: str
    session_date: date
    symbol: str
    observed_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MomentumGrade:
    verdict: str
    sessions: int
    filled_gradable: int
    wins: int
    win_rate_pct: Decimal | None
    average_pnl_pct: Decimal | None
    sample_met: bool
    win_rate_met: bool | None
    average_met: bool | None
    drop_session_met: bool | None
    drop_symbol_met: bool | None
    counts: dict[str, int]
    reduced_samples: tuple[dict[str, Any], ...] = ()
