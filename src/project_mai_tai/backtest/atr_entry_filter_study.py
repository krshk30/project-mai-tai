"""Research entry filters for the +1% target / -2% floor ATR bracket study.

The candidate population is the output of ``atr_bracket_study``.  Features are joined strictly
from information available before each entry.  Future quote data is kept in separate audit fields
and is never admitted to a selector.  The primary evaluation is leave-one-session-out: every day is
ranked by a model trained on the other six days, with at most six entries selected.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.backtest.data import DbMarketDataSource, Quote, SchwabBar

EASTERN = ZoneInfo("America/New_York")
POLICY = "target+1_floor-2"
MAX_DAILY_SELECTION = 6


@dataclass(frozen=True)
class PolicyTrade:
    session_day_et: str
    symbol: str
    entry_slot: str
    entry_mode: str
    scanner_window_start: datetime
    entry_ts: datetime
    entry_px: float
    exit_reason: str
    ret_pct: float

    @property
    def won(self) -> bool:
        return self.exit_reason == "target"


@dataclass(frozen=True)
class IndicatorBar:
    ts: datetime
    volume: float
    indicators: dict[str, object]


@dataclass(frozen=True)
class ConfirmSnapshot:
    ts: datetime
    rank_score: float | None
    day_volume: float | None
    float_used: float | None
    change_pct: float | None


@dataclass
class EntryCandidate:
    session_day_et: str
    symbol: str
    entry_ts: str
    entry_slot: str
    entry_mode: str
    entry_px: float
    won: bool
    ret_pct: float
    features: dict[str, float | None]
    first_bid_ret_pct: float | None
    future_mfe_pct: float | None
    future_mae_pct: float | None
    future_reached_plus_1: bool
    future_reached_plus_2: bool
    future_reached_plus_5: bool
    mae_before_plus_1_pct: float | None
    mae_before_plus_2_pct: float | None
    mae_before_plus_5_pct: float | None
    seconds_to_plus_1: float | None
    seconds_to_plus_2: float | None
    seconds_to_plus_5: float | None


@dataclass(frozen=True)
class FutureAudit:
    first_bid_ret_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    mae_before_plus_1_pct: float | None
    mae_before_plus_2_pct: float | None
    mae_before_plus_5_pct: float | None
    seconds_to_plus_1: float | None
    seconds_to_plus_2: float | None
    seconds_to_plus_5: float | None

    @property
    def reached_plus_1(self) -> bool:
        return self.seconds_to_plus_1 is not None

    @property
    def reached_plus_2(self) -> bool:
        return self.seconds_to_plus_2 is not None

    @property
    def reached_plus_5(self) -> bool:
        return self.seconds_to_plus_5 is not None


MODEL_FEATURES = (
    "log_entry_price",
    "spread_pct",
    "minutes_since_0700",
    "scanner_age_min",
    "confirm_rank_score",
    "log_confirm_day_volume",
    "log_float_used",
    "confirm_change_pct",
    "vwap_dist_pct",
    "macd_pct",
    "histogram_pct",
    "macd_delta_pct",
    "macd_increasing",
    "macd_above_signal",
    "poly_volume_ratio_20",
    "poly_volume_5_vs_prev_5",
    "schwab_volume_ratio_5",
    "is_reclaim",
    "is_reactive",
    "entry_sequence",
)

FEATURE_FAMILIES = {
    "all": MODEL_FEATURES,
    "volume": (
        "log_confirm_day_volume",
        "poly_volume_ratio_20",
        "poly_volume_5_vs_prev_5",
        "schwab_volume_ratio_5",
    ),
    "macd": (
        "macd_pct",
        "histogram_pct",
        "macd_delta_pct",
        "macd_increasing",
        "macd_above_signal",
    ),
    "vwap": ("vwap_dist_pct",),
    "market_context": (
        "log_entry_price",
        "spread_pct",
        "minutes_since_0700",
        "scanner_age_min",
        "confirm_rank_score",
        "confirm_change_pct",
        "vwap_dist_pct",
        "macd_pct",
        "histogram_pct",
        "macd_delta_pct",
        "poly_volume_ratio_20",
        "poly_volume_5_vs_prev_5",
        "schwab_volume_ratio_5",
    ),
    "path_only": ("is_reclaim", "is_reactive", "entry_sequence"),
}


class DbFeatureSource:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory
        self._market = DbMarketDataSource(session_factory)

    def indicator_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[IndicatorBar]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT bar_time, volume, indicators FROM strategy_bar_history "
                    "WHERE strategy_code='polygon_30s' AND interval_secs=30 "
                    "AND symbol=:symbol AND bar_time>=:start AND bar_time<:end "
                    "ORDER BY bar_time"
                ),
                {"symbol": symbol, "start": start, "end": end},
            ).all()
        return [
            IndicatorBar(ts=ts, volume=float(volume or 0), indicators=indicators or {})
            for ts, volume, indicators in rows
        ]

    def confirms(
        self, symbol: str, trade_date: date, start: datetime, end: datetime
    ) -> list[ConfirmSnapshot]:
        with self._sf() as session:
            rows = session.execute(
                text(
                    "SELECT event_at, rank_score, day_volume, float_used, change_pct "
                    "FROM scanner_confirmed_events WHERE trade_date=:trade_date "
                    "AND symbol=:symbol AND event_type='CONFIRM' "
                    "AND abs(extract(epoch from (created_at-event_at)))<=120 "
                    "AND event_at>=:start AND event_at<:end ORDER BY event_at"
                ),
                {
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "start": start,
                    "end": end,
                },
            ).all()
        return [
            ConfirmSnapshot(
                ts=ts,
                rank_score=_optional_float(rank),
                day_volume=_optional_float(volume),
                float_used=_optional_float(float_used),
                change_pct=_optional_float(change),
            )
            for ts, rank, volume, float_used, change in rows
        ]

    def schwab_bars(self, symbol: str, start: datetime, end: datetime) -> list[SchwabBar]:
        return self._market.schwab_bars(symbol, start, end)

    def schwab_quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        return self._market.schwab_quotes(symbol, start, end)

    def exit_quotes(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        return self._market.quotes(symbol, start, end)


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _log_value(value: float | None) -> float | None:
    if value is None or value < 0:
        return None
    return math.log10(value + 1.0)


def _indicator_value(bar: IndicatorBar | None, key: str) -> float | None:
    if bar is None:
        return None
    return _optional_float(bar.indicators.get(key))


def _latest_completed_indicator(
    bars: Sequence[IndicatorBar], entry_ts: datetime
) -> tuple[IndicatorBar | None, int]:
    # Bar timestamps are bucket starts.  Only admit a 30-second bucket after it fully closed.
    eligible = [bar for bar in bars if bar.ts + timedelta(seconds=30) <= entry_ts]
    if not eligible:
        return None, -1
    return eligible[-1], len(eligible) - 1


def _ratio(value: float, baseline: float) -> float | None:
    if baseline <= 0:
        return None
    return value / baseline


def _volume_features(bars: Sequence[IndicatorBar], index: int) -> tuple[float | None, float | None]:
    if index < 0:
        return None, None
    current = float(bars[index].volume)
    prior_20 = [float(bar.volume) for bar in bars[max(0, index - 20) : index]]
    ratio_20 = _ratio(current, statistics.fmean(prior_20)) if prior_20 else None
    recent_5 = [float(bar.volume) for bar in bars[max(0, index - 4) : index + 1]]
    previous_5 = [float(bar.volume) for bar in bars[max(0, index - 9) : max(0, index - 4)]]
    ratio_5 = _ratio(sum(recent_5), sum(previous_5)) if previous_5 else None
    return ratio_20, ratio_5


def _schwab_volume_ratio(bars: Sequence[SchwabBar], entry_ts: datetime) -> float | None:
    entry_ms = int(entry_ts.timestamp() * 1000)
    eligible = [bar for bar in bars if int(bar.ts) + 2 <= entry_ms]
    if len(eligible) < 2:
        return None
    previous = eligible[-6:-1]
    return _ratio(float(eligible[-1].volume), statistics.fmean(bar.volume for bar in previous))


def _latest_confirm(
    confirms: Sequence[ConfirmSnapshot], entry_ts: datetime
) -> ConfirmSnapshot | None:
    eligible = [confirm for confirm in confirms if confirm.ts <= entry_ts]
    return eligible[-1] if eligible else None


def _entry_quote(quotes: Sequence[Quote], entry_ts: datetime) -> Quote | None:
    exact = [quote for quote in quotes if quote.ts == entry_ts]
    if exact:
        return exact[-1]
    prior = [quote for quote in quotes if quote.ts <= entry_ts]
    if not prior or (entry_ts - prior[-1].ts).total_seconds() > 5:
        return None
    return prior[-1]


def audit_future_quotes(
    quotes: Sequence[Quote], entry_ts: datetime, entry_px: float
) -> FutureAudit:
    future = [quote for quote in quotes if quote.ts > entry_ts and quote.bid > 0]
    if not future:
        return FutureAudit(None, None, None, None, None, None, None, None, None)
    returns = [
        (quote.ts, (float(quote.bid) - entry_px) / entry_px * 100.0) for quote in future
    ]

    def first_passage(threshold: float) -> tuple[float | None, float | None]:
        running_min = 0.0
        for ts, value in returns:
            running_min = min(running_min, value)
            if value >= threshold:
                return running_min, (ts - entry_ts).total_seconds()
        return None, None

    mae_1, seconds_1 = first_passage(1.0)
    mae_2, seconds_2 = first_passage(2.0)
    mae_5, seconds_5 = first_passage(5.0)
    values = [value for _, value in returns]
    return FutureAudit(
        first_bid_ret_pct=values[0],
        mfe_pct=max(values),
        mae_pct=min(values),
        mae_before_plus_1_pct=mae_1,
        mae_before_plus_2_pct=mae_2,
        mae_before_plus_5_pct=mae_5,
        seconds_to_plus_1=seconds_1,
        seconds_to_plus_2=seconds_2,
        seconds_to_plus_5=seconds_5,
    )


def load_policy_trades(path: Path, policy: str = POLICY) -> list[PolicyTrade]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        PolicyTrade(
            session_day_et=row["session_day_et"],
            symbol=row["symbol"],
            entry_slot=row["entry_slot"],
            entry_mode=row["entry_mode"],
            scanner_window_start=datetime.fromisoformat(row["scanner_window_start"]),
            entry_ts=datetime.fromisoformat(row["entry_ts"]),
            entry_px=float(row["entry_px"]),
            exit_reason=row["exit_reason"],
            ret_pct=float(row["ret_pct"]),
        )
        for row in rows
        if row["policy"] == policy
    ]


def build_candidates(source: DbFeatureSource, trades: Sequence[PolicyTrade]) -> list[EntryCandidate]:
    grouped: dict[tuple[str, str], list[PolicyTrade]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.session_day_et, trade.symbol)].append(trade)

    candidates: list[EntryCandidate] = []
    for (day_text, symbol), symbol_trades in sorted(grouped.items()):
        day = datetime.strptime(day_text, "%Y-%m-%d").replace(tzinfo=EASTERN)
        start = day.replace(hour=4)
        end = day.replace(hour=16)
        indicator_bars = source.indicator_bars(symbol, start, end)
        confirms = source.confirms(symbol, day.date(), start, end)
        schwab_bars = source.schwab_bars(symbol, start, end)
        schwab_quotes = source.schwab_quotes(symbol, start, end)
        exit_quotes = source.exit_quotes(symbol, start, end)

        for sequence, trade in enumerate(sorted(symbol_trades, key=lambda item: item.entry_ts), 1):
            indicator, indicator_index = _latest_completed_indicator(
                indicator_bars, trade.entry_ts
            )
            confirm = _latest_confirm(confirms, trade.entry_ts)
            quote = _entry_quote(schwab_quotes, trade.entry_ts)
            indicator_price = _indicator_value(indicator, "price")
            vwap = _indicator_value(indicator, "selected_vwap") or _indicator_value(
                indicator, "vwap"
            )
            macd = _indicator_value(indicator, "macd")
            histogram = _indicator_value(indicator, "histogram")
            macd_delta = _indicator_value(indicator, "macd_delta")
            volume_ratio_20, volume_5_ratio = _volume_features(
                indicator_bars, indicator_index
            )
            entry_et = trade.entry_ts.astimezone(EASTERN)
            seven = entry_et.replace(hour=7, minute=0, second=0, microsecond=0)
            future_audit = audit_future_quotes(
                exit_quotes, trade.entry_ts, trade.entry_px
            )
            features: dict[str, float | None] = {
                "log_entry_price": _log_value(trade.entry_px),
                "spread_pct": (
                    (float(quote.ask) - float(quote.bid)) / float(quote.ask) * 100.0
                    if quote is not None and quote.ask > 0
                    else None
                ),
                "minutes_since_0700": (entry_et - seven).total_seconds() / 60.0,
                "scanner_age_min": (
                    (trade.entry_ts - confirm.ts).total_seconds() / 60.0
                    if confirm is not None
                    else None
                ),
                "confirm_rank_score": confirm.rank_score if confirm else None,
                "log_confirm_day_volume": _log_value(confirm.day_volume if confirm else None),
                "log_float_used": _log_value(confirm.float_used if confirm else None),
                "confirm_change_pct": confirm.change_pct if confirm else None,
                "vwap_dist_pct": (
                    (trade.entry_px - vwap) / vwap * 100.0 if vwap is not None and vwap > 0 else None
                ),
                "macd_pct": (
                    macd / indicator_price * 100.0
                    if macd is not None and indicator_price is not None and indicator_price > 0
                    else None
                ),
                "histogram_pct": (
                    histogram / indicator_price * 100.0
                    if histogram is not None
                    and indicator_price is not None
                    and indicator_price > 0
                    else None
                ),
                "macd_delta_pct": (
                    macd_delta / indicator_price * 100.0
                    if macd_delta is not None
                    and indicator_price is not None
                    and indicator_price > 0
                    else None
                ),
                "macd_increasing": (
                    float(bool(indicator.indicators.get("macd_increasing")))
                    if indicator is not None
                    else None
                ),
                "macd_above_signal": (
                    float(bool(indicator.indicators.get("macd_above_signal")))
                    if indicator is not None
                    else None
                ),
                "poly_volume_ratio_20": volume_ratio_20,
                "poly_volume_5_vs_prev_5": volume_5_ratio,
                "schwab_volume_ratio_5": _schwab_volume_ratio(schwab_bars, trade.entry_ts),
                "is_reclaim": float(trade.entry_slot == "reclaim"),
                "is_reactive": float(trade.entry_mode == "reactive"),
                "entry_sequence": float(sequence),
            }
            candidates.append(
                EntryCandidate(
                    session_day_et=trade.session_day_et,
                    symbol=trade.symbol,
                    entry_ts=trade.entry_ts.isoformat(),
                    entry_slot=trade.entry_slot,
                    entry_mode=trade.entry_mode,
                    entry_px=trade.entry_px,
                    won=trade.won,
                    ret_pct=trade.ret_pct,
                    features=features,
                    first_bid_ret_pct=future_audit.first_bid_ret_pct,
                    future_mfe_pct=future_audit.mfe_pct,
                    future_mae_pct=future_audit.mae_pct,
                    future_reached_plus_1=future_audit.reached_plus_1,
                    future_reached_plus_2=future_audit.reached_plus_2,
                    future_reached_plus_5=future_audit.reached_plus_5,
                    mae_before_plus_1_pct=future_audit.mae_before_plus_1_pct,
                    mae_before_plus_2_pct=future_audit.mae_before_plus_2_pct,
                    mae_before_plus_5_pct=future_audit.mae_before_plus_5_pct,
                    seconds_to_plus_1=future_audit.seconds_to_plus_1,
                    seconds_to_plus_2=future_audit.seconds_to_plus_2,
                    seconds_to_plus_5=future_audit.seconds_to_plus_5,
                )
            )
        print(f"features {day_text} {symbol}: {len(symbol_trades)}", flush=True)
    return candidates


@dataclass(frozen=True)
class _FittedModel:
    features: tuple[str, ...]
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    weights: tuple[float, ...]
    bias: float

    def score(self, candidate: EntryCandidate) -> float:
        total = self.bias
        for index, name in enumerate(self.features):
            value = candidate.features.get(name)
            filled = self.medians[index] if value is None else float(value)
            normalized = (filled - self.means[index]) / self.scales[index]
            total += self.weights[index] * normalized
        return _sigmoid(total)


def _sigmoid(value: float) -> float:
    if value >= 0:
        term = math.exp(-min(value, 40.0))
        return 1.0 / (1.0 + term)
    term = math.exp(max(value, -40.0))
    return term / (1.0 + term)


def fit_logistic(
    candidates: Sequence[EntryCandidate],
    features: Sequence[str],
    *,
    l2: float = 2.0,
    steps: int = 2_000,
    learning_rate: float = 0.08,
) -> _FittedModel:
    if not candidates:
        raise ValueError("training population is empty")
    names = tuple(features)
    columns: list[list[float]] = []
    medians: list[float] = []
    means: list[float] = []
    scales: list[float] = []
    for name in names:
        observed = [float(value) for row in candidates if (value := row.features.get(name)) is not None]
        median = statistics.median(observed) if observed else 0.0
        column = [
            median if row.features.get(name) is None else float(row.features[name])
            for row in candidates
        ]
        mean = statistics.fmean(column)
        scale = statistics.pstdev(column) or 1.0
        medians.append(median)
        means.append(mean)
        scales.append(scale)
        columns.append([(value - mean) / scale for value in column])

    labels = [float(row.won) for row in candidates]
    rate = min(1.0 - 1e-6, max(1e-6, statistics.fmean(labels)))
    bias = math.log(rate / (1.0 - rate))
    weights = [0.0] * len(names)
    n = float(len(candidates))
    for step in range(steps):
        bias_gradient = 0.0
        gradients = [0.0] * len(names)
        for row_index, label in enumerate(labels):
            linear = bias + sum(
                weights[column_index] * columns[column_index][row_index]
                for column_index in range(len(names))
            )
            error = _sigmoid(linear) - label
            bias_gradient += error
            for column_index in range(len(names)):
                gradients[column_index] += error * columns[column_index][row_index]
        rate_step = learning_rate / math.sqrt(1.0 + step / 250.0)
        bias -= rate_step * bias_gradient / n
        for column_index in range(len(names)):
            penalty = l2 * weights[column_index] / n
            weights[column_index] -= rate_step * (gradients[column_index] / n + penalty)

    return _FittedModel(
        features=names,
        medians=tuple(medians),
        means=tuple(means),
        scales=tuple(scales),
        weights=tuple(weights),
        bias=bias,
    )


def leave_one_day_out(
    candidates: Sequence[EntryCandidate],
    features: Sequence[str],
    *,
    daily_cap: int = MAX_DAILY_SELECTION,
    per_symbol_cap: int | None = None,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    days = sorted({candidate.session_day_et for candidate in candidates})
    for held_day in days:
        train = [candidate for candidate in candidates if candidate.session_day_et != held_day]
        test = [candidate for candidate in candidates if candidate.session_day_et == held_day]
        model = fit_logistic(train, features)
        ranked = sorted(
            ((model.score(candidate), candidate) for candidate in test),
            key=lambda item: (item[0], item[1].entry_ts),
            reverse=True,
        )
        chosen: list[tuple[float, EntryCandidate]] = []
        symbol_counts: dict[str, int] = defaultdict(int)
        for score, candidate in ranked:
            if per_symbol_cap is not None and symbol_counts[candidate.symbol] >= per_symbol_cap:
                continue
            chosen.append((score, candidate))
            symbol_counts[candidate.symbol] += 1
            if len(chosen) >= daily_cap:
                break
        for rank, (score, candidate) in enumerate(chosen, 1):
            selected.append(
                {
                    "session_day_et": held_day,
                    "rank": rank,
                    "score": score,
                    "symbol": candidate.symbol,
                    "entry_ts": candidate.entry_ts,
                    "won": candidate.won,
                    "ret_pct": candidate.ret_pct,
                }
            )
    return selected


def _selection_summary(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    wins = sum(bool(row["won"]) for row in rows)
    returns = [float(row["ret_pct"]) for row in rows]
    return {
        "selected": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate_pct": wins / len(rows) * 100.0 if rows else None,
        "total_return_pct": sum(returns),
        "mean_return_pct": statistics.fmean(returns) if returns else None,
    }


def _auc(candidates: Sequence[EntryCandidate], feature: str) -> float | None:
    observed = [
        (float(value), candidate.won)
        for candidate in candidates
        if (value := candidate.features.get(feature)) is not None
    ]
    winners = [value for value, won in observed if won]
    losers = [value for value, won in observed if not won]
    if not winners or not losers:
        return None
    favorable = 0.0
    for winner in winners:
        for loser in losers:
            favorable += 1.0 if winner > loser else 0.5 if winner == loser else 0.0
    return favorable / (len(winners) * len(losers))


def build_summary(candidates: Sequence[EntryCandidate]) -> dict[str, object]:
    family_rows = {}
    selections = {}
    for family, features in FEATURE_FAMILIES.items():
        rows = leave_one_day_out(candidates, features)
        family_rows[family] = _selection_summary(rows)
        selections[family] = rows
    selection_variants = {}
    for name, family, symbol_cap in (
        ("volume_one_per_symbol", "volume", 1),
        ("volume_two_per_symbol", "volume", 2),
        ("market_context_two_per_symbol", "market_context", 2),
    ):
        rows = leave_one_day_out(
            candidates,
            FEATURE_FAMILIES[family],
            per_symbol_cap=symbol_cap,
        )
        selection_variants[name] = _selection_summary(rows)

    by_day = {}
    for day in sorted({candidate.session_day_et for candidate in candidates}):
        rows = [candidate for candidate in candidates if candidate.session_day_et == day]
        wins = sum(candidate.won for candidate in rows)
        by_day[day] = {
            "candidates": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "oracle_top_6_wins": min(MAX_DAILY_SELECTION, wins),
            "oracle_top_6_losses": max(0, min(MAX_DAILY_SELECTION, len(rows)) - wins),
        }

    feature_stats = {}
    for feature in MODEL_FEATURES:
        observed = [
            candidate.features[feature]
            for candidate in candidates
            if candidate.features.get(feature) is not None
        ]
        winner_values = [
            float(candidate.features[feature])
            for candidate in candidates
            if candidate.won and candidate.features.get(feature) is not None
        ]
        loser_values = [
            float(candidate.features[feature])
            for candidate in candidates
            if not candidate.won and candidate.features.get(feature) is not None
        ]
        feature_stats[feature] = {
            "coverage": len(observed),
            "winner_median": statistics.median(winner_values) if winner_values else None,
            "loser_median": statistics.median(loser_values) if loser_values else None,
            "auc_higher_is_win": _auc(candidates, feature),
        }

    first_bid_negative = sum(
        candidate.first_bid_ret_pct is not None and candidate.first_bid_ret_pct < 0
        for candidate in candidates
    )
    losers = [candidate for candidate in candidates if not candidate.won]
    plus_5_runners_after_loss = [
        candidate for candidate in losers if candidate.future_reached_plus_5
    ]

    def median_present(values: Iterable[float | None]) -> float | None:
        observed = [float(value) for value in values if value is not None]
        return statistics.median(observed) if observed else None

    return {
        "population": _selection_summary(
            [
                {"won": candidate.won, "ret_pct": candidate.ret_pct}
                for candidate in candidates
            ]
        ),
        "by_day": by_day,
        "zero_floor_audit": {
            "entries": len(candidates),
            "first_bid_negative": first_bid_negative,
            "first_bid_non_negative": len(candidates) - first_bid_negative,
            "later_reached_plus_1": sum(candidate.future_reached_plus_1 for candidate in candidates),
            "later_reached_plus_2": sum(candidate.future_reached_plus_2 for candidate in candidates),
            "later_reached_plus_5": sum(candidate.future_reached_plus_5 for candidate in candidates),
            "floor_losers_later_reached_plus_1": sum(
                candidate.future_reached_plus_1 for candidate in losers
            ),
            "floor_losers_later_reached_plus_2": sum(
                candidate.future_reached_plus_2 for candidate in losers
            ),
            "floor_losers_later_reached_plus_5": len(plus_5_runners_after_loss),
            "floor_loser_plus_5_median_prior_mae_pct": median_present(
                candidate.mae_before_plus_5_pct for candidate in plus_5_runners_after_loss
            ),
            "floor_loser_plus_5_median_seconds": median_present(
                candidate.seconds_to_plus_5 for candidate in plus_5_runners_after_loss
            ),
        },
        "feature_stats": feature_stats,
        "leave_one_day_out": family_rows,
        "leave_one_day_out_variants": selection_variants,
        "selections": selections,
    }


def _candidate_row(candidate: EntryCandidate) -> dict[str, object]:
    row = {key: value for key, value in asdict(candidate).items() if key != "features"}
    row.update(candidate.features)
    return row


def write_outputs(output_dir: Path, candidates: Sequence[EntryCandidate]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-entry-filter-study-2026-08-24-to-2026-09-01"
    candidate_path = output_dir / f"{stem}-candidates.csv"
    selected_path = output_dir / f"{stem}-selected.csv"
    summary_path = output_dir / f"{stem}.json"
    candidate_rows = [_candidate_row(candidate) for candidate in candidates]
    with candidate_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(candidate_rows[0]))
        writer.writeheader()
        writer.writerows(candidate_rows)
    summary = build_summary(candidates)
    selected_rows = summary["selections"]["all"]
    with selected_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected_rows[0]))
        writer.writeheader()
        writer.writerows(selected_rows)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"candidates={candidate_path}")
    print(f"selected={selected_path}")
    print(f"summary={summary_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", required=True, type=Path)
    parser.add_argument("--output-dir", default="analysis/reports", type=Path)
    args = parser.parse_args()

    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    source = DbFeatureSource(build_session_factory(get_settings()))
    trades = load_policy_trades(args.trades)
    candidates = build_candidates(source, trades)
    write_outputs(args.output_dir, candidates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
