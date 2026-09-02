#!/usr/bin/env python3
"""Evaluate the operator's locked exit rule over real first/resting fills.

The population comes from the reviewed 82-event resting-fill census. Prices are
timestamped Massive NBBO bids. ATR SELL endpoints are recalculated and are
therefore explicitly labelled as caveated in every output row that uses one.

A price with no counterparty is not a price: quotes inside a detected print-free
halt window are excluded from extrema and cannot trigger an exit.
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
from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HALT_MIN_QUOTE_UPDATES,
    HaltWindow,
    confirmed_halt_window,
    timestamp_is_halted,
    window_contains_halt,
)
from project_mai_tai.settings import get_settings

TARGET_PCT = Decimal("5")
TARGET_10_PCT = Decimal("10")
STOP_PCT = Decimal("8")
QUOTE_FRESHNESS = timedelta(seconds=10)
DEPTH_DISCLOSURE = (
    "NOT SIZE-QUALIFIED: displayed bid-size contract is unverified; price-level bid only"
)
REPORTABLE_STRATUM = "REPORTABLE_ENDPOINT_INDEPENDENT"
CAVEATED_STRATUM = "CAVEATED_RECALCULATED_ATR_SELL"
BACKSTOP_STRATUM = "BACKSTOP_16_00"
UNANSWERABLE_STRATUM = "UNANSWERABLE"


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal
    observed_at: datetime | None = None


@dataclass(frozen=True)
class LegResult:
    fill: Fill
    outcome: str
    trigger_at: datetime | None
    exit_bid: Decimal | None
    return_pct: Decimal | None
    note: str


def result_stratum(outcome: str) -> str:
    if outcome in {"exited at +5%", "exited at -8%"}:
        return REPORTABLE_STRATUM
    if outcome == "exited on ATR flip":
        return CAVEATED_STRATUM
    if outcome == "still open at 16:00":
        return BACKSTOP_STRATUM
    return UNANSWERABLE_STRATUM


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


def detect_halt_windows(
    session_factory,
    symbol: str,
    session_start: datetime,
    session_end: datetime,
) -> list[HaltWindow]:
    """Find print-free LULD-shaped intervals with a continuing quote stream."""
    sql = """
        WITH ordered_prints AS (
            SELECT event_ts,
                   lag(event_ts) OVER (ORDER BY event_ts, id) AS prior_event_ts
            FROM market_capture_trades
            WHERE symbol=:symbol AND event_ts>=:session_start AND event_ts<=:session_end
        ), candidate_gaps AS (
            SELECT prior_event_ts AS last_print_at, event_ts AS reopen_print_at
            FROM ordered_prints
            WHERE prior_event_ts IS NOT NULL
              AND event_ts-prior_event_ts>=:minimum_gap
        )
        SELECT gap.last_print_at, gap.reopen_print_at, count(quote.id) AS quote_updates
        FROM candidate_gaps gap
        JOIN market_capture_quotes quote
          ON quote.symbol=:symbol
         AND quote.event_ts>gap.last_print_at
         AND quote.event_ts<gap.reopen_print_at
        GROUP BY gap.last_print_at, gap.reopen_print_at
        HAVING count(quote.id)>=:minimum_quotes
        ORDER BY gap.last_print_at
    """
    with session_factory() as session:
        rows = session.execute(
            text(sql),
            {
                "symbol": symbol,
                "session_start": session_start,
                "session_end": session_end,
                "minimum_gap": HALT_MIN_PRINT_GAP,
                "minimum_quotes": HALT_MIN_QUOTE_UPDATES,
            },
        ).all()
    windows = [
        confirmed_halt_window(
            last_print_at=row[0],
            reopen_print_at=row[1],
            quote_updates=int(row[2]),
        )
        for row in rows
    ]
    return [window for window in windows if window is not None]


def halt_exclusion(
    halts: list[HaltWindow],
) -> tuple[str, dict[str, datetime]]:
    clauses = []
    params = {}
    for index, halt in enumerate(halts):
        start_key = f"halt_start_{index}"
        end_key = f"halt_end_{index}"
        clauses.append(f"NOT (event_ts>:{start_key} AND event_ts<:{end_key})")
        params[start_key] = halt.last_print_at
        params[end_key] = halt.reopen_print_at
    return (" AND ".join(clauses) if clauses else "TRUE"), params


def defer_halted_boundary(
    sell_at: datetime | None,
    halts: list[HaltWindow],
) -> tuple[datetime | None, bool]:
    if sell_at is None:
        return None, False
    for halt in halts:
        if halt.last_print_at < sell_at < halt.reopen_print_at:
            return halt.reopen_print_at, True
    return sell_at, False


def deferred_threshold_point(
    session,
    *,
    params: dict,
    halts: list[HaltWindow],
    threshold_key: str,
    comparator: str,
    valid: str,
    tradable: str,
) -> tuple[QuotePoint | None, tuple[datetime, datetime] | None]:
    """Carry a threshold observed while halted to its first executable reopen quote."""
    if comparator not in {">=", "<="}:
        raise ValueError(f"unsupported threshold comparator: {comparator}")
    candidates = []
    unanswered = []
    for halt in halts:
        halt_params = {
            **params,
            "active_halt_start": halt.last_print_at,
            "active_halt_end": halt.reopen_print_at,
            "active_reopen_end": halt.reopen_print_at + QUOTE_FRESHNESS,
        }
        observed = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint "
            f"AND event_ts>:active_halt_start AND event_ts<:active_halt_end "
            f"AND {valid} AND bid_price{comparator}:{threshold_key} "
            "ORDER BY event_ts,id LIMIT 1",
            halt_params,
        )
        if observed is None:
            continue
        reopened = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:active_halt_end AND event_ts<=:active_reopen_end "
            f"AND {valid} AND {tradable} ORDER BY event_ts,id LIMIT 1",
            halt_params,
        )
        if reopened is None:
            unanswered.append((halt.reopen_print_at, observed.at))
            continue
        candidates.append(QuotePoint(reopened.at, reopened.bid, observed.at))
    point = min(candidates, key=lambda item: (item.at, item.observed_at)) if candidates else None
    missing = min(unanswered) if unanswered else None
    return point, missing


def earlier_point(*points: QuotePoint | None) -> QuotePoint | None:
    present = [point for point in points if point is not None]
    return (
        min(present, key=lambda item: (item.at, item.observed_at or item.at)) if present else None
    )


def quote_points(
    session_factory,
    fill: Fill,
    endpoint: datetime,
    halts: list[HaltWindow] | None = None,
) -> dict[str, QuotePoint | tuple[datetime, datetime] | None]:
    valid = "bid_price>0 AND ask_price>=bid_price"
    tradable, halt_params = halt_exclusion(halts or [])
    params = {
        "symbol": fill.symbol,
        "entry": fill.at,
        "endpoint": endpoint,
        "target": fill.price * (Decimal("1") + TARGET_PCT / Decimal("100")),
        "target10": fill.price * (Decimal("1") + TARGET_10_PCT / Decimal("100")),
        "stop": fill.price * (Decimal("1") - STOP_PCT / Decimal("100")),
        "fresh_end": fill.at + QUOTE_FRESHNESS,
        "exit_end": endpoint + QUOTE_FRESHNESS,
        **halt_params,
    }
    with session_factory() as session:
        first = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:fresh_end AND {valid} AND {tradable} "
            "ORDER BY event_ts,id LIMIT 1",
            params,
        )
        target = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} AND {tradable} "
            "AND bid_price>=:target ORDER BY event_ts,id LIMIT 1",
            params,
        )
        stop = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} AND {tradable} "
            "AND bid_price<=:stop ORDER BY event_ts,id LIMIT 1",
            params,
        )
        target10 = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} AND {tradable} "
            "AND bid_price>=:target10 ORDER BY event_ts,id LIMIT 1",
            params,
        )
        endpoint_after = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:endpoint AND event_ts<=:exit_end AND {valid} AND {tradable} "
            "ORDER BY event_ts,id LIMIT 1",
            params,
        )
        endpoint_before = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts<=:endpoint AND event_ts>=:endpoint-interval '10 seconds' "
            f"AND {valid} AND {tradable} "
            "ORDER BY event_ts DESC,id DESC LIMIT 1",
            params,
        )
        high = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} AND {tradable} "
            "ORDER BY bid_price DESC,event_ts,id LIMIT 1",
            params,
        )
        low = _quote(
            session,
            f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE symbol=:symbol "
            f"AND event_ts>=:entry AND event_ts<=:endpoint AND {valid} AND {tradable} "
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
        deferred_target, target_unanswerable = deferred_threshold_point(
            session,
            params=params,
            halts=halts or [],
            threshold_key="target",
            comparator=">=",
            valid=valid,
            tradable=tradable,
        )
        deferred_stop, stop_unanswerable = deferred_threshold_point(
            session,
            params=params,
            halts=halts or [],
            threshold_key="stop",
            comparator="<=",
            valid=valid,
            tradable=tradable,
        )
        deferred_target10, target10_unanswerable = deferred_threshold_point(
            session,
            params=params,
            halts=halts or [],
            threshold_key="target10",
            comparator=">=",
            valid=valid,
            tradable=tradable,
        )
    return {
        "first": first,
        "target": earlier_point(target, deferred_target),
        "stop": earlier_point(stop, deferred_stop),
        "target10": earlier_point(target10, deferred_target10),
        "target_unanswerable": target_unanswerable,
        "stop_unanswerable": stop_unanswerable,
        "target10_unanswerable": target10_unanswerable,
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
    return (
        sum(
            (fill.quantity * value for fill, value in values if value is not None),
            Decimal("0"),
        )
        / total_qty
    )


def choose_outcome(
    fill: Fill,
    sell_at: datetime | None,
    cutoff: datetime,
    points: dict[str, QuotePoint | tuple[datetime, datetime] | None],
    *,
    sell_deferred: bool = False,
) -> LegResult:
    if points["first"] is None:
        return LegResult(
            fill, "UNANSWERABLE", None, None, None, "no valid bid within 10s after fill"
        )

    target = points["target"]
    stop = points["stop"]
    assert target is None or isinstance(target, QuotePoint)
    assert stop is None or isinstance(stop, QuotePoint)
    if target is not None and stop is not None and target.at == stop.at:
        target_observed = target.observed_at or target.at
        stop_observed = stop.observed_at or stop.at
        if target_observed == stop_observed:
            return LegResult(fill, "UNANSWERABLE", None, None, None, "target/stop timestamp tie")

    candidates = []
    if target is not None:
        candidates.append((target.at, target.observed_at or target.at, 0, "exited at +5%", target))
    if stop is not None:
        candidates.append((stop.at, stop.observed_at or stop.at, 1, "exited at -8%", stop))
    target_missing = points.get("target_unanswerable")
    stop_missing = points.get("stop_unanswerable")
    if isinstance(target_missing, tuple):
        candidates.append((*target_missing, 0, "UNANSWERABLE", None))
    if isinstance(stop_missing, tuple):
        candidates.append((*stop_missing, 1, "UNANSWERABLE", None))
    boundary_outcome = "exited on ATR flip" if sell_at is not None else "still open at 16:00"
    boundary_at = sell_at or cutoff
    candidates.append((boundary_at, boundary_at, 2, boundary_outcome, None))
    trigger_at, _, _, outcome, point = min(candidates, key=lambda item: item[:3])

    if outcome == "UNANSWERABLE":
        return LegResult(
            fill,
            outcome,
            trigger_at,
            None,
            None,
            "halted threshold had no executable quote within 10s after reopen",
        )

    if point is None:
        point = points["endpoint_after"] if sell_at is not None else points["endpoint_before"]
        assert point is None or isinstance(point, QuotePoint)
        if point is None:
            where = "recalculated ATR SELL" if sell_at is not None else "16:00"
            return LegResult(
                fill, "UNANSWERABLE", trigger_at, None, None, f"no valid bid within 10s of {where}"
            )
        trigger_at = point.at
    result = (point.bid / fill.price - Decimal("1")) * Decimal("100")
    note = "endpoint-independent"
    if point.observed_at is not None:
        note = "threshold observed during halt; executed after reopen"
    if outcome == "exited on ATR flip":
        note = "depends on recalculated ATR SELL endpoint"
        if sell_deferred:
            note += "; raw flip was halted, executed after reopen"
    elif outcome == "still open at 16:00":
        note = "16:00 backstop"
    return LegResult(fill, outcome, trigger_at, point.bid, result, note)


def choose_yardstick(
    fill: Fill,
    sell_at: datetime | None,
    cutoff: datetime,
    points: dict[str, QuotePoint | tuple[datetime, datetime] | None],
    *,
    target_key: str,
    target_label: str,
    sell_deferred: bool = False,
) -> LegResult:
    if points["first"] is None:
        return LegResult(
            fill, "UNANSWERABLE", None, None, None, "no valid bid within 10s after fill"
        )
    target = points[target_key]
    assert target is None or isinstance(target, QuotePoint)
    missing = points.get(f"{target_key}_unanswerable")
    target_order = (target.at, target.observed_at or target.at) if target is not None else None
    if isinstance(missing, tuple) and (target_order is None or missing < target_order):
        return LegResult(
            fill,
            "UNANSWERABLE",
            missing[0],
            None,
            None,
            "halted threshold had no executable quote within 10s after reopen",
        )
    if target is not None:
        result = (target.bid / fill.price - Decimal("1")) * Decimal("100")
        note = "endpoint-independent"
        if target.observed_at is not None:
            note = "threshold observed during halt; executed after reopen"
        return LegResult(fill, target_label, target.at, target.bid, result, note)
    endpoint = points["endpoint_after"] if sell_at is not None else points["endpoint_before"]
    assert endpoint is None or isinstance(endpoint, QuotePoint)
    if endpoint is None:
        where = "recalculated ATR SELL" if sell_at is not None else "16:00"
        return LegResult(
            fill,
            "UNANSWERABLE",
            sell_at or cutoff,
            None,
            None,
            f"no valid bid within 10s of {where}",
        )
    result = (endpoint.bid / fill.price - Decimal("1")) * Decimal("100")
    note = "depends on recalculated ATR SELL endpoint" if sell_at is not None else "16:00 backstop"
    if sell_at is not None and sell_deferred:
        note += "; raw flip was halted, executed after reopen"
    outcome = "exited on ATR flip" if sell_at is not None else "still open at 16:00"
    return LegResult(fill, outcome, endpoint.at, endpoint.bid, result, note)


def choose_boundary(
    fill: Fill,
    sell_at: datetime | None,
    cutoff: datetime,
    points: dict[str, QuotePoint | tuple[datetime, datetime] | None],
    *,
    sell_deferred: bool = False,
) -> LegResult:
    """Hold without price exits until the halt-aware ATR SELL or 16:00 backstop."""
    if points["first"] is None:
        return LegResult(
            fill, "UNANSWERABLE", None, None, None, "no valid bid within 10s after fill"
        )
    endpoint = points["endpoint_after"] if sell_at is not None else points["endpoint_before"]
    assert endpoint is None or isinstance(endpoint, QuotePoint)
    if endpoint is None:
        where = "recalculated ATR SELL" if sell_at is not None else "16:00"
        return LegResult(
            fill,
            "UNANSWERABLE",
            sell_at or cutoff,
            None,
            None,
            f"no valid bid within 10s of {where}",
        )
    result = (endpoint.bid / fill.price - Decimal("1")) * Decimal("100")
    if sell_at is not None:
        outcome = "exited on ATR flip"
        note = "depends on recalculated ATR SELL endpoint"
        if sell_deferred:
            note += "; raw flip was halted, executed after reopen"
    else:
        outcome = "still open at 16:00"
        note = "16:00 backstop"
    return LegResult(fill, outcome, endpoint.at, endpoint.bid, result, note)


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
        (
            result.return_pct * result.fill.quantity
            for result in results
            if result.return_pct is not None
        ),
        Decimal("0"),
    )
    return outcomes.pop(), weighted / total_qty, results[0].note


def event_return(results: list[LegResult]) -> Decimal | None:
    if any(result.return_pct is None for result in results):
        return None
    total_qty = sum((result.fill.quantity for result in results), Decimal("0"))
    return (
        sum(
            (
                result.return_pct * result.fill.quantity
                for result in results
                if result.return_pct is not None
            ),
            Decimal("0"),
        )
        / total_qty
    )


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
        default=Path("analysis/reports/actual-resting-entry-extrema-2026-08-24-to-2026-09-01.csv"),
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

    halt_keys = sorted({(legs[0].session_day, legs[0].symbol) for legs in grouped})
    halts_by_key = {}
    for day, symbol in halt_keys:
        session_start, session_end = session_bounds(day)
        halts_by_key[(day, symbol)] = detect_halt_windows(
            session_factory,
            symbol,
            session_start,
            session_end,
        )

    # Measure the exposure using the old, unfiltered quote path before applying the correction.
    halt_window_events = []
    halted_sell_boundary_events = []
    halted_trigger_events = []
    halted_plus5_events = []
    halted_plus10_events = []
    for event_id, legs in events:
        entry_at = min(fill.at for fill in legs)
        _, cutoff = session_bounds(legs[0].session_day)
        raw_sell_at = boundaries[(legs[0].symbol, entry_at.isoformat())]
        raw_endpoint = measurement_end(raw_sell_at, cutoff)
        event_halts = halts_by_key[(legs[0].session_day, legs[0].symbol)]
        label = f"{legs[0].session_day} {legs[0].symbol} event {event_id}"
        if window_contains_halt(entry_at, raw_endpoint, event_halts):
            halt_window_events.append(label)
        if timestamp_is_halted(raw_sell_at, event_halts):
            halted_sell_boundary_events.append(label)
        raw_points = [quote_points(session_factory, fill, raw_endpoint, []) for fill in legs]
        raw_results = [
            choose_outcome(
                fill,
                raw_sell_at,
                cutoff,
                points,
            )
            for fill, points in zip(legs, raw_points, strict=True)
        ]
        raw_plus5_results = [
            choose_yardstick(
                fill,
                raw_sell_at,
                cutoff,
                points,
                target_key="target",
                target_label="exited at +5%",
            )
            for fill, points in zip(legs, raw_points, strict=True)
        ]
        raw_plus10_results = [
            choose_yardstick(
                fill,
                raw_sell_at,
                cutoff,
                points,
                target_key="target10",
                target_label="exited at +10%",
            )
            for fill, points in zip(legs, raw_points, strict=True)
        ]
        halted_results = [
            result for result in raw_results if timestamp_is_halted(result.trigger_at, event_halts)
        ]
        if halted_results:
            outcomes = ", ".join(sorted({result.outcome for result in halted_results}))
            halted_trigger_events.append(f"{label} ({outcomes})")
        if any(timestamp_is_halted(result.trigger_at, event_halts) for result in raw_plus5_results):
            halted_plus5_events.append(label)
        if any(
            timestamp_is_halted(result.trigger_at, event_halts) for result in raw_plus10_results
        ):
            halted_plus10_events.append(label)

    rows = []
    for event_id, legs in events:
        legs.sort(key=lambda fill: (fill.at, fill.account, fill.fill_id))
        entry_at = min(fill.at for fill in legs)
        _, cutoff = session_bounds(legs[0].session_day)
        raw_sell_at = boundaries[(legs[0].symbol, entry_at.isoformat())]
        event_halts = halts_by_key[(legs[0].session_day, legs[0].symbol)]
        sell_at, sell_deferred = defer_halted_boundary(raw_sell_at, event_halts)
        endpoint = measurement_end(sell_at, cutoff)
        leg_points = [quote_points(session_factory, fill, endpoint, event_halts) for fill in legs]
        leg_results = [
            choose_outcome(
                fill,
                sell_at,
                cutoff,
                points,
                sell_deferred=sell_deferred,
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        plus5_results = [
            choose_yardstick(
                fill,
                sell_at,
                cutoff,
                points,
                target_key="target",
                target_label="exited at +5%",
                sell_deferred=sell_deferred,
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        plus10_results = [
            choose_yardstick(
                fill,
                sell_at,
                cutoff,
                points,
                target_key="target10",
                target_label="exited at +10%",
                sell_deferred=sell_deferred,
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        boundary_results = [
            choose_boundary(
                fill,
                sell_at,
                cutoff,
                points,
                sell_deferred=sell_deferred,
            )
            for fill, points in zip(legs, leg_points, strict=True)
        ]
        outcome, return_pct, note = event_outcome(leg_results)
        plus5_return = event_return(plus5_results)
        plus10_return = event_return(plus10_results)
        boundary_return = event_return(boundary_results)
        actual_results = [actual_leg_result(fill) for fill in legs]
        actual_return = weighted_values(
            [(fill, result[0]) for fill, result in zip(legs, actual_results, strict=True)]
        )
        touch_orders = []
        for points in leg_points:
            target_at = points["target"].at if points["target"] else None
            stop_at = points["stop"].at if points["stop"] else None
            if target_at is not None and stop_at is not None:
                touch_orders.append(
                    "+5 first"
                    if target_at < stop_at
                    else "-8 first"
                    if stop_at < target_at
                    else "tie"
                )
            else:
                touch_orders.append("not both touched")
        rows.append(
            {
                "event": event_id,
                "date_et": legs[0].session_day,
                "symbol": legs[0].symbol,
                "fill_time_et": "; ".join(
                    f"{account_label(r.fill.account)}: {render_time(r.fill.at)}"
                    for r in leg_results
                ),
                "fill_price": "; ".join(
                    f"{account_label(r.fill.account)}: {r.fill.price:.4f}" for r in leg_results
                ),
                "stamped_flip_level": "; ".join(
                    f"{account_label(r.fill.account)}: {r.fill.cw_flip_level:.4f}"
                    for r in leg_results
                ),
                "raw_recalculated_atr_sell_et": (
                    render_time(raw_sell_at) if raw_sell_at else "none; 16:00 backstop"
                ),
                "recalculated_atr_sell_et": render_time(sell_at)
                if sell_at
                else "none; 16:00 backstop",
                "sell_deferred_from_halt": "yes" if sell_deferred else "no",
                "halt_windows_in_measurement": sum(
                    halt.reopen_print_at > entry_at and halt.last_print_at < endpoint
                    for halt in event_halts
                ),
                "outcome": outcome,
                "result_stratum": result_stratum(outcome),
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
                    f"{account_label(r.fill.account)}: {render_time(r.trigger_at)}"
                    for r in leg_results
                ),
                "exit_bid": "; ".join(
                    f"{account_label(r.fill.account)}: {r.exit_bid:.4f}"
                    if r.exit_bid is not None
                    else f"{account_label(r.fill.account)}: NA"
                    for r in leg_results
                ),
                "high_bid_pct_time": "; ".join(
                    f"{account_label(fill.account)}: "
                    f"{((points['high'].bid / fill.price - Decimal('1')) * Decimal('100')):+.4f}% "
                    f"@ {(points['high'].at - fill.at).total_seconds() / 60:.3f}m"
                    if points["high"] is not None
                    else f"{account_label(fill.account)}: NA"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "low_bid_pct_time": "; ".join(
                    f"{account_label(fill.account)}: "
                    f"{((points['low'].bid / fill.price - Decimal('1')) * Decimal('100')):+.4f}% "
                    f"@ {(points['low'].at - fill.at).total_seconds() / 60:.3f}m"
                    if points["low"] is not None
                    else f"{account_label(fill.account)}: NA"
                    for fill, points in zip(legs, leg_points, strict=True)
                ),
                "event_return_pct": f"{return_pct:+.4f}" if return_pct is not None else "NA",
                "actual_exit_time_et": "; ".join(
                    f"{account_label(fill.account)}: {render_time(result[1])}"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "actual_exit_price": "; ".join(
                    f"{account_label(fill.account)}: {result[2]:.4f}"
                    if result[2] is not None
                    else f"{account_label(fill.account)}: NA"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "actual_return_pct": f"{actual_return:+.4f}" if actual_return is not None else "NA",
                "actual_exit_note": "; ".join(
                    f"{account_label(fill.account)}: {result[3]}"
                    for fill, result in zip(legs, actual_results, strict=True)
                ),
                "dependency": note,
                "depth_basis": DEPTH_DISCLOSURE,
                "plus5_no_stop_return_pct": f"{plus5_return:+.4f}"
                if plus5_return is not None
                else "NA",
                "plus5_ungradable_reason": "; ".join(
                    f"{account_label(result.fill.account)}: {result.note}"
                    for result in plus5_results
                    if result.return_pct is None
                ),
                "plus10_no_stop_return_pct": f"{plus10_return:+.4f}"
                if plus10_return is not None
                else "NA",
                "plus10_ungradable_reason": "; ".join(
                    f"{account_label(result.fill.account)}: {result.note}"
                    for result in plus10_results
                    if result.return_pct is None
                ),
                "atr_sell_counterfactual_pct": f"{boundary_return:+.4f}"
                if boundary_return is not None
                else "NA",
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
        {part.rsplit(": ", 1)[1] for part in row["touch_order"].split("; ")} for row in rows
    ]
    all_legs_both = sum("not both touched" not in orders for orders in order_sets)
    both_target_first = sum(orders == {"+5 first"} for orders in order_sets)
    both_stop_first = sum(orders == {"-8 first"} for orders in order_sets)
    both_mixed = all_legs_both - both_target_first - both_stop_first
    gradable = [Decimal(row["event_return_pct"]) for row in rows if row["event_return_pct"] != "NA"]
    total = len(rows)
    stratum_counts = {
        stratum: sum(row["result_stratum"] == stratum for row in rows)
        for stratum in (
            REPORTABLE_STRATUM,
            CAVEATED_STRATUM,
            BACKSTOP_STRATUM,
            UNANSWERABLE_STRATUM,
        )
    }
    for row in rows:
        row["stratum_denominator"] = f"{stratum_counts[row['result_stratum']]} / {total}"
    sanity_line = (
        f"BID-VS-PRINT SANITY GATE: PASS ({sanity_gradable} broker legs checked, "
        "0 bid highs above print highs)."
        if not sanity_failures
        else f"BID-VS-PRINT SANITY GATE: FAIL ({len(sanity_failures)} of "
        f"{sanity_gradable} gradable broker legs have bid high above print high)."
    )
    summary = [
        "Every result uses `actual resting fill -> recalculated ATR SELL`; `16:00 ET` is backstop only.",
        f"Depth basis on every row: {DEPTH_DISCLOSURE}.",
        f"Halt definition: print gap >= {HALT_MIN_PRINT_GAP.total_seconds():.0f}s with "
        f">= {HALT_MIN_QUOTE_UPDATES} quote updates continuing inside the gap.",
        f"PRE-FIX HALT EXPOSURE: windows containing halt {len(halt_window_events)} / {total}; "
        f"raw ATR SELL boundaries inside halt {len(halted_sell_boundary_events)} / {total}; "
        f"operator-rule exits inside halt {len(halted_trigger_events)} / {total}; "
        f"+5 yardstick exits inside halt {len(halted_plus5_events)} / {total}; "
        f"+10 yardstick exits inside halt {len(halted_plus10_events)} / {total}.",
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
        summary.extend(
            [
                "",
                "Halt-window events:",
                *[f"- {item}" for item in halt_window_events],
                "",
                "Old triggers inside halt:",
                *[f"- {item}" for item in halted_trigger_events],
                "",
                "Ledger refused:",
                *[f"- {item}" for item in sanity_failures],
            ]
        )
        print("\n".join(summary))
        return 2

    plus5_pairs = [
        row
        for row in rows
        if row["event_return_pct"] != "NA" and row["plus5_no_stop_return_pct"] != "NA"
    ]
    plus10_pairs = [
        row
        for row in rows
        if row["event_return_pct"] != "NA" and row["plus10_no_stop_return_pct"] != "NA"
    ]
    operator_total = sum(gradable, Decimal("0"))
    plus5_operator = sum((Decimal(row["event_return_pct"]) for row in plus5_pairs), Decimal("0"))
    plus5_total = sum(
        (Decimal(row["plus5_no_stop_return_pct"]) for row in plus5_pairs), Decimal("0")
    )
    plus10_operator = sum((Decimal(row["event_return_pct"]) for row in plus10_pairs), Decimal("0"))
    plus10_total = sum(
        (Decimal(row["plus10_no_stop_return_pct"]) for row in plus10_pairs), Decimal("0")
    )
    operator_rows = [row for row in rows if row["event_return_pct"] != "NA"]
    reportable_rows = [
        row for row in operator_rows if row["result_stratum"] == REPORTABLE_STRATUM
    ]
    caveated_rows = [
        row for row in operator_rows if row["result_stratum"] == CAVEATED_STRATUM
    ]
    backstop_rows = [
        row for row in operator_rows if row["result_stratum"] == BACKSTOP_STRATUM
    ]

    def stratum_total(selected_rows: list[dict[str, object]]) -> Decimal:
        return sum(
            (Decimal(str(row["event_return_pct"])) for row in selected_rows),
            Decimal("0"),
        )

    def paired_totals(
        selected_rows: list[dict[str, object]], field: str
    ) -> tuple[list[dict[str, object]], Decimal, Decimal]:
        paired = [row for row in selected_rows if row[field] != "NA"]
        return (
            paired,
            stratum_total(paired),
            sum((Decimal(str(row[field])) for row in paired), Decimal("0")),
        )

    reportable_total = stratum_total(reportable_rows)
    caveated_total = stratum_total(caveated_rows)
    backstop_total = stratum_total(backstop_rows)
    reportable_plus5 = paired_totals(reportable_rows, "plus5_no_stop_return_pct")
    caveated_plus5 = paired_totals(caveated_rows, "plus5_no_stop_return_pct")
    backstop_plus5 = paired_totals(backstop_rows, "plus5_no_stop_return_pct")
    reportable_plus10 = paired_totals(reportable_rows, "plus10_no_stop_return_pct")
    caveated_plus10 = paired_totals(caveated_rows, "plus10_no_stop_return_pct")
    backstop_plus10 = paired_totals(backstop_rows, "plus10_no_stop_return_pct")
    reportable_realized = paired_totals(reportable_rows, "actual_return_pct")
    caveated_realized = paired_totals(caveated_rows, "actual_return_pct")
    backstop_realized = paired_totals(backstop_rows, "actual_return_pct")
    stopped_rows = [row for row in operator_rows if row["outcome"] == "exited at -8%"]
    stopped_counterfactual = [
        row for row in stopped_rows if row["atr_sell_counterfactual_pct"] != "NA"
    ]
    stopped_paid = sum((Decimal(row["event_return_pct"]) for row in stopped_rows), Decimal("0"))
    stopped_paired_paid = sum(
        (Decimal(row["event_return_pct"]) for row in stopped_counterfactual),
        Decimal("0"),
    )
    stopped_unprotected = sum(
        (Decimal(row["atr_sell_counterfactual_pct"]) for row in stopped_counterfactual),
        Decimal("0"),
    )
    dropped_plus5 = [row for row in operator_rows if row["plus5_no_stop_return_pct"] == "NA"]
    dropped_plus10 = [row for row in operator_rows if row["plus10_no_stop_return_pct"] == "NA"]
    summary.extend(
        [
            "",
            "REPRODUCTION CONTROLS ONLY - legacy pooled values are not reportable headlines:",
            f"- pooled control {operator_total:+.4f}% on {len(gradable)} / {total} gradable",
            f"- pooled +5 pairing: operator {plus5_operator:+.4f}% vs "
            f"+5 {plus5_total:+.4f}% on {len(plus5_pairs)} / {total}",
            f"- pooled +10 pairing: operator {plus10_operator:+.4f}% vs "
            f"+10 {plus10_total:+.4f}% on {len(plus10_pairs)} / {total}",
            "",
            f"REPORTABLE ENDPOINT-INDEPENDENT: {reportable_total:+.4f}% on "
            f"{len(reportable_rows)} / {stratum_counts[REPORTABLE_STRATUM]} stratum rows "
            f"({stratum_counts[REPORTABLE_STRATUM]} / {total} total).",
            f"CAVEATED RECALCULATED ATR SELL: {caveated_total:+.4f}% on "
            f"{len(caveated_rows)} / {stratum_counts[CAVEATED_STRATUM]} caveated rows "
            f"({stratum_counts[CAVEATED_STRATUM]} / {total} total); never pooled into a headline.",
            f"16:00 BACKSTOP: {backstop_total:+.4f}% on "
            f"{len(backstop_rows)} / {stratum_counts[BACKSTOP_STRATUM]} backstop rows "
            f"({stratum_counts[BACKSTOP_STRATUM]} / {total} total).",
            "",
            f"REPORTABLE +5 PAIRING: operator {reportable_plus5[1]:+.4f}% vs "
            f"+5 {reportable_plus5[2]:+.4f}% on {len(reportable_plus5[0])} / "
            f"{stratum_counts[REPORTABLE_STRATUM]} stratum rows ({len(reportable_plus5[0])} / {total} total).",
            f"CAVEATED +5 PAIRING: operator {caveated_plus5[1]:+.4f}% vs "
            f"+5 {caveated_plus5[2]:+.4f}% on {len(caveated_plus5[0])} / "
            f"{stratum_counts[CAVEATED_STRATUM]} caveated rows ({len(caveated_plus5[0])} / {total} total).",
            f"BACKSTOP +5 PAIRING: operator {backstop_plus5[1]:+.4f}% vs "
            f"+5 {backstop_plus5[2]:+.4f}% on {len(backstop_plus5[0])} / "
            f"{stratum_counts[BACKSTOP_STRATUM]} backstop rows ({len(backstop_plus5[0])} / {total} total).",
            f"REPORTABLE +10 PAIRING: operator {reportable_plus10[1]:+.4f}% vs "
            f"+10 {reportable_plus10[2]:+.4f}% on {len(reportable_plus10[0])} / "
            f"{stratum_counts[REPORTABLE_STRATUM]} stratum rows ({len(reportable_plus10[0])} / {total} total).",
            f"CAVEATED +10 PAIRING: operator {caveated_plus10[1]:+.4f}% vs "
            f"+10 {caveated_plus10[2]:+.4f}% on {len(caveated_plus10[0])} / "
            f"{stratum_counts[CAVEATED_STRATUM]} caveated rows ({len(caveated_plus10[0])} / {total} total).",
            f"BACKSTOP +10 PAIRING: operator {backstop_plus10[1]:+.4f}% vs "
            f"+10 {backstop_plus10[2]:+.4f}% on {len(backstop_plus10[0])} / "
            f"{stratum_counts[BACKSTOP_STRATUM]} backstop rows ({len(backstop_plus10[0])} / {total} total).",
            "",
            f"REPORTABLE REALIZED CONTROL: operator {reportable_realized[1]:+.4f}% vs "
            f"realized {reportable_realized[2]:+.4f}% on {len(reportable_realized[0])} / "
            f"{stratum_counts[REPORTABLE_STRATUM]} stratum rows ({len(reportable_realized[0])} / {total} total).",
            f"CAVEATED REALIZED CONTROL: operator {caveated_realized[1]:+.4f}% vs "
            f"realized {caveated_realized[2]:+.4f}% on {len(caveated_realized[0])} / "
            f"{stratum_counts[CAVEATED_STRATUM]} caveated rows ({len(caveated_realized[0])} / {total} total).",
            f"BACKSTOP REALIZED CONTROL: operator {backstop_realized[1]:+.4f}% vs "
            f"realized {backstop_realized[2]:+.4f}% on {len(backstop_realized[0])} / "
            f"{stratum_counts[BACKSTOP_STRATUM]} backstop rows ({len(backstop_realized[0])} / {total} total).",
            f"STOPPED COUNTERFACTUAL: all {len(stopped_rows)} / "
            f"{stratum_counts[REPORTABLE_STRATUM]} endpoint-independent rows "
            f"({len(stopped_rows)} / {total} total) cost "
            f"{stopped_paid:+.4f}%; matched {len(stopped_counterfactual)} / "
            f"{len(stopped_rows)} compare -8 rule {stopped_paired_paid:+.4f}% vs "
            f"ATR SELL {stopped_unprotected:+.4f}%.",
            "",
            "| stopped | date | sym | -8 rule % | ATR SELL % |",
            "|---:|---|---|---:|---:|",
            *[
                f"| {row['event']} | {row['date_et']} | {row['symbol']} | "
                f"{row['event_return_pct']} | {row['atr_sell_counterfactual_pct']} |"
                for row in stopped_rows
            ],
            "",
            f"Dropped from +5 pairing: {len(dropped_plus5)} rows; operator contribution "
            f"{sum((Decimal(row['event_return_pct']) for row in dropped_plus5), Decimal('0')):+.4f}%.",
            *[
                f"- event {row['event']} {row['date_et']} {row['symbol']}: "
                f"operator {row['event_return_pct']}%; {row['plus5_ungradable_reason']}"
                for row in dropped_plus5
            ],
            f"Dropped from +10 pairing: {len(dropped_plus10)} rows; operator contribution "
            f"{sum((Decimal(row['event_return_pct']) for row in dropped_plus10), Decimal('0')):+.4f}%.",
            *[
                f"- event {row['event']} {row['date_et']} {row['symbol']}: "
                f"operator {row['event_return_pct']}%; {row['plus10_ungradable_reason']}"
                for row in dropped_plus10
            ],
            "",
            "| sym | buy | fill | hi % / hi t | lo % / lo t | exit | px | by | rule % | real % | stratum | denom | depth |",
            "|---|---|---|---|---|---|---|---|---:|---:|---|---:|---|",
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
            f"{row['event_return_pct']}% | {real}% | {row['result_stratum']} | "
            f"{row['stratum_denominator']} | {row['depth_basis']} |"
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
