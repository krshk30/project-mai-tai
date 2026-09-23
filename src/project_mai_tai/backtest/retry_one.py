"""Causal RETRY-ONE study over durable v2 episodes, fills, and stored bars.

Observed legs use their real broker fills. A counterfactual retry is added only after every
filled sibling in the preceding logical fanout trip has a durable supported close and stored
bars prove a later fresh cross. Counterfactual exits use the live static bracket (+5%/-8%);
when target and stop are both touched in one minute, the stop wins.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

ET = ZoneInfo("America/New_York")
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
SUPPORTED_CLOSES = frozenset(
    {"CONFIRMATION_EXIT", "CW_HARD_STOP", "CW_FLOOR", "CW_FLIP", "OCO_RESOLVED_FLAT"}
)
TARGET_MULTIPLIER = Decimal("1.05")
STOP_MULTIPLIER = Decimal("0.92")
SESSION_END = time(16)


class EvidenceUnknown(RuntimeError):
    """The requested population cannot be graded without inventing evidence."""


@dataclass(frozen=True)
class Bar:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ManagedRow:
    row_id: str
    account: str
    symbol: str
    entry_at: datetime
    closed_at: datetime
    quantity: Decimal


@dataclass(frozen=True)
class FillRow:
    fill_id: str
    account: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    order_quantity: Decimal
    order_type: str
    reason: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class StudyInput:
    managed_rows: tuple[ManagedRow, ...]
    fills: tuple[FillRow, ...]
    bars: dict[tuple[date, str], tuple[Bar, ...]]


@dataclass(frozen=True)
class LegResult:
    account: str
    source: str
    entry_order_type: str
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    return_pct: Decimal
    exit_reason: str


@dataclass(frozen=True)
class LogicalTrip:
    day: date
    symbol: str
    segment_id: str
    opened_at: datetime
    closed_at: datetime
    line: Decimal
    source: str
    fresh_cross_at: datetime | None
    legs: tuple[LegResult, ...]


@dataclass(frozen=True)
class VariantResult:
    max_retries: int
    logical_trips: int
    broker_trades: int
    winners: int
    losers: int
    flats: int
    return_sum_pct: Decimal
    by_account: dict[str, dict[str, object]]
    names: tuple[str, ...]


@dataclass(frozen=True)
class RetryOneReport:
    source_commit: str
    start: date
    end: date
    managed_rows: int
    mapped_legs: int
    unknown_rows: int
    observed_logical_trips: int
    causal_logical_trips: int
    synthetic_logical_trips: int
    noncausal_observed_trips: int
    as_traded: dict[str, object]
    variants: tuple[VariantResult, ...]
    comparisons: dict[str, object]
    forgone_winners_at_one_retry: tuple[str, ...]
    vsa_replay: dict[str, object]
    dcoy_replay: dict[str, object]
    known_limits: tuple[str, ...]


class RetryOneDataSource(Protocol):
    def load(self, start: date, end: date) -> StudyInput: ...


def _metadata(intent_payload: object, order_payload: object) -> dict[str, object]:
    result: dict[str, object] = {}
    if isinstance(intent_payload, dict):
        nested = intent_payload.get("metadata")
        if isinstance(nested, dict):
            result.update(nested)
    if isinstance(order_payload, dict):
        result.update(order_payload)
    return result


def _exit_reason(fill: FillRow) -> str:
    prefix = "oms_v2_managed_exit:"
    if fill.reason.startswith(prefix):
        return fill.reason[len(prefix) :].strip().upper()
    if fill.order_type.strip().lower() == "oco_exit":
        return "OCO_RESOLVED_FLAT"
    return fill.reason.strip().upper() or "UNKNOWN"


def _line(fill: FillRow) -> Decimal:
    for key in ("cw_flip_level", "stop_price", "entry_price", "reference_price"):
        try:
            value = Decimal(str(fill.metadata.get(key, "")))
        except Exception:  # noqa: BLE001 - malformed historical metadata remains explicit
            continue
        if value > 0:
            return value
    return fill.price


def _segment(fill: FillRow, row: ManagedRow) -> str:
    value = str(fill.metadata.get("fanout_segment_id", "") or "").strip()
    return value or f"row:{row.row_id}"


def _weighted_exit(
    fills: list[FillRow], quantity: Decimal
) -> tuple[datetime, Decimal, str] | None:
    remaining = quantity
    proceeds = Decimal("0")
    used = Decimal("0")
    terminal: FillRow | None = None
    for fill in sorted(fills, key=lambda item: item.filled_at):
        take = min(remaining, fill.quantity)
        if take <= 0:
            continue
        proceeds += take * fill.price
        used += take
        remaining -= take
        terminal = fill
        if remaining <= 0:
            break
    if remaining > 0 or terminal is None or used <= 0:
        return None
    return terminal.filled_at, proceeds / used, _exit_reason(terminal)


def _map_observed_trips(data: StudyInput) -> tuple[list[LogicalTrip], int]:
    fills_by_key: dict[tuple[str, str], list[FillRow]] = defaultdict(list)
    for fill in data.fills:
        fills_by_key[(fill.account, fill.symbol)].append(fill)
    used_buys: set[str] = set()
    grouped: dict[tuple[date, str, str], list[LegResult]] = defaultdict(list)
    group_lines: dict[tuple[date, str, str], list[Decimal]] = defaultdict(list)
    unknown = 0
    for row in sorted(data.managed_rows, key=lambda item: item.entry_at):
        candidates = [
            fill
            for fill in fills_by_key[(row.account, row.symbol)]
            if fill.side == "buy"
            and fill.fill_id not in used_buys
            and row.entry_at - timedelta(minutes=2) <= fill.filled_at
            and fill.filled_at <= row.entry_at + timedelta(seconds=15)
        ]
        if not candidates:
            unknown += 1
            continue
        entry = min(candidates, key=lambda fill: abs(fill.filled_at - row.entry_at))
        used_buys.add(entry.fill_id)
        exits = [
            fill
            for fill in fills_by_key[(row.account, row.symbol)]
            if fill.side == "sell"
            and fill.filled_at >= entry.filled_at
            and fill.filled_at <= row.closed_at + timedelta(seconds=3)
        ]
        terminal = _weighted_exit(exits, row.quantity)
        if terminal is None:
            unknown += 1
            continue
        exit_at, exit_price, reason = terminal
        return_pct = (exit_price / entry.price - Decimal("1")) * Decimal("100")
        day = entry.filled_at.astimezone(ET).date()
        segment = _segment(entry, row)
        key = (day, row.symbol, segment)
        grouped[key].append(
            LegResult(
                account=row.account,
                source="observed",
                entry_order_type=entry.order_type,
                entry_at=entry.filled_at,
                exit_at=exit_at,
                entry_price=entry.price,
                exit_price=exit_price,
                return_pct=return_pct,
                exit_reason=reason,
            )
        )
        group_lines[key].append(_line(entry))

    trips = []
    for (day, symbol, segment), legs in grouped.items():
        ordered = tuple(sorted(legs, key=lambda item: item.account))
        lines = sorted(group_lines[(day, symbol, segment)])
        trips.append(
            LogicalTrip(
                day=day,
                symbol=symbol,
                segment_id=segment,
                opened_at=min(leg.entry_at for leg in ordered),
                closed_at=max(leg.exit_at for leg in ordered),
                line=lines[len(lines) // 2],
                source="observed",
                fresh_cross_at=None,
                legs=ordered,
            )
        )
    return sorted(trips, key=lambda item: (item.day, item.symbol, item.opened_at)), unknown


def _fresh_cross(
    bars: tuple[Bar, ...],
    *,
    after: datetime,
    line: Decimal,
    before: datetime | None = None,
) -> datetime | None:
    armed = False
    for bar in bars:
        if bar.at <= after or (before is not None and bar.at > before):
            continue
        if not armed:
            if bar.close < line:
                armed = True
            continue
        if bar.high >= line:
            return bar.at
    return None


def _armed_below_line(
    bars: tuple[Bar, ...], *, after: datetime, line: Decimal, before: datetime
) -> bool:
    return any(
        bar.at > after and bar.at <= before and bar.close < line
        for bar in bars
    )


def _model_counterfactual(
    bars: tuple[Bar, ...],
    *,
    day: date,
    symbol: str,
    line: Decimal,
    after: datetime,
    index: int,
) -> LogicalTrip | None:
    session_end = datetime.combine(day, SESSION_END, tzinfo=ET).astimezone(UTC)
    crossed_at = _fresh_cross(bars, after=after, line=line, before=session_end)
    if crossed_at is None:
        return None
    target = line * TARGET_MULTIPLIER
    stop = line * STOP_MULTIPLIER
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason = ""
    last_close: Decimal | None = None
    for bar in bars:
        # The one-minute bar proves the cross but cannot order its pre-cross low against its high.
        # Outcome grading therefore begins on the next bar rather than inventing an intrabar path.
        if bar.at <= crossed_at or bar.at > session_end:
            continue
        last_close = bar.close
        stop_hit = bar.low <= stop
        target_hit = bar.high >= target
        if stop_hit:
            exit_at, exit_price, exit_reason = bar.at, stop, "STOP"
            break
        if target_hit:
            exit_at, exit_price, exit_reason = bar.at, target, "TARGET"
            break
    if exit_at is None or exit_price is None:
        if last_close is None:
            return None
        exit_at, exit_price, exit_reason = session_end, last_close, "SESSION_END"
    return_pct = (exit_price / line - Decimal("1")) * Decimal("100")
    legs = tuple(
        LegResult(
            account=account,
            source="modeled",
            entry_order_type="MODELED_STOP",
            entry_at=crossed_at,
            exit_at=exit_at,
            entry_price=line,
            exit_price=exit_price,
            return_pct=return_pct,
            exit_reason=exit_reason,
        )
        for account in ACCOUNTS
    )
    return LogicalTrip(
        day=day,
        symbol=symbol,
        segment_id=f"modeled:{index}",
        opened_at=crossed_at,
        closed_at=exit_at,
        line=line,
        source="modeled",
        fresh_cross_at=crossed_at,
        legs=legs,
    )


def _causal_sequences(
    observed: list[LogicalTrip], data: StudyInput
) -> tuple[dict[tuple[date, str], list[LogicalTrip]], int]:
    by_name: dict[tuple[date, str], list[LogicalTrip]] = defaultdict(list)
    for trip in observed:
        by_name[(trip.day, trip.symbol)].append(trip)
    noncausal = 0
    for key, trips in by_name.items():
        trips.sort(key=lambda item: item.opened_at)
        causal = [trips[0]]
        bars = data.bars.get(key, ())
        for trip in trips[1:]:
            prior = causal[-1]
            crossed_at = _fresh_cross(
                bars,
                after=prior.closed_at,
                line=trip.line,
                before=trip.opened_at,
            )
            if (
                crossed_at is None
                and any(leg.entry_order_type.upper() == "STOP_LIMIT" for leg in trip.legs)
                and _armed_below_line(
                    bars,
                    after=prior.closed_at,
                    line=trip.line,
                    before=trip.opened_at,
                )
            ):
                # A broker STOP_LIMIT fill is definitive cross evidence. Stored minute bars can
                # round the high by one tick (DCOY 11:03: 5.7900 vs trigger 5.7912), so the fill
                # resolves that boundary without inventing a crossing price.
                crossed_at = trip.opened_at
            if crossed_at is None:
                noncausal += 1
                continue
            causal.append(replace(trip, fresh_cross_at=crossed_at))
        while len(causal) < 3:
            prior = causal[-1]
            if not prior.legs or any(
                leg.exit_reason not in SUPPORTED_CLOSES and leg.source == "observed"
                for leg in prior.legs
            ):
                break
            modeled = _model_counterfactual(
                bars,
                day=key[0],
                symbol=key[1],
                line=prior.line,
                after=prior.closed_at,
                index=len(causal) + 1,
            )
            if modeled is None:
                break
            causal.append(modeled)
        by_name[key] = causal
    return by_name, noncausal


def _summary(legs: list[LegResult]) -> dict[str, object]:
    winners = sum(leg.return_pct > 0 for leg in legs)
    losers = sum(leg.return_pct < 0 for leg in legs)
    return {
        "trades": len(legs),
        "winners": winners,
        "losers": losers,
        "flats": len(legs) - winners - losers,
        "return_sum_pct": str(sum((leg.return_pct for leg in legs), Decimal("0")).quantize(Decimal("0.0001"))),
    }


def _variant(
    sequences: dict[tuple[date, str], list[LogicalTrip]], max_retries: int
) -> VariantResult:
    selected = [trip for trips in sequences.values() for trip in trips[: 1 + max_retries]]
    legs = [leg for trip in selected for leg in trip.legs]
    by_account = {
        account: _summary([leg for leg in legs if leg.account == account])
        for account in ACCOUNTS
    }
    total = _summary(legs)
    return VariantResult(
        max_retries=max_retries,
        logical_trips=len(selected),
        broker_trades=int(total["trades"]),
        winners=int(total["winners"]),
        losers=int(total["losers"]),
        flats=int(total["flats"]),
        return_sum_pct=Decimal(str(total["return_sum_pct"])),
        by_account=by_account,
        names=tuple(sorted({f"{trip.day}:{trip.symbol}" for trip in selected})),
    )


def evaluate(
    data: StudyInput, start: date, end: date, *, source_commit: str = ""
) -> RetryOneReport:
    observed, unknown = _map_observed_trips(data)
    sequences, noncausal = _causal_sequences(observed, data)
    variants = tuple(_variant(sequences, value) for value in (0, 1, 2))
    observed_legs = [leg for trip in observed for leg in trip.legs]
    at_one = {trip.segment_id for trips in sequences.values() for trip in trips[:2]}
    forgone = sorted(
        {
            f"{trip.day}:{trip.symbol}:{leg.account}:{trip.opened_at.astimezone(ET):%H:%M}"
            for trips in sequences.values()
            for trip in trips[2:]
            for leg in trip.legs
            if leg.return_pct > 0 and trip.segment_id not in at_one
        }
    )

    def named(day: date, symbol: str) -> dict[str, object]:
        trips = sequences.get((day, symbol), [])
        return {
            "logical_trips": len(trips),
            "trips": [
                {
                    "source": trip.source,
                    "opened_at_et": trip.opened_at.astimezone(ET).isoformat(),
                    "closed_at_et": trip.closed_at.astimezone(ET).isoformat(),
                    "line": str(trip.line),
                    "fresh_cross_at_et": (
                        trip.fresh_cross_at.astimezone(ET).isoformat()
                        if trip.fresh_cross_at is not None
                        else None
                    ),
                    "legs": [
                        {
                            **asdict(leg),
                            "entry_at": leg.entry_at.astimezone(ET).isoformat(),
                            "exit_at": leg.exit_at.astimezone(ET).isoformat(),
                            "entry_price": str(leg.entry_price),
                            "exit_price": str(leg.exit_price),
                            "return_pct": str(leg.return_pct.quantize(Decimal("0.0001"))),
                        }
                        for leg in trip.legs
                    ],
                }
                for trip in trips
            ],
        }

    return RetryOneReport(
        source_commit=source_commit,
        start=start,
        end=end,
        managed_rows=len(data.managed_rows),
        mapped_legs=len(observed_legs),
        unknown_rows=unknown,
        observed_logical_trips=len(observed),
        causal_logical_trips=sum(len(value) for value in sequences.values()),
        synthetic_logical_trips=sum(
            trip.source == "modeled" for trips in sequences.values() for trip in trips
        ),
        noncausal_observed_trips=noncausal,
        as_traded=_summary(observed_legs),
        variants=variants,
        comparisons={
            "max_one_minus_as_traded_return_pct": str(
                (
                    variants[1].return_sum_pct
                    - Decimal(str(_summary(observed_legs)["return_sum_pct"]))
                ).quantize(Decimal("0.0001"))
            ),
            "max_one_minus_max_zero_return_pct": str(
                (variants[1].return_sum_pct - variants[0].return_sum_pct).quantize(
                    Decimal("0.0001")
                )
            ),
            "max_one_better_than_as_traded": (
                variants[1].return_sum_pct
                > Decimal(str(_summary(observed_legs)["return_sum_pct"]))
            ),
            "max_one_better_than_max_zero": (
                variants[1].return_sum_pct > variants[0].return_sum_pct
            ),
        },
        forgone_winners_at_one_retry=tuple(forgone),
        vsa_replay=named(date(2026, 9, 23), "VSA"),
        dcoy_replay=named(date(2026, 9, 22), "DCOY"),
        known_limits=(
            "Observed legs use real fills; only missing tail retries are modeled.",
            "A fresh cross requires one completed stored bar below the line and a later bar high at or above it.",
            "Sparse Schwab bars can make a real fresh cross unmeasurable; those observed trips are excluded, never inferred.",
            "The cross bar proves entry only; outcome grading starts on the next bar because its intrabar order is unknown.",
            "Counterfactual target and stop touched in the same post-entry minute is scored STOP.",
        ),
    )


class DbRetryOneDataSource:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def load(self, start: date, end: date) -> StudyInput:
        end_exclusive = end + timedelta(days=1)
        with self._sf() as session:
            managed = session.execute(
                text(
                    "SELECT id, broker_account_name, symbol, entry_time, updated_at, "
                    "original_quantity FROM oms_managed_positions "
                    "WHERE strategy_code='schwab_1m_v2' AND status='closed' "
                    "AND current_quantity=0 "
                    "AND (entry_time AT TIME ZONE 'America/New_York')::date BETWEEN :start AND :end "
                    "AND broker_account_name IN ('live:schwab_1m_v2','live:orb') "
                    "ORDER BY entry_time"
                ),
                {"start": start, "end": end},
            ).all()
            fill_rows = session.execute(
                text(
                    "SELECT f.id, a.name, f.symbol, f.side, f.quantity, f.price, f.filled_at, "
                    "o.quantity, o.order_type, COALESCE(i.reason,''), i.payload, o.payload "
                    "FROM fills f JOIN broker_orders o ON o.id=f.order_id "
                    "JOIN broker_accounts a ON a.id=f.broker_account_id "
                    "JOIN strategies s ON s.id=f.strategy_id "
                    "LEFT JOIN trade_intents i ON i.id=o.intent_id "
                    "WHERE s.code='schwab_1m_v2' "
                    "AND f.filled_at >= (CAST(:start AS date) - interval '2 minutes') "
                    "AND f.filled_at < (CAST(:end_exclusive AS date) + interval '5 minutes') "
                    "AND a.name IN ('live:schwab_1m_v2','live:orb') "
                    "ORDER BY f.filled_at"
                ),
                {"start": start, "end_exclusive": end_exclusive},
            ).all()
            symbols = sorted({str(row[2]).upper() for row in managed})
            bars = (
                session.execute(
                    text(
                        "SELECT DISTINCT ON (symbol,bar_time) symbol,bar_time,open_price,"
                        "high_price,low_price,close_price FROM strategy_bar_history "
                        "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
                        "AND symbol = ANY(:symbols) "
                        "AND (bar_time AT TIME ZONE 'America/New_York')::date BETWEEN :start AND :end "
                        "ORDER BY symbol,bar_time,CASE WHEN source='live' THEN 0 ELSE 1 END"
                    ),
                    {"symbols": symbols, "start": start, "end": end},
                ).all()
                if symbols
                else []
            )

        managed_rows = tuple(
            ManagedRow(
                row_id=str(row[0]),
                account=str(row[1]),
                symbol=str(row[2]).upper(),
                entry_at=row[3].astimezone(UTC),
                closed_at=row[4].astimezone(UTC),
                quantity=Decimal(row[5]),
            )
            for row in managed
        )
        fills = tuple(
            FillRow(
                fill_id=str(row[0]),
                account=str(row[1]),
                symbol=str(row[2]).upper(),
                side=str(row[3]).lower(),
                quantity=Decimal(row[4]),
                price=Decimal(row[5]),
                filled_at=row[6].astimezone(UTC),
                order_quantity=Decimal(row[7]),
                order_type=str(row[8] or ""),
                reason=str(row[9] or ""),
                metadata=_metadata(row[10], row[11]),
            )
            for row in fill_rows
        )
        bars_by_key: dict[tuple[date, str], list[Bar]] = defaultdict(list)
        for symbol, at, open_price, high, low, close in bars:
            normalized_at = at.astimezone(UTC)
            bars_by_key[(normalized_at.astimezone(ET).date(), str(symbol).upper())].append(
                Bar(
                    at=normalized_at,
                    open=Decimal(open_price),
                    high=Decimal(high),
                    low=Decimal(low),
                    close=Decimal(close),
                )
            )
        return StudyInput(
            managed_rows=managed_rows,
            fills=fills,
            bars={key: tuple(value) for key, value in bars_by_key.items()},
        )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def render_markdown(report: RetryOneReport) -> str:
    lines = [
        "# RETRY-ONE causal backtest",
        "",
        f"Window: {report.start} through {report.end}. Both live broker legs; no dollars.",
        f"Source commit: `{report.source_commit}`.",
        "",
        "## Population",
        "",
        f"- Managed rows: {report.managed_rows}; fill-mapped legs: {report.mapped_legs}; unknown: {report.unknown_rows}.",
        f"- Observed logical trips: {report.observed_logical_trips}; causal trips including modeled tails: {report.causal_logical_trips}; modeled: {report.synthetic_logical_trips}.",
        f"- Observed later trips without stored fresh-cross proof: {report.noncausal_observed_trips} (excluded).",
        "",
        "## Results",
        "",
        "| Max retries | Logical trips | Broker trades | Winners | Losers | Flats | Sum return % |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report.variants:
        lines.append(
            f"| {result.max_retries} | {result.logical_trips} | {result.broker_trades} | "
            f"{result.winners} | {result.losers} | {result.flats} | {result.return_sum_pct} |"
        )
    lines.extend(
        [
            "",
            "As traded: "
            f"{report.as_traded['trades']} broker trades, {report.as_traded['winners']} winners, "
            f"{report.as_traded['losers']} losers, sum {report.as_traded['return_sum_pct']}%.",
            "",
            "Comparison: max_retries=1 minus as-traded = "
            f"{report.comparisons['max_one_minus_as_traded_return_pct']} points; "
            "max_retries=1 minus max_retries=0 = "
            f"{report.comparisons['max_one_minus_max_zero_return_pct']} points.",
            "",
            "## Broker split",
            "",
        ]
    )
    for result in report.variants:
        for account in ACCOUNTS:
            row = result.by_account[account]
            lines.append(
                f"- max={result.max_retries} {account}: {row['trades']} trades, "
                f"{row['winners']}W/{row['losers']}L/{row['flats']}F, sum {row['return_sum_pct']}%."
            )
    lines.extend(["", "## Forgone winners at one retry", ""])
    lines.extend(
        [f"- {value}" for value in report.forgone_winners_at_one_retry]
        or ["- None in the measured population."]
    )
    lines.extend(
        [
            "",
            "## Named replays",
            "",
            f"- VSA 2026-09-23: {json.dumps(report.vsa_replay, default=_json_default, sort_keys=True)}",
            f"- DCOY 2026-09-22: {json.dumps(report.dcoy_replay, default=_json_default, sort_keys=True)}",
            "",
            "## Known limits",
            "",
        ]
    )
    lines.extend(f"- {value}" for value in report.known_limits)
    return "\n".join(lines) + "\n"


def _session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args = {"options": "-c default_transaction_read_only=on"}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    return sessionmaker(bind=engine, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 24))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 23))
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get("MAI_TAI_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("MAI_TAI_DATABASE_URL is required")
    data = DbRetryOneDataSource(_session_factory(database_url)).load(args.start, args.end)
    report = evaluate(data, args.start, args.end, source_commit=args.source_commit)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(asdict(report), default=_json_default, indent=2, sort_keys=True) + "\n"
    )
    args.markdown.write_text(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
