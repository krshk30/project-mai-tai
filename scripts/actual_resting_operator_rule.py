#!/usr/bin/env python3
"""Evaluate the operator's locked exit rule over real first/resting fills.

The population comes from the reviewed 82-event resting-fill census. Prices are
timestamped Massive NBBO bids. ATR SELL endpoints are recalculated and are
therefore explicitly labelled as caveated in every output row that uses one.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from actual_resting_entry_extrema import (
    EASTERN,
    Fill,
    account_label,
    exit_trigger,
    group_events,
    load_all_fills,
    pair_exits,
)
from actual_resting_entry_flip_window import sell_boundaries, session_bounds
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.replay import build_replay_settings
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

TARGET_PCT = Decimal("5")
TARGET_10_PCT = Decimal("10")
STOP_PCT = Decimal("8")
QUOTE_FRESHNESS = timedelta(seconds=10)


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal


@dataclass(frozen=True)
class LegResult:
    fill: Fill
    outcome: str
    trigger_at: datetime | None
    exit_bid: Decimal | None
    return_pct: Decimal | None
    note: str


def _label_values(raw: str) -> list[str]:
    return [part.split(": ", 1)[1] for part in raw.split("; ")]


def load_population(
    path: Path,
    fills: list[Fill],
    *,
    start_day: str,
    end_day: str,
) -> list[tuple[int, list[Fill]]]:
    """Select real resting fills; never derive entries from bars or ATR state."""
    by_id = {fill.fill_id: fill for fill in fills}
    legacy_ids: set[str] = set()
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if not start_day <= row["date_et"] <= end_day:
                continue
            for fill_id in _label_values(row["fill_id"]):
                legacy_ids.add(fill_id)

    entries: list[Fill] = []
    for fill in fills:
        if fill.side != "buy" or not start_day <= fill.session_day <= end_day:
            continue
        if fill.slot == "reclaim":
            continue
        if fill.slot != "first" and fill.fill_id not in legacy_ids:
            continue
        if fill.cw_flip_level is None:
            raise RuntimeError(
                f"real resting fill {fill.fill_id} has no stamped cw_flip_level; refusing recomputation"
            )
        entries.append(fill)

    missing = sorted(legacy_ids - by_id.keys())
    if missing:
        raise RuntimeError(f"vetted resting fill missing from database: {missing[0]}")
    return list(enumerate(group_events(entries), 1))


def _quote(session, sql: str, params: dict) -> QuotePoint | None:
    row = session.execute(text(sql), params).first()
    if row is None:
        return None
    return QuotePoint(row[1].astimezone(UTC), Decimal(str(row[0])))


def quote_points(session_factory, fill: Fill, endpoint: datetime) -> dict[str, QuotePoint | None]:
    valid = "bid_price>0 AND ask_price>=bid_price"
    params = {
        "symbol": fill.symbol,
        "entry": fill.at,
        "endpoint": endpoint,
        "target": fill.price * (Decimal("1") + TARGET_PCT / Decimal("100")),
        "target10": fill.price * (Decimal("1") + TARGET_10_PCT / Decimal("100")),
        "stop": fill.price * (Decimal("1") - STOP_PCT / Decimal("100")),
        "fresh_end": fill.at + QUOTE_FRESHNESS,
        "exit_end": endpoint + QUOTE_FRESHNESS,
    }
    with session_factory() as session:
        first = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:fresh_end AND {valid} "
            "ORDER BY event_ts,id LIMIT 1",
            params,
        )
        target = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} "
            "AND bid_price>=:target ORDER BY event_ts,id LIMIT 1",
            params,
        )
        stop = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} "
            "AND bid_price<=:stop ORDER BY event_ts,id LIMIT 1",
            params,
        )
        target10 = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} "
            "AND bid_price>=:target10 ORDER BY event_ts,id LIMIT 1",
            params,
        )
        endpoint_after = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:endpoint AND event_ts<=:exit_end AND {valid} "
            "ORDER BY event_ts,id LIMIT 1",
            params,
        )
        endpoint_before = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts<=:endpoint AND event_ts>=:endpoint-interval '10 seconds' AND {valid} "
            "ORDER BY event_ts DESC,id DESC LIMIT 1",
            params,
        )
        high = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} "
            "ORDER BY bid_price DESC,event_ts,id LIMIT 1",
            params,
        )
        low = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} "
            "ORDER BY bid_price,event_ts,id LIMIT 1",
            params,
        )
        print_high = _quote(
            session,
            "SELECT price,event_ts FROM market_capture_trades WHERE symbol=:symbol "
            "AND event_ts>=:entry AND event_ts<=:endpoint AND price>0 "
            "ORDER BY price DESC,event_ts,id LIMIT 1",
            params,
        )
    return {
        "first": first,
        "target": target,
        "stop": stop,
        "target10": target10,
        "endpoint_after": endpoint_after,
        "endpoint_before": endpoint_before,
        "high": high,
        "low": low,
        "print_high": print_high,
    }


def actual_leg_result(fill: Fill) -> tuple[Decimal | None, datetime | None, Decimal | None, str]:
    closed = sum((quantity for quantity, _ in fill.exits), Decimal("0"))
    if closed < fill.quantity:
        return None, None, None, f"broker fills close only {closed} of {fill.quantity} shares"
    proceeds = sum((quantity * exit_fill.price for quantity, exit_fill in fill.exits), Decimal("0"))
    exit_price = proceeds / fill.quantity
    return_pct = (exit_price / fill.price - Decimal("1")) * Decimal("100")
    exit_at = max(exit_fill.at for _, exit_fill in fill.exits)
    triggers = list(dict.fromkeys(exit_trigger(fill, exit_fill) for _, exit_fill in fill.exits))
    return return_pct, exit_at, exit_price, "+".join(triggers)


def weighted_values(values: list[tuple[Fill, Decimal | None]]) -> Decimal | None:
    if any(value is None for _, value in values):
        return None
    total_qty = sum((fill.quantity for fill, _ in values), Decimal("0"))
    return sum(
        (fill.quantity * value for fill, value in values if value is not None),
        Decimal("0"),
    ) / total_qty


def choose_outcome(
    fill: Fill,
    sell_at: datetime | None,
    cutoff: datetime,
    points: dict[str, QuotePoint | None],
) -> LegResult:
    if points["first"] is None:
        return LegResult(fill, "UNANSWERABLE", None, None, None, "no valid bid within 10s after fill")

    target = points["target"]
    stop = points["stop"]
    if target is not None and stop is not None and target.at == stop.at:
        return LegResult(fill, "UNANSWERABLE", None, None, None, "target/stop timestamp tie")

    candidates = []
    if target is not None:
        candidates.append((target.at, 0, "exited at +5%", target))
    if stop is not None:
        candidates.append((stop.at, 1, "exited at -8%", stop))
    boundary_outcome = "exited on ATR flip" if sell_at is not None else "still open at 16:00"
    candidates.append((sell_at or cutoff, 2, boundary_outcome, None))
    trigger_at, _, outcome, point = min(candidates, key=lambda item: (item[0], item[1]))

    if point is None:
        point = points["endpoint_after"] if sell_at is not None else points["endpoint_before"]
        if point is None:
            where = "recalculated ATR SELL" if sell_at is not None else "16:00"
            return LegResult(fill, "UNANSWERABLE", trigger_at, None, None, f"no valid bid within 10s of {where}")
    result = (point.bid / fill.price - Decimal("1")) * Decimal("100")
    note = "endpoint-independent"
    if outcome == "exited on ATR flip":
        note = "depends on recalculated ATR SELL endpoint"
    elif outcome == "still open at 16:00":
        note = "16:00 backstop"
    return LegResult(fill, outcome, trigger_at, point.bid, result, note)


def choose_yardstick(
    fill: Fill,
    sell_at: datetime | None,
    cutoff: datetime,
    points: dict[str, QuotePoint | None],
    *,
    target_key: str,
    target_label: str,
) -> LegResult:
    if points["first"] is None:
        return LegResult(fill, "UNANSWERABLE", None, None, None, "no valid bid within 10s after fill")
    target = points[target_key]
    if target is not None:
        result = (target.bid / fill.price - Decimal("1")) * Decimal("100")
        return LegResult(fill, target_label, target.at, target.bid, result, "endpoint-independent")
    endpoint = points["endpoint_after"] if sell_at is not None else points["endpoint_before"]
    if endpoint is None:
        where = "recalculated ATR SELL" if sell_at is not None else "16:00"
        return LegResult(fill, "UNANSWERABLE", sell_at or cutoff, None, None, f"no valid bid within 10s of {where}")
    result = (endpoint.bid / fill.price - Decimal("1")) * Decimal("100")
    note = "depends on recalculated ATR SELL endpoint" if sell_at is not None else "16:00 backstop"
    outcome = "exited on ATR flip" if sell_at is not None else "still open at 16:00"
    return LegResult(fill, outcome, sell_at or cutoff, endpoint.bid, result, note)


def event_outcome(results: list[LegResult]) -> tuple[str, Decimal | None, str]:
    outcomes = {result.outcome for result in results}
    if "UNANSWERABLE" in outcomes:
        reasons = "; ".join(
            f"{account_label(result.fill.account)}: {result.note}"
            for result in results
            if result.outcome == "UNANSWERABLE"
        )
        return "UNANSWERABLE", None, reasons
    if len(outcomes) != 1:
        detail = "; ".join(
            f"{account_label(result.fill.account)}={result.outcome}" for result in results
        )
        return "UNANSWERABLE", None, f"broker legs disagree: {detail}"
    total_qty = sum((result.fill.quantity for result in results), Decimal("0"))
    weighted = sum(
        (result.return_pct * result.fill.quantity for result in results if result.return_pct is not None),
        Decimal("0"),
    )
    return outcomes.pop(), weighted / total_qty, results[0].note


def event_return(results: list[LegResult]) -> Decimal | None:
    if any(result.return_pct is None for result in results):
        return None
    total_qty = sum((result.fill.quantity for result in results), Decimal("0"))
    return sum(
        (result.return_pct * result.fill.quantity for result in results if result.return_pct is not None),
        Decimal("0"),
    ) / total_qty


def render_time(value: datetime | None) -> str:
    return value.astimezone(EASTERN).strftime("%H:%M:%S.%f")[:-3] if value else "NA"


def measurement_end(sell_at: datetime | None, cutoff: datetime) -> datetime:
    return sell_at if sell_at is not None else cutoff


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, help="first ET session, YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="last ET session, YYYY-MM-DD")
    parser.add_argument(
        "--legacy-population",
        type=Path,
        default=Path(
            "analysis/reports/actual-resting-entry-extrema-2026-08-24-to-2026-09-01.csv"
        ),
        help="vetted fill IDs for the pre-cw_entry_slot era",
    )
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()

    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")

    settings = get_settings()
    session_factory = build_session_factory(settings)
    start, _ = session_bounds(args.start_date)
    end_day = datetime.fromisoformat(args.end_date).date() + timedelta(days=1)
    end, _ = session_bounds(end_day.isoformat())
    all_fills = load_all_fills(session_factory, start, end)
    events = load_population(
        args.legacy_population,
        all_fills,
        start_day=args.start_date,
        end_day=args.end_date,
    )
    if not events:
        raise RuntimeError("no real first/resting fills in requested date range")
    pair_exits(all_fills)
    grouped = [legs for _, legs in events]
    boundaries = sell_boundaries(
        DbMarketDataSource(session_factory),
        build_replay_settings(base=settings),
        grouped,
    )

    rows = []
    for event_id, legs in events:
        legs.sort(key=lambda fill: (fill.at, fill.account, fill.fill_id))
        entry_at = min(fill.at for fill in legs)
        _, cutoff = session_bounds(legs[0].session_day)
        sell_at = boundaries[(legs[0].symbol, entry_at.isoformat())]
        endpoint = measurement_end(sell_at, cutoff)
        leg_points = [quote_points(session_factory, fill, endpoint) for fill in legs]
        leg_results = [
            choose_outcome(fill, sell_at, cutoff, points)
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        plus5_results = [
            choose_yardstick(
                fill, sell_at, cutoff, points, target_key="target", target_label="exited at +5%"
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        plus10_results = [
            choose_yardstick(
                fill, sell_at, cutoff, points, target_key="target10", target_label="exited at +10%"
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        outcome, return_pct, note = event_outcome(leg_results)
        plus5_return = event_return(plus5_results)
        plus10_return = event_return(plus10_results)
        actual_results = [actual_leg_result(fill) for fill in legs]
        actual_return = weighted_values(
            [(fill, result[0]) for fill, result in zip(legs, actual_results, strict=True)]
        )
        touch_orders = []
        for points in leg_points:
            target_at = points["target"].at if points["target"] else None
            stop_at = points["stop"].at if points["stop"] else None
            if target_at is not None and stop_at is not None:
                touch_orders.append("+5 first" if target_at < stop_at else "-8 first" if stop_at < target_at else "tie")
            else:
                touch_orders.append("not both touched")
        rows.append(
            {
                "event": event_id,
                "date_et": legs[0].session_day,
                "symbol": legs[0].symbol,
                "fill_time_et": "; ".join(
                    f"{account_label(r.fill.account)}: {render_time(r.fill.at)}" for r in leg_results
                ),
                "fill_price": "; ".join(
                    f"{account_label(r.fill.account)}: {r.fill.price:.4f}" for r in leg_results
                ),
                "stamped_flip_level": "; ".join(
                    f"{account_label(r.fill.account)}: {r.fill.cw_flip_level:.4f}"
                    for r in leg_results
                ),
                "recalculated_atr_sell_et": render_time(sell_at) if sell_at else "none; 16:00 backstop",
                "outcome": outcome,
                "plus5_touch_et": "; ".join(
                    f"{account_label(fill.account)}: {render_time(points['target'].at if points['target'] else None)}"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "minus8_touch_et": "; ".join(
                    f"{account_label(fill.account)}: {render_time(points['stop'].at if points['stop'] else None)}"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "touch_order": "; ".join(
                    f"{account_label(fill.account)}: {order}"
                    for fill, order in zip(legs, touch_orders, strict=True)
                ),
                "trigger_time_et": "; ".join(
                    f"{account_label(r.fill.account)}: {render_time(r.trigger_at)}" for r in leg_results
                ),
                "exit_bid": "; ".join(
                    f"{account_label(r.fill.account)}: {r.exit_bid:.4f}" if r.exit_bid is not None
                    else f"{account_label(r.fill.account)}: NA"
                    for r in leg_results
                ),
                "high_bid_pct_time": "; ".join(
                    f"{account_label(fill.account)}: "
                    f"{((points['high'].bid / fill.price - Decimal('1')) * Decimal('100')):+.4f}% "
                    f"@ {(points['high'].at - fill.at).total_seconds() / 60:.3f}m"
                    if points["high"] is not None else f"{account_label(fill.account)}: NA"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "low_bid_pct_time": "; ".join(
                    f"{account_label(fill.account)}: "
                    f"{((points['low'].bid / fill.price - Decimal('1')) * Decimal('100')):+.4f}% "
                    f"@ {(points['low'].at - fill.at).total_seconds() / 60:.3f}m"
                    if points["low"] is not None else f"{account_label(fill.account)}: NA"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "event_return_pct": f"{return_pct:+.4f}" if return_pct is not None else "NA",
                "actual_exit_time_et": "; ".join(
                    f"{account_label(fill.account)}: {render_time(result[1])}"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "actual_exit_price": "; ".join(
                    f"{account_label(fill.account)}: {result[2]:.4f}"
                    if result[2] is not None else f"{account_label(fill.account)}: NA"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "actual_return_pct": f"{actual_return:+.4f}" if actual_return is not None else "NA",
                "actual_exit_note": "; ".join(
                    f"{account_label(fill.account)}: {result[3]}"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "dependency": note,
                "plus5_no_stop_return_pct": f"{plus5_return:+.4f}" if plus5_return is not None else "NA",
                "plus10_no_stop_return_pct": f"{plus10_return:+.4f}" if plus10_return is not None else "NA",
                "window": "actual resting fill -> recalculated ATR SELL; 16:00 backstop",
                "bid_print_gate": "; ".join(
                    f"{account_label(fill.account)}: "
                    f"bid={points['high'].bid:.4f} print={points['print_high'].bid:.4f}"
                    if points["high"] is not None and points["print_high"] is not None
                    else f"{account_label(fill.account)}: UNANSWERABLE"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
            }
        )

    sanity_failures = []
    sanity_gradable = 0
    for event_id, legs in events:
        row = rows[event_id - 1]
        for item in row["bid_print_gate"].split("; "):
            if "UNANSWERABLE" in item:
                continue
            sanity_gradable += 1
            bid_text, print_text = item.split(": ", 1)[1].split(" ")
            bid = Decimal(bid_text.split("=")[1])
            print_high = Decimal(print_text.split("=")[1])
            if bid > print_high:
                sanity_failures.append(f"{row['date_et']} {row['symbol']} {item}")
    outcomes = (
        "exited at +5%",
        "exited at -8%",
        "exited on ATR flip",
        "still open at 16:00",
        "UNANSWERABLE",
    )
    counts = {outcome: sum(row["outcome"] == outcome for row in rows) for outcome in outcomes}
    order_sets = [
        {part.rsplit(": ", 1)[1] for part in row["touch_order"].split("; ")}
        for row in rows
    ]
    all_legs_both = sum("not both touched" not in orders for orders in order_sets)
    both_target_first = sum(orders == {"+5 first"} for orders in order_sets)
    both_stop_first = sum(orders == {"-8 first"} for orders in order_sets)
    both_mixed = all_legs_both - both_target_first - both_stop_first
    gradable = [Decimal(row["event_return_pct"]) for row in rows if row["event_return_pct"] != "NA"]
    total = len(rows)
    sanity_line = (
        f"BID-VS-PRINT SANITY GATE: PASS ({sanity_gradable} broker legs checked, "
        "0 bid highs above print highs)."
        if not sanity_failures
        else f"BID-VS-PRINT SANITY GATE: FAIL ({len(sanity_failures)} of "
        f"{sanity_gradable} gradable broker legs have bid high above print high)."
    )
    summary = [
        "Every result uses `actual resting fill -> recalculated ATR SELL`; `16:00 ET` is backstop only.",
        sanity_line,
        "",
        f"Gradable: {len(gradable)} / {total}",
        "",
        "| Outcome | Count | Dependency |",
        "|---|---:|---|",
        f"| +5% | {counts['exited at +5%']} / {total} | endpoint-independent |",
        f"| -8% | {counts['exited at -8%']} / {total} | endpoint-independent |",
        f"| FLIP | {counts['exited on ATR flip']} / {total} | depends on recalculated endpoint |",
        f"| 16:00 | {counts['still open at 16:00']} / {total} | backstop |",
        f"| Unanswerable | {counts['UNANSWERABLE']} / {total} | no inference |",
        "",
        f"Both thresholds touched on every broker leg: {all_legs_both} / {total} "
        f"(+5 first {both_target_first}, -8 first {both_stop_first}, mixed/tied {both_mixed}).",
    ]
    if sanity_failures:
        summary.extend(["", "Ledger refused:", *[f"- {item}" for item in sanity_failures]])
        print("\n".join(summary))
        return 2

    summary.extend(
        [
            "",
            "| sym | buy | fill | hi % / hi t | lo % / lo t | exit | px | by | rule % | real % |",
            "|---|---|---|---|---|---|---|---|---:|---:|",
        ]
    )
    for row in rows:
        by = {
            "exited at +5%": "+5%",
            "exited at -8%": "-8%",
            "exited on ATR flip": "FLIP",
            "still open at 16:00": "16:00",
            "UNANSWERABLE": "UNANSWERABLE",
        }[row["outcome"]]
        real = row["actual_return_pct"]
        if real == "NA":
            real = f"-- ({row['actual_exit_note']})"
        summary.append(
            f"| {row['symbol']} | {row['fill_time_et']} | {row['fill_price']} | "
            f"{row['high_bid_pct_time']} | {row['low_bid_pct_time']} | "
            f"{row['trigger_time_et']} | {row['exit_bid']} | {by} | "
            f"{row['event_return_pct']}% | {real}% |"
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(summary))
    print(f"\nCSV supplement: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
