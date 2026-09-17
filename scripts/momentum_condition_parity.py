#!/usr/bin/env python3
"""Compare Momentum trade-condition policies with Massive one-second aggregates."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_paper.conditions import (
    ConditionRule,
    build_condition_snapshot,
    classify_condition_codes,
)
from project_mai_tai.settings import get_settings


_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _Trade:
    sip_ts_ms: int
    price: Decimal
    conditions: tuple[int, ...]

    @property
    def second_ms(self) -> int:
        return self.sip_ts_ms // 1000 * 1000


@dataclass(frozen=True)
class _Bar:
    second_ms: int
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class _Parity:
    name: str
    total_trades: int
    eligible_trades: int
    bars: int
    matching_bars: int
    mismatched_bars: int
    bars_without_eligible_print: int
    phantom_seconds: int
    max_30s_rise_pct: Decimal | None
    max_60s_rise_pct: Decimal | None


def _value(source: object, *names: str) -> object:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M:%S").time()


def _bounds(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    start_at = datetime.combine(day, start, tzinfo=_ET).astimezone(UTC)
    end_at = datetime.combine(day, end, tzinfo=_ET).astimezone(UTC)
    if end_at <= start_at:
        raise ValueError("--end must be later than --start on the same ET date")
    return start_at, end_at


def _condition_codes(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return (int(value),)


def _load_trades(client: object, symbol: str, start: datetime, end: datetime) -> list[_Trade]:
    rows: list[_Trade] = []
    for raw in client.list_trades(
        symbol,
        timestamp_gte=int(start.timestamp() * 1_000_000_000),
        timestamp_lt=int(end.timestamp() * 1_000_000_000),
        order="asc",
        sort="timestamp",
        limit=50_000,
    ):
        sip_ns = _value(raw, "sip_timestamp", "t")
        price = _value(raw, "price", "p")
        if sip_ns is None or price is None:
            continue
        rows.append(
            _Trade(
                sip_ts_ms=int(sip_ns) // 1_000_000,
                price=Decimal(str(price)),
                conditions=_condition_codes(_value(raw, "conditions", "c")),
            )
        )
    return rows


def _load_bars(client: object, symbol: str, start: datetime, end: datetime) -> list[_Bar]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1
    rows: list[_Bar] = []
    for raw in client.list_aggs(
        symbol,
        1,
        "second",
        start_ms,
        end_ms,
        adjusted=True,
        sort="asc",
        limit=50_000,
    ):
        timestamp = _value(raw, "timestamp", "t")
        high = _value(raw, "high", "h")
        low = _value(raw, "low", "l")
        if timestamp is None or high is None or low is None:
            continue
        rows.append(
            _Bar(
                second_ms=int(timestamp) // 1000 * 1000,
                high=Decimal(str(high)),
                low=Decimal(str(low)),
            )
        )
    return rows


def evaluate(
    name: str,
    trades: Iterable[_Trade],
    bars: Iterable[_Bar],
    rules: Mapping[int, ConditionRule],
    neutral_codes: frozenset[int],
) -> _Parity:
    trade_rows = list(trades)
    bar_rows = list(bars)
    eligible_by_second: dict[int, list[Decimal]] = defaultdict(list)
    eligible_rows: list[_Trade] = []
    for trade in trade_rows:
        eligible, _ = classify_condition_codes(
            rules,
            trade.conditions,
            neutral_codes=neutral_codes,
        )
        if not eligible:
            continue
        eligible_rows.append(trade)
        eligible_by_second[trade.second_ms].append(trade.price)

    bar_by_second = {bar.second_ms: bar for bar in bar_rows}
    matching_bars = 0
    mismatched_bars = 0
    bars_without_eligible_print = 0
    for second_ms, bar in bar_by_second.items():
        prices = eligible_by_second.get(second_ms)
        if not prices:
            bars_without_eligible_print += 1
        elif max(prices) == bar.high and min(prices) == bar.low:
            matching_bars += 1
        else:
            mismatched_bars += 1

    return _Parity(
        name=name,
        total_trades=len(trade_rows),
        eligible_trades=len(eligible_rows),
        bars=len(bar_by_second),
        matching_bars=matching_bars,
        mismatched_bars=mismatched_bars,
        bars_without_eligible_print=bars_without_eligible_print,
        phantom_seconds=len(set(eligible_by_second) - set(bar_by_second)),
        max_30s_rise_pct=_max_rise_pct(eligible_rows, 30),
        max_60s_rise_pct=_max_rise_pct(eligible_rows, 60),
    )


def _max_rise_pct(trades: Iterable[_Trade], window_seconds: int) -> Decimal | None:
    ordered = sorted(trades, key=lambda row: row.sip_ts_ms)
    references: deque[tuple[int, Decimal]] = deque()
    best: Decimal | None = None
    index = 0
    while index < len(ordered):
        timestamp = ordered[index].sip_ts_ms
        group: list[_Trade] = []
        while index < len(ordered) and ordered[index].sip_ts_ms == timestamp:
            group.append(ordered[index])
            index += 1
        window_start = timestamp - window_seconds * 1000
        while references and references[0][0] < window_start:
            references.popleft()
        if references:
            reference = references[0][1]
            for trade in group:
                move = (trade.price / reference - Decimal("1")) * Decimal("100")
                best = move if best is None else max(best, move)
        group_low = min(trade.price for trade in group)
        while references and references[-1][1] >= group_low:
            references.pop()
        references.append((timestamp, group_low))
    return best


def _pct(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def _render(row: _Parity) -> str:
    return (
        f"{row.name:<28} eligible={row.eligible_trades}/{row.total_trades} "
        f"bar_high_low_match={row.matching_bars}/{row.bars} "
        f"mismatched_bars={row.mismatched_bars} "
        f"bars_without_eligible_print={row.bars_without_eligible_print} "
        f"phantom_seconds={row.phantom_seconds} "
        f"max_30s_rise={_pct(row.max_30s_rise_pct)} "
        f"max_60s_rise={_pct(row.max_60s_rise_pct)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("date", type=date.fromisoformat)
    parser.add_argument("--start", type=_parse_clock, default=time(4, 30))
    parser.add_argument("--end", type=_parse_clock, default=time(6, 0))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.massive_api_key:
        raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required")
    from massive import RESTClient

    client = RESTClient(api_key=settings.massive_api_key)
    retrieved_at = datetime.now(UTC)
    conditions = list(
        client.list_conditions(
            asset_class="stocks",
            data_type="trade",
            limit=1000,
            sort="id",
            order="asc",
        )
    )
    snapshot = build_condition_snapshot(conditions, retrieved_at=retrieved_at)
    start, end = _bounds(args.date, args.start, args.end)
    symbol = args.symbol.upper()
    trades = _load_trades(client, symbol, start, end)
    bars = _load_bars(client, symbol, start, end)
    rows = (
        evaluate("current_rule", trades, bars, snapshot.rules, frozenset()),
        evaluate("condition_12_neutral", trades, bars, snapshot.rules, frozenset({12})),
        evaluate("condition_12_and_37_neutral", trades, bars, snapshot.rules, frozenset({12, 37})),
    )
    print(
        f"symbol={symbol} window_et={start.astimezone(_ET).isoformat()}.."
        f"{end.astimezone(_ET).isoformat()} condition_snapshot={snapshot.version}"
    )
    for row in rows:
        print(_render(row))

    accepted = rows[1]
    if (
        accepted.bars == 0
        or accepted.matching_bars != accepted.bars
        or accepted.phantom_seconds != 0
    ):
        print("acceptance=FAIL condition_12_neutral did not reproduce every one-second bar")
        return 1
    print("acceptance=PASS condition_12_neutral reproduced every one-second bar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
