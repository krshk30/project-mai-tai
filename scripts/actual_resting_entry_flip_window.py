#!/usr/bin/env python3
"""SUPERSEDED measurement helper; do not use for the operator-rule study.

This legacy instrument uses trade prints for extrema and reconstructs ATR SELL
boundaries with ``ReplayStrategy``. Use ``actual_resting_operator_rule.py`` for
the guarded bid-based measurement instead.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from actual_resting_entry_extrema import (
    EASTERN,
    account_label,
    fmt_et,
    group_events,
    leg_boundary,
    load_all_fills,
    load_audit_slots,
    load_entry_fills,
    pair_exits,
    pct,
)
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.replay import (
    BAR_CLOSE_OFFSET_MS,
    ReplayStrategy,
    _to_chartbar,
    build_replay_settings,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings


def session_bounds(day: str) -> tuple[datetime, datetime]:
    session_day = datetime.fromisoformat(day).date()
    start = datetime.combine(session_day, time(4), EASTERN).astimezone(UTC)
    end = datetime.combine(session_day, time(16), EASTERN).astimezone(UTC)
    return start, end


def sell_boundaries(source, settings, events) -> dict[tuple[str, str], datetime | None]:
    boundaries: dict[tuple[str, str], datetime | None] = {}
    keys = sorted({(event[0].session_day, event[0].symbol) for event in events})
    for day, symbol in keys:
        start, end = session_bounds(day)
        bars = source.schwab_bars(symbol, start, end)
        strategy = ReplayStrategy(settings)
        state = strategy.watchlist_state(symbol)
        sells: list[datetime] = []
        for bar in sorted(bars, key=lambda item: item.ts):
            decision_ms = int(bar.ts) + BAR_CLOSE_OFFSET_MS
            strategy._clock_ms = decision_ms
            signal = strategy._update_atr_state(
                state,
                _to_chartbar(symbol, bar),
                observation_phase="replay",
            )
            if signal is not None and signal.get("flip") == "SELL":
                decision_at = datetime.fromtimestamp(decision_ms / 1000.0, UTC)
                if decision_at <= end:
                    sells.append(decision_at)
        entries = [
            min(leg.at for leg in event)
            for event in events
            if event[0].session_day == day and event[0].symbol == symbol
        ]
        for entry_at in entries:
            boundaries[(symbol, entry_at.isoformat())] = next(
                (sell_at for sell_at in sells if sell_at > entry_at), None
            )
    return boundaries


def trade_extrema(session_factory, symbol: str, start: datetime, end: datetime):
    with session_factory() as session:
        params = {"symbol": symbol, "start": start, "end": end}
        high = session.execute(
            text(
                "SELECT price,event_ts FROM market_capture_trades "
                "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end AND price>0 "
                "ORDER BY price DESC,event_ts ASC LIMIT 1"
            ),
            params,
        ).first()
        low = session.execute(
            text(
                "SELECT price,event_ts FROM market_capture_trades "
                "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end AND price>0 "
                "ORDER BY price ASC,event_ts ASC LIMIT 1"
            ),
            params,
        ).first()
    return high, low


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=Path, required=True)
    parser.add_argument("--primary-audit", type=Path, required=True)
    parser.add_argument("--webull-audit", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()

    slots = load_audit_slots(args.primary_audit, args.webull_audit)
    entry_shells = load_entry_fills(args.entries, slots)
    if len(entry_shells) != 106:
        raise RuntimeError(f"expected 106 first/resting fill rows, got {len(entry_shells)}")

    first_start, _ = session_bounds(min(fill.session_day for fill in entry_shells))
    _, final_end = session_bounds(max(fill.session_day for fill in entry_shells))
    base_settings = get_settings()
    session_factory = build_session_factory(base_settings)
    all_fills = load_all_fills(session_factory, first_start, final_end)
    by_id = {fill.fill_id: fill for fill in all_fills}
    entries = []
    for shell in entry_shells:
        real = by_id.get(shell.fill_id)
        if real is None:
            raise RuntimeError(f"entry fill disappeared from database: {shell.fill_id}")
        real.fanout_slot_id = shell.fanout_slot_id
        real.slot = "first"
        entries.append(real)
    pair_exits(all_fills)
    events = group_events(entries)
    if len(events) != 82:
        raise RuntimeError(f"expected 82 distinct first/resting events, got {len(events)}")

    source = DbMarketDataSource(session_factory)
    replay_settings = build_replay_settings(base=base_settings)
    boundaries = sell_boundaries(source, replay_settings, events)

    csv_rows: list[dict[str, str]] = []
    markdown_rows: list[str] = []
    for number, event in enumerate(events, 1):
        event.sort(key=lambda fill: (fill.at, fill.account, fill.fill_id))
        entry_at = min(leg.at for leg in event)
        _, cutoff = session_bounds(event[0].session_day)
        sell_at = boundaries[(event[0].symbol, entry_at.isoformat())]
        window_end = sell_at or cutoff
        sell_text = fmt_et(sell_at) if sell_at is not None else "16:00 no SELL"
        duplicate = len(event) > 1 and len({fill.account for fill in event}) == 1
        legs: list[dict[str, str]] = []
        for leg in event:
            high, low = trade_extrema(session_factory, leg.symbol, leg.at, window_end)
            if high is None or low is None:
                raise RuntimeError(
                    f"missing trades for event {number} {leg.symbol} {leg.fill_id}"
                )
            high_price = Decimal(str(high[0]))
            low_price = Decimal(str(low[0]))
            actual_at, actual_kind, actual_price, _ = leg_boundary(leg)
            actual = (
                f"{fmt_et(actual_at, millis=True)} ${actual_price}"
                if actual_kind == "actual exit"
                else "none by 16:00"
            )
            legs.append(
                {
                    "broker": account_label(leg.account),
                    "buy": fmt_et(leg.at, millis=True),
                    "fill": f"${leg.price:.4f}",
                    "high_price": f"${high_price:.4f}",
                    "high_pct": f"{pct(high_price, leg.price):+.4f}%",
                    "high_min": f"{(high[1] - leg.at).total_seconds() / 60:.3f}m",
                    "low_price": f"${low_price:.4f}",
                    "low_pct": f"{pct(low_price, leg.price):+.4f}%",
                    "low_min": f"{(low[1] - leg.at).total_seconds() / 60:.3f}m",
                    "actual": actual,
                }
            )

        def md_join(key: str) -> str:
            return "<br>".join(f"{leg['broker']}: {leg[key]}" for leg in legs)

        def csv_join(key: str) -> str:
            return "; ".join(f"{leg['broker']}: {leg[key]}" for leg in legs)
        broker = "+".join(dict.fromkeys(leg["broker"] for leg in legs))
        if duplicate:
            broker += " DUPLICATE x2"
        high_md = "<br>".join(
            f"{leg['broker']}: {leg['high_price']} ({leg['high_pct']}) @ {leg['high_min']}"
            for leg in legs
        )
        low_md = "<br>".join(
            f"{leg['broker']}: {leg['low_price']} ({leg['low_pct']}) @ {leg['low_min']}"
            for leg in legs
        )
        high_csv = "; ".join(
            f"{leg['broker']}: {leg['high_price']} ({leg['high_pct']}) @ {leg['high_min']}"
            for leg in legs
        )
        low_csv = "; ".join(
            f"{leg['broker']}: {leg['low_price']} ({leg['low_pct']}) @ {leg['low_min']}"
            for leg in legs
        )
        csv_rows.append(
            {
                "event": str(number),
                "date_et": event[0].session_day,
                "symbol": event[0].symbol,
                "buy_time_et": csv_join("buy"),
                "broker": broker,
                "fill_price": csv_join("fill"),
                "atr_sell_time_et": sell_text,
                "high_price_pct_elapsed": high_csv,
                "low_price_pct_elapsed": low_csv,
                "actual_exit_time_price": csv_join("actual"),
            }
        )
        markdown_rows.append(
            f"| {number} | {event[0].session_day} | {event[0].symbol} | {md_join('buy')} | "
            f"{broker} | {md_join('fill')} | {sell_text} | {high_md} | {low_md} | "
            f"{md_join('actual')} |"
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    lines = [
        "| # | Date | Sym | BUY ET | Broker | Fill | SELL ET | High / up / min | Low / down / min | Actual exit |",
        "|---:|---|---|---|---|---|---|---|---|---|",
        *markdown_rows,
        "",
    ]
    args.markdown.write_text("\n".join(lines))
    print(f"events={len(events)} csv_rows={len(csv_rows)}")
    print(args.csv)
    print(args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
