from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class TradeCoachConfig:
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    request_timeout_seconds: int = 8
    context_bars: int = 20
    review_bars_after_exit: int = 20
    max_similar_trades: int = 5
    review_type: str = "post_trade"


class EpisodeBarSnapshot(BaseModel):
    bar_time: datetime
    interval_secs: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    trade_count: int
    position_state: str = ""
    position_quantity: int = 0
    decision_status: str = ""
    decision_reason: str = ""
    decision_path: str = ""
    decision_score: str = ""
    decision_score_details: str = ""
    indicators: dict[str, Any] = Field(default_factory=dict)


class TradeEpisode(BaseModel):
    strategy_code: str
    broker_account_name: str
    symbol: str
    cycle_key: str
    entry_time: datetime
    exit_time: datetime
    path: str = ""
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    pnl_pct: float
    summary: str
    intent_ids: list[str] = Field(default_factory=list)
    primary_intent_id: str | None = None
    intents: list[dict[str, Any]] = Field(default_factory=list)
    risk_checks: list[dict[str, Any]] = Field(default_factory=list)
    orders: list[dict[str, Any]] = Field(default_factory=list)
    order_events: list[dict[str, Any]] = Field(default_factory=list)
    fills: list[dict[str, Any]] = Field(default_factory=list)
    bars_before: list[EpisodeBarSnapshot] = Field(default_factory=list)
    bars_during: list[EpisodeBarSnapshot] = Field(default_factory=list)
    bars_after: list[EpisodeBarSnapshot] = Field(default_factory=list)
    similar_reviews: list[dict[str, Any]] = Field(default_factory=list)


class TradeCoachReview(BaseModel):
    verdict: str = Field(description="good | bad | mixed | skip")
    action: str = Field(description="enter | enter_early | wait | skip | reduce | exit | hold")
    execution_timing: str = Field(description="early | on_time | late | skip")
    confidence: float = Field(ge=0.0, le=1.0)
    setup_quality: float = Field(ge=0.0, le=1.0)
    should_have_traded: bool
    key_reasons: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)
    rule_violations: list[str] = Field(default_factory=list)
    next_time: list[str] = Field(default_factory=list)
    concise_summary: str = ""
