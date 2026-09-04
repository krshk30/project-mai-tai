#!/usr/bin/env python3
"""Read-only replay of the deployed ORB strategy against captured market data.

The entry signal is produced by ``OrbService`` and ``OrbTickAggregator`` from the
same raw trade series consumed in production. Simulated marketable-limit entries
use the latest fresh NBBO ask visible when that signal reaches the OMS. Position
extrema, trailing stops, and exits use executable NBBO bids only.
"""

from __future__ import annotations

import argparse
import csv
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HaltWindow,
    confirmed_halt_window,
    timestamp_is_halted,
)
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

EASTERN = ZoneInfo("America/New_York")
DISCLOSURE = "SIMULATED | NO REALISED CONTROL | NOT SIZE-QUALIFIED"
FILL_RULE = (
    "latest positive NBBO ask at the signal time, no older than the deployed OMS freshness "
    "limit and no higher than the stamped breakout cap; assumed immediately filled at that ask"
)


@dataclass(frozen=True)
class TradePoint:
    at: datetime
    price: Decimal
    size: int
    exchange: str = ""
    conditions: str = ""


@dataclass(frozen=True)
class QuotePoint:
    at: datetime
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class EntryPlan:
    symbol: str
    at: datetime
    price: Decimal
    metadata: dict[str, object]
    source: str
    note: str


@dataclass
class OpenPosition:
    entry: EntryPlan
    trail_pct: Decimal
    stop: Decimal
    high_water: Decimal
    high_bid: Decimal | None = None
    high_at: datetime | None = None
    low_bid: Decimal | None = None
    low_at: datetime | None = None


@dataclass(frozen=True)
class LedgerRow:
    entry: EntryPlan
    high_bid: Decimal | None
    high_at: datetime | None
    low_bid: Decimal | None
    low_at: datetime | None
    exit_at: datetime | None
    exit_price: Decimal | None
    exit_rule: str
    note: str

    @property
    def high_pct(self) -> Decimal | None:
        return _pct(self.high_bid, self.entry.price)

    @property
    def low_pct(self) -> Decimal | None:
        return _pct(self.low_bid, self.entry.price)

    @property
    def return_pct(self) -> Decimal | None:
        return _pct(self.exit_price, self.entry.price)


@dataclass
class ReplayResult:
    rows: list[LedgerRow] = field(default_factory=list)
    abandoned: dict[str, int] = field(default_factory=dict)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _pct(value: Decimal | None, basis: Decimal) -> Decimal | None:
    if value is None or basis <= 0:
        return None
    return (value / basis - Decimal("1")) * Decimal("100")


def session_bounds(day: date) -> tuple[datetime, datetime, datetime]:
    observe = datetime.combine(day, time(9, 25), EASTERN).astimezone(UTC)
    close = datetime.combine(day, time(16, 0), EASTERN).astimezone(UTC)
    query_start = datetime.combine(day, time(4, 0), EASTERN).astimezone(UTC)
    return query_start, observe, close


def flatten_at(day: date, settings: Settings) -> datetime:
    configured = bool(settings.orb_window_flatten_enabled) and "orb" in {
        value.strip()
        for value in str(settings.orb_window_flatten_strategies).split(",")
        if value.strip()
    }
    if not configured:
        return datetime.combine(day, time(16, 0), EASTERN).astimezone(UTC)
    return datetime.combine(
        day,
        time(
            int(settings.orb_window_flatten_hour_et),
            int(settings.orb_window_flatten_minute_et),
        ),
        EASTERN,
    ).astimezone(UTC)


def dedupe_trades(rows: Iterable[TradePoint]) -> list[TradePoint]:
    seen: set[tuple[datetime, Decimal, int, str, str]] = set()
    result: list[TradePoint] = []
    for row in sorted(rows, key=lambda point: point.at):
        key = (row.at, row.price, row.size, row.exchange, row.conditions)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def detect_halts(trades: Sequence[TradePoint], quotes: Sequence[QuotePoint]) -> list[HaltWindow]:
    """Use the shared replay/live halt definition; no symbol exceptions."""
    quote_times = [quote.at for quote in quotes]
    windows: list[HaltWindow] = []
    for prior, current in zip(trades, trades[1:], strict=False):
        if current.at - prior.at < HALT_MIN_PRINT_GAP:
            continue
        updates = bisect_left(quote_times, current.at) - bisect_right(quote_times, prior.at)
        window = confirmed_halt_window(
            last_print_at=prior.at,
            reopen_print_at=current.at,
            quote_updates=updates,
        )
        if window is not None:
            windows.append(window)
    return windows


def choose_entry_population(
    real_entries: Sequence[EntryPlan], simulated_entries: Sequence[EntryPlan]
) -> list[EntryPlan]:
    """A session with durable ORB fills is never replaced by simulated entries."""
    return list(real_entries if real_entries else simulated_entries)


def stamped_trail_pct(metadata: dict[str, object]) -> Decimal | None:
    """Read the level carried by the intent; never fall back to current configuration."""
    value = metadata.get("trail_pct")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result > 0 else None


def observe_bid(position: OpenPosition, quote: QuotePoint) -> str | None:
    """Apply the production bid-only ratchet and return a triggered exit rule."""
    bid = quote.bid
    if position.high_bid is None or bid > position.high_bid:
        position.high_bid = bid
        position.high_at = quote.at
    if position.low_bid is None or bid < position.low_bid:
        position.low_bid = bid
        position.low_at = quote.at
    stop, high_water = OmsRiskService._ratcheted_trailing_stop(
        position.stop,
        position.high_water,
        bid,
        float(position.trail_pct),
    )
    position.stop = stop
    position.high_water = high_water
    return f"TRAIL-{position.trail_pct.normalize()}%" if bid <= position.stop else None


def close_position(
    position: OpenPosition,
    *,
    quote: QuotePoint | None,
    rule: str,
    note: str = "",
) -> LedgerRow:
    return LedgerRow(
        entry=position.entry,
        high_bid=position.high_bid,
        high_at=position.high_at,
        low_bid=position.low_bid,
        low_at=position.low_at,
        exit_at=quote.at if quote else None,
        exit_price=quote.bid if quote else None,
        exit_rule=rule,
        note=note,
    )


def evaluate_entry(
    entry: EntryPlan,
    quotes: Sequence[QuotePoint],
    halts: Sequence[HaltWindow],
    flatten_time: datetime,
) -> LedgerRow:
    """Evaluate one stamped/real entry using executable bids and halt exclusion."""
    trail = stamped_trail_pct(entry.metadata)
    if trail is None:
        return LedgerRow(
            entry, None, None, None, None, None, None, "UNANSWERABLE", "missing stamped trail_pct"
        )
    position = OpenPosition(
        entry=entry,
        trail_pct=trail,
        stop=entry.price * (Decimal("1") - trail / Decimal("100")),
        high_water=entry.price,
    )
    for quote in quotes:
        if quote.at < entry.at or quote.bid <= 0 or timestamp_is_halted(quote.at, list(halts)):
            continue
        trigger = observe_bid(position, quote)
        if trigger is not None:
            return close_position(position, quote=quote, rule=trigger)
        if quote.at >= flatten_time:
            return close_position(position, quote=quote, rule="WINDOW_FLATTEN")
    return close_position(
        position,
        quote=None,
        rule="UNANSWERABLE",
        note="no executable non-halted bid reached an exit by 16:00 ET",
    )


def _price_simulated_entry(
    event,
    *,
    settings: Settings,
    quote: QuotePoint | None,
    signal_at: datetime,
) -> tuple[Decimal | None, str]:
    """Call the deployed OMS quote-pricing path, replacing only its persistence side effect."""
    oms = OmsRiskService.__new__(OmsRiskService)
    oms.settings = settings
    oms.logger = logging.getLogger("orb-backtest")
    oms._latest_quotes_by_symbol = {}
    if quote is not None:
        oms._latest_quotes_by_symbol[event.payload.symbol] = {
            "bid": float(quote.bid),
            "ask": float(quote.ask),
            "received_at": quote.at,
        }

    def abandon(**kwargs):
        return str(kwargs["reason_code"])

    oms._abandon_orb_entry = abandon
    with patch("project_mai_tai.oms.service.utcnow", return_value=signal_at):
        rejected = OmsRiskService._apply_orb_quote_priced_entry(
            oms,
            session=None,
            event=event,
            intent=object(),
        )
    if rejected is not None:
        return None, str(rejected)
    ask = event.payload.metadata.get("oms_quote_ask")
    return (Decimal(str(ask)), "") if ask is not None else (None, "NO_ASSUMED_FILL")


def _configured_service(settings: Settings, day: date, universe: set[str]) -> OrbService:
    service = OrbService(settings=settings, redis_client=object(), session_factory=None)
    open_at = datetime.combine(day, time(9, 30), EASTERN).astimezone(UTC)
    observe_at = datetime.combine(day, time(9, 25), EASTERN).astimezone(UTC)
    service._session_open_utc = lambda: open_at
    service._observe_open_utc = lambda: observe_at
    service._universe = set(universe)
    return service


def replay_simulated_symbol(
    *,
    day: date,
    symbol: str,
    trades: Sequence[TradePoint],
    quotes: Sequence[QuotePoint],
    settings: Settings,
    universe: set[str],
) -> ReplayResult:
    """Replay one symbol, using production ORB bars/signals and OMS entry pricing."""
    service = _configured_service(settings, day, universe)
    aggregator = OrbTickAggregator(
        session_open=datetime.combine(day, time(9, 25), EASTERN).astimezone(UTC)
    )
    halts = detect_halts(trades, quotes)
    flatten_time = flatten_at(day, settings)
    result = ReplayResult()
    position: OpenPosition | None = None
    quote_index = 0
    latest_quote: QuotePoint | None = None

    for trade in trades:
        while quote_index < len(quotes) and quotes[quote_index].at <= trade.at:
            quote = quotes[quote_index]
            quote_index += 1
            if timestamp_is_halted(quote.at, halts):
                continue
            latest_quote = quote
            if position is None or quote.at < position.entry.at or quote.bid <= 0:
                continue
            trigger = observe_bid(position, quote)
            if trigger is not None or quote.at >= flatten_time:
                rule = trigger or "WINDOW_FLATTEN"
                result.rows.append(close_position(position, quote=quote, rule=rule))
                service._apply_order_event(
                    symbol=symbol,
                    side="sell",
                    event_type="filled",
                    quantity=1.0,
                    payload={"fill_price": str(quote.bid)},
                )
                position = None

        bar = aggregator.add_tick(trade.at, float(trade.price), float(trade.size))
        if bar is None:
            continue
        service._on_bar(symbol, bar)
        pending, service._pending_intents = service._pending_intents, []
        for pending_symbol, signal_level in pending:
            event = service._build_open_intent(pending_symbol, signal_level)
            entry_price, reason = _price_simulated_entry(
                event,
                settings=settings,
                quote=latest_quote,
                signal_at=trade.at,
            )
            if entry_price is None or timestamp_is_halted(trade.at, halts):
                reason = "SIGNAL_IN_HALT" if timestamp_is_halted(trade.at, halts) else reason
                result.abandoned[reason] = result.abandoned.get(reason, 0) + 1
                service._apply_order_event(
                    symbol=pending_symbol,
                    side="buy",
                    event_type="rejected",
                    quantity=1.0,
                    payload={"reason": reason},
                )
                continue
            metadata = dict(event.payload.metadata)
            entry = EntryPlan(
                symbol=pending_symbol,
                at=trade.at,
                price=entry_price,
                metadata=metadata,
                source="SIMULATED",
                note="ASSUMED ASK FILL",
            )
            trail = stamped_trail_pct(metadata)
            if trail is None:
                result.rows.append(
                    LedgerRow(
                        entry,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "UNANSWERABLE",
                        "missing stamped trail_pct",
                    )
                )
                continue
            position = OpenPosition(
                entry=entry,
                trail_pct=trail,
                stop=entry_price * (Decimal("1") - trail / Decimal("100")),
                high_water=entry_price,
            )
            service._apply_order_event(
                symbol=pending_symbol,
                side="buy",
                event_type="filled",
                quantity=1.0,
                payload={"fill_price": str(entry_price)},
            )

    while quote_index < len(quotes) and position is not None:
        quote = quotes[quote_index]
        quote_index += 1
        if quote.at < position.entry.at or quote.bid <= 0 or timestamp_is_halted(quote.at, halts):
            continue
        trigger = observe_bid(position, quote)
        if trigger is not None or quote.at >= flatten_time:
            result.rows.append(
                close_position(position, quote=quote, rule=trigger or "WINDOW_FLATTEN")
            )
            position = None
    if position is not None:
        result.rows.append(
            close_position(
                position,
                quote=None,
                rule="UNANSWERABLE",
                note="no executable non-halted bid reached an exit by 16:00 ET",
            )
        )
    return result


def _metadata(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("metadata")
    return dict(nested) if isinstance(nested, dict) else dict(payload)


def load_real_entries(session_factory, day: date) -> list[EntryPlan]:
    start, _, close = session_bounds(day)
    sql = """
        SELECT f.symbol, f.filled_at, f.price, ti.payload, bo.payload, f.payload
        FROM fills f
        JOIN broker_orders bo ON bo.id=f.order_id
        JOIN strategies s ON s.id=f.strategy_id
        LEFT JOIN trade_intents ti ON ti.id=bo.intent_id
        WHERE s.code='orb' AND lower(f.side)='buy'
          AND f.filled_at>=:start AND f.filled_at<=:close
        ORDER BY f.filled_at, f.id
    """
    with session_factory() as session:
        rows = session.execute(text(sql), {"start": start, "close": close}).all()
    entries = []
    for symbol, filled_at, price, intent_payload, order_payload, fill_payload in rows:
        metadata: dict[str, object] = {}
        for payload in (intent_payload, order_payload, fill_payload):
            metadata.update(_metadata(payload))
        entries.append(
            EntryPlan(
                str(symbol).upper(),
                _utc(filled_at),
                Decimal(str(price)),
                metadata,
                "REAL_FILL",
                "READ FROM DURABLE FILL",
            )
        )
    return entries


def load_universe(session_factory, day: date) -> set[str]:
    cutoff = datetime.combine(day, time(9, 25), EASTERN).astimezone(UTC)
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (symbol) symbol, event_type
            FROM scanner_confirmed_events
            WHERE trade_date=:day AND event_at<=:cutoff
            ORDER BY symbol, event_at DESC, id DESC
        )
        SELECT symbol FROM latest WHERE event_type='CONFIRM' ORDER BY symbol
    """
    with session_factory() as session:
        return {
            str(row[0]).upper()
            for row in session.execute(text(sql), {"day": day, "cutoff": cutoff})
        }


def load_market(
    session_factory, day: date, symbol: str
) -> tuple[list[TradePoint], list[QuotePoint]]:
    start, _, close = session_bounds(day)
    end = close + timedelta(minutes=1)
    with session_factory() as session:
        trades = [
            TradePoint(
                _utc(row[0]),
                Decimal(str(row[1])),
                int(row[2] or 0),
                str(row[3] or ""),
                str(row[4] or ""),
            )
            for row in session.execute(
                text(
                    "SELECT event_ts,price,size,exchange,conditions FROM market_capture_trades "
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
            if Decimal(str(row[1])) > 0
        ]
        quotes = [
            QuotePoint(_utc(row[0]), Decimal(str(row[1])), Decimal(str(row[2])))
            for row in session.execute(
                text(
                    "SELECT event_ts,bid_price,ask_price FROM market_capture_quotes "
                    "WHERE symbol=:symbol AND event_ts>=:start AND event_ts<=:end "
                    "AND bid_price IS NOT NULL AND ask_price IS NOT NULL ORDER BY event_ts,id"
                ),
                {"symbol": symbol, "start": start, "end": end},
            )
        ]
    return dedupe_trades(trades), quotes


def run_day(session_factory, settings: Settings, day: date) -> ReplayResult:
    universe = load_universe(session_factory, day)
    real_entries = load_real_entries(session_factory, day)
    result = ReplayResult()
    if real_entries:
        by_symbol: dict[str, list[EntryPlan]] = {}
        for entry in real_entries:
            by_symbol.setdefault(entry.symbol, []).append(entry)
        for symbol, entries in by_symbol.items():
            trades, quotes = load_market(session_factory, day, symbol)
            halts = detect_halts(trades, quotes)
            for entry in choose_entry_population(entries, []):
                result.rows.append(evaluate_entry(entry, quotes, halts, flatten_at(day, settings)))
        return result

    for symbol in sorted(universe):
        trades, quotes = load_market(session_factory, day, symbol)
        replay = replay_simulated_symbol(
            day=day,
            symbol=symbol,
            trades=trades,
            quotes=quotes,
            settings=settings,
            universe=universe,
        )
        result.rows.extend(replay.rows)
        for reason, count in replay.abandoned.items():
            result.abandoned[reason] = result.abandoned.get(reason, 0) + count
    return result


def _clock(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return "-"
    return value.astimezone(EASTERN).strftime("%H:%M:%S" if seconds else "%H:%M")


def _money(value: Decimal | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def _percent(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def render(rows: Sequence[LedgerRow], abandoned: dict[str, int]) -> str:
    ordered = sorted(rows, key=lambda row: row.entry.at)
    total = len(ordered)
    multiple_days = len({row.entry.at.astimezone(EASTERN).date() for row in ordered}) > 1
    lines = [DISCLOSURE, f"Fill rule: {FILL_RULE}", ""]
    date_header = " date |" if multiple_days else ""
    lines.append(
        f"|{date_header} sym | entry time (ET) | entry price | high % / minute | low % / minute | exit time | exit price | exit rule | return | assumption |"
    )
    lines.append(
        "|" + ("---|" if multiple_days else "") + "---|---:|---:|---:|---:|---:|---:|---|---:|---|"
    )
    for row in ordered:
        values = [
            row.entry.symbol,
            _clock(row.entry.at, seconds=True),
            _money(row.entry.price),
            f"{_percent(row.high_pct)} / {_clock(row.high_at)}",
            f"{_percent(row.low_pct)} / {_clock(row.low_at)}",
            _clock(row.exit_at, seconds=True),
            _money(row.exit_price),
            row.exit_rule,
            _percent(row.return_pct),
            row.note or row.entry.note,
        ]
        if multiple_days:
            values.insert(0, row.entry.at.astimezone(EASTERN).date().isoformat())
        lines.append("| " + " | ".join(values) + " |")
    gradable = [row for row in ordered if row.return_pct is not None]
    total_return = sum(
        (row.return_pct for row in gradable if row.return_pct is not None), Decimal("0")
    )
    counts: dict[str, int] = {}
    for row in ordered:
        counts[row.exit_rule] = counts.get(row.exit_rule, 0) + 1
    lines.extend(
        [
            "",
            f"Entries: {len(gradable)}/{total} gradable; {total - len(gradable)}/{total} UNANSWERABLE.",
            f"Total return: {_percent(total_return)} across {len(gradable)}/{total} gradable entries.",
            "Exit counts: "
            + ", ".join(f"{name} {count}/{total}" for name, count in sorted(counts.items())),
        ]
    )
    if abandoned:
        attempts = total + sum(abandoned.values())
        lines.append(
            "Abandoned signal attempts: "
            + ", ".join(
                f"{reason} {count}/{attempts}" for reason, count in sorted(abandoned.items())
            )
        )
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[LedgerRow]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_date",
                "symbol",
                "entry_time_et",
                "entry_price",
                "high_pct",
                "high_at_et",
                "low_pct",
                "low_at_et",
                "exit_time_et",
                "exit_price",
                "exit_rule",
                "return_pct",
                "source",
                "assumption",
                "qualification",
            ],
        )
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item.entry.at):
            writer.writerow(
                {
                    "session_date": row.entry.at.astimezone(EASTERN).date().isoformat(),
                    "symbol": row.entry.symbol,
                    "entry_time_et": _clock(row.entry.at, seconds=True),
                    "entry_price": row.entry.price,
                    "high_pct": f"{row.high_pct:.4f}" if row.high_pct is not None else "",
                    "high_at_et": _clock(row.high_at),
                    "low_pct": f"{row.low_pct:.4f}" if row.low_pct is not None else "",
                    "low_at_et": _clock(row.low_at),
                    "exit_time_et": _clock(row.exit_at, seconds=True),
                    "exit_price": row.exit_price or "",
                    "exit_rule": row.exit_rule,
                    "return_pct": (f"{row.return_pct:.4f}" if row.return_pct is not None else ""),
                    "source": row.entry.source,
                    "assumption": row.note or row.entry.note,
                    "qualification": DISCLOSURE,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_day = args.end_date or args.start_date
    if end_day < args.start_date:
        raise SystemExit("--end-date must not precede --start-date")
    settings = get_settings()
    session_factory = build_session_factory(settings)
    rows: list[LedgerRow] = []
    abandoned: dict[str, int] = {}
    day = args.start_date
    while day <= end_day:
        replay = run_day(session_factory, settings, day)
        rows.extend(replay.rows)
        for reason, count in replay.abandoned.items():
            abandoned[reason] = abandoned.get(reason, 0) + count
        day += timedelta(days=1)
    print(render(rows, abandoned))
    if args.csv:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
