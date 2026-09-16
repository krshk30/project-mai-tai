#!/usr/bin/env python3
"""Pre-registered 30-session G5 gate for the v2 Massive ATR seed."""

from __future__ import annotations

import argparse
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as time_value, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from massive import RESTClient
from sqlalchemy import select

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms


ET = ZoneInfo("America/New_York")
PARITY_TOLERANCE = 0.001
PARITY_GATE = 0.995
LOST_FLIP_GATE = 0.01
TARGET_MULTIPLIER = 1.05
STOP_MULTIPLIER = 0.92


@dataclass(frozen=True)
class Outcome:
    session_day: date
    symbol: str
    entry_ts: int
    entry: float
    exit_ts: int
    reason: str
    return_pct: float


def _et(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, UTC).astimezone(ET)


def _load_population(start_day: date, end_day: date) -> dict[tuple[date, str], list[Bar]]:
    settings = get_settings()
    factory = build_session_factory(settings)
    lo = datetime.combine(start_day, time_value(4), tzinfo=ET).astimezone(UTC)
    hi = datetime.combine(end_day + timedelta(days=1), time_value(4), tzinfo=ET).astimezone(UTC)
    with factory() as session:
        rows = session.execute(
            select(StrategyBarHistory)
            .where(
                StrategyBarHistory.strategy_code == "schwab_1m_v2",
                StrategyBarHistory.interval_secs == 60,
                StrategyBarHistory.source == "live",
                StrategyBarHistory.bar_time >= lo,
                StrategyBarHistory.bar_time < hi,
            )
            .order_by(StrategyBarHistory.bar_time)
        ).scalars().all()
    population: dict[tuple[date, str], list[Bar]] = defaultdict(list)
    for row in rows:
        bar_time = row.bar_time if row.bar_time.tzinfo else row.bar_time.replace(tzinfo=UTC)
        session_day = bar_time.astimezone(ET).date()
        if start_day <= session_day <= end_day:
            population[(session_day, row.symbol)].append(
                Bar(
                    int(bar_time.timestamp() * 1000),
                    float(row.open_price),
                    float(row.high_price),
                    float(row.low_price),
                    float(row.close_price),
                    int(row.volume or 0),
                )
            )
    return dict(population)


def _massive_day(client: RESTClient, symbol: str, session_day: date) -> list[Bar]:
    lo = int(datetime.combine(session_day, time_value(4), tzinfo=ET).timestamp() * 1000)
    hi = int(datetime.combine(session_day, time_value(20), tzinfo=ET).timestamp() * 1000)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            rows = client.list_aggs(
                symbol,
                1,
                "minute",
                from_=session_day.isoformat(),
                to=session_day.isoformat(),
                adjusted=True,
                sort="asc",
                limit=50_000,
            )
            return [
                Bar(
                    int(row.timestamp),
                    float(row.open),
                    float(row.high),
                    float(row.low),
                    float(row.close),
                    int(row.volume or 0),
                )
                for row in rows
                if lo <= int(row.timestamp) < hi
            ]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(1.5)
    raise RuntimeError(f"Massive fetch failed for {symbol} {session_day}: {last_error}")


def _tradeable_buys(rows: list[dict], first_schwab_ms: int) -> dict[int, dict]:
    return {
        int(row["ts"]): row
        for row in rows
        if row.get("flip") == "BUY"
        and int(row["ts"]) >= first_schwab_ms
        and time_value(7) <= _et(int(row["ts"])).time() < time_value(16)
    }


def _score_gained(
    session_day: date,
    symbol: str,
    bars: list[Bar],
    rows: list[dict],
    entry_ts: int,
) -> Outcome:
    indexes = {bar.ts: index for index, bar in enumerate(bars)}
    entry_index = indexes[entry_ts]
    entry = float(bars[entry_index].close)
    target = entry * TARGET_MULTIPLIER
    stop = entry * STOP_MULTIPLIER
    sells = {int(row["ts"]) for row in rows if row.get("flip") == "SELL"}
    eligible = [bar for bar in bars if _et(bar.ts).time() < time_value(16)]
    last = eligible[-1]
    for bar in eligible[entry_index + 1 :]:
        if bar.low <= stop:
            return Outcome(session_day, symbol, entry_ts, entry, bar.ts, "stop", -8.0)
        if bar.high >= target:
            return Outcome(session_day, symbol, entry_ts, entry, bar.ts, "target", 5.0)
        if bar.ts in sells:
            return Outcome(
                session_day,
                symbol,
                entry_ts,
                entry,
                bar.ts,
                "sell_flip",
                (bar.close / entry - 1.0) * 100.0,
            )
    return Outcome(
        session_day,
        symbol,
        entry_ts,
        entry,
        last.ts,
        "session_end",
        (last.close / entry - 1.0) * 100.0,
    )


def _pct(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def run(start_day: date, end_day: date, output: Path) -> int:
    settings = get_settings()
    if not settings.massive_api_key:
        raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is missing")
    population = _load_population(start_day, end_day)
    client = RESTClient(api_key=settings.massive_api_key)
    parity_by_day: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    flips_by_day: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    lost: list[tuple[date, str, int]] = []
    gained: list[Outcome] = []
    fetch_failures: list[tuple[date, str, str]] = []

    for (session_day, symbol), schwab in sorted(population.items()):
        first_schwab = schwab[0].ts
        try:
            massive = _massive_day(client, symbol, session_day)
        except Exception as exc:  # noqa: BLE001
            fetch_failures.append((session_day, symbol, str(exc)))
            continue
        massive_by_ts = {bar.ts: bar for bar in massive}
        for schwab_bar in schwab:
            massive_bar = massive_by_ts.get(schwab_bar.ts)
            if massive_bar is None:
                continue
            parity_by_day[session_day][1] += 1
            matches = all(
                abs(massive_value - schwab_value)
                <= PARITY_TOLERANCE * abs(schwab_value)
                for massive_value, schwab_value in (
                    (massive_bar.close, schwab_bar.close),
                    (massive_bar.high, schwab_bar.high),
                    (massive_bar.low, schwab_bar.low),
                )
            )
            parity_by_day[session_day][0] += int(matches)

        anchor = session_start_ts_ms(first_schwab)
        pre = [bar for bar in massive if anchor <= bar.ts < first_schwab]
        seeded_bars = pre + schwab
        current_rows = compute_atr_trail(schwab, seed="sma5", period=5, factor=3.5)
        seeded_rows = compute_atr_trail(seeded_bars, seed="sma5", period=5, factor=3.5)
        current = _tradeable_buys(current_rows, first_schwab)
        seeded = _tradeable_buys(seeded_rows, first_schwab)
        same = set(current) & set(seeded)
        lost_keys = set(current) - set(seeded)
        gained_keys = set(seeded) - set(current)
        flips_by_day[session_day][0] += len(current)
        flips_by_day[session_day][1] += len(seeded)
        flips_by_day[session_day][2] += len(same)
        flips_by_day[session_day][3] += len(lost_keys)
        lost.extend((session_day, symbol, timestamp) for timestamp in sorted(lost_keys))
        gained.extend(
            _score_gained(session_day, symbol, seeded_bars, seeded_rows, timestamp)
            for timestamp in sorted(gained_keys)
        )

    parity_ok = sum(row[0] for row in parity_by_day.values())
    parity_total = sum(row[1] for row in parity_by_day.values())
    current_total = sum(row[0] for row in flips_by_day.values())
    seeded_total = sum(row[1] for row in flips_by_day.values())
    preserved_total = sum(row[2] for row in flips_by_day.values())
    parity_rate = _pct(parity_ok, parity_total)
    lost_rate = _pct(len(lost), current_total)
    returns = [row.return_pct for row in gained]
    by_symbol: dict[str, float] = defaultdict(float)
    for row in gained:
        by_symbol[row.symbol] += row.return_pct
    drop_one = [
        (sum(returns) - symbol_return, symbol)
        for symbol, symbol_return in by_symbol.items()
    ]
    worst_drop_one = min(drop_one, default=(0.0, "-"))
    session_days = {session_day for session_day, _symbol in population}
    coverage_ok = len(session_days) == 30 and not fetch_failures
    gate_pass = (
        coverage_ok
        and parity_total > 0
        and parity_rate >= PARITY_GATE
        and lost_rate <= LOST_FLIP_GATE
    )

    lines = [
        "# v2 ATR Massive Seed — 30-Session Replay",
        "",
        f"Run window: `{start_day}` through `{end_day}` ET. Generated `{datetime.now(UTC).isoformat()}`.",
        "",
        f"**G5 verdict: {'PASS' if gate_pass else 'FAIL'}**",
        "",
        "| Gate | Result | Required |",
        "|---|---:|---:|",
        f"| Session coverage | {len(session_days)}/30 sessions; fetch failures {len(fetch_failures)}/{len(population)} symbol-days | 30/30; 0 failures |",
        f"| Massive/Schwab c-h-l parity | {parity_ok}/{parity_total} ({parity_rate:.3%}) | >=99.5% |",
        f"| Tradeable BUY flip populations | current {current_total}; seeded {seeded_total} | denominator shown |",
        f"| Current BUY flips preserved | {preserved_total}/{current_total}; lost {len(lost)}/{current_total} ({lost_rate:.3%}) | lost <=1.0% |",
        f"| Symbol-days fetched | {len(population) - len(fetch_failures)}/{len(population)} | denominator shown |",
        "",
        "## Per-Day Denominators",
        "",
        "| ET day | symbol-days | parity | current BUY | seeded BUY | preserved | lost |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    population_by_day: dict[date, int] = defaultdict(int)
    for session_day, _symbol in population:
        population_by_day[session_day] += 1
    for session_day in sorted(population_by_day):
        parity = parity_by_day[session_day]
        flips = flips_by_day[session_day]
        lines.append(
            f"| {session_day} | {population_by_day[session_day]} | {parity[0]}/{parity[1]} "
            f"({_pct(parity[0], parity[1]):.2%}) | {flips[0]} | {flips[1]} | {flips[2]} | {flips[3]} |"
        )
    lines.extend(
        [
            "",
            "## Gained Flips",
            "",
            f"Gained: **{len(gained)}**. Median return: **{statistics.median(returns) if returns else 0.0:+.2f}%**. "
            f"Sum: **{sum(returns):+.2f}%**. Targets/stops/sell-flip/session-end: "
            f"**{sum(row.reason == 'target' for row in gained)}/"
            f"{sum(row.reason == 'stop' for row in gained)}/"
            f"{sum(row.reason == 'sell_flip' for row in gained)}/"
            f"{sum(row.reason == 'session_end' for row in gained)}**.",
            f"Drop-one by symbol worst remaining sum: **{worst_drop_one[0]:+.2f}%** after dropping `{worst_drop_one[1]}`.",
            "",
            "| ET day | symbol | BUY | entry | exit | reason | return |",
            "|---|---|---:|---:|---:|---|---:|",
        ]
    )
    for row in gained:
        lines.append(
            f"| {row.session_day} | {row.symbol} | {_et(row.entry_ts):%H:%M} | {row.entry:.4f} | "
            f"{_et(row.exit_ts):%H:%M} | {row.reason} | {row.return_pct:+.2f}% |"
        )
    lines.extend(["", "## Lost Current Flips", ""])
    if lost:
        lines.extend(["| ET day | symbol | BUY |", "|---|---|---:|"])
        lines.extend(
            f"| {session_day} | {symbol} | {_et(timestamp):%H:%M} |"
            for session_day, symbol, timestamp in lost
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Fetch Failures", ""])
    if fetch_failures:
        lines.extend(f"- `{day} {symbol}`: {reason}" for day, symbol, reason in fetch_failures)
    else:
        lines.append("None.")
    output.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:15]))
    return 0 if gate_pass else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-08-05")
    parser.add_argument("--end", default="2026-09-16")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/review-artifacts/v2-atr-massive-seed/replay-30.md"),
    )
    args = parser.parse_args()
    return run(date.fromisoformat(args.start), date.fromisoformat(args.end), args.output)


if __name__ == "__main__":
    raise SystemExit(main())
