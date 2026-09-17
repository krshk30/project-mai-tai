"""Capture and replay the frozen Momentum 20% live rule over closed sessions.

Capture is deliberately separate from replay. The trading box uses one-second
aggregates only to find a safe superset of candidate symbols, then downloads raw
trades for those symbols. Replay is offline and routes every captured row through
the production normalizer and :class:`MomentumPaperEngine`.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_paper.conditions import (
    build_condition_snapshot,
    condition_snapshot_from_payload,
)
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine, symbol_is_eligible
from project_mai_tai.momentum_paper.models import (
    MOMENTUM_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)
from project_mai_tai.services.momentum_paper_app import normalize_raw_trade
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS


_ET = ZoneInfo("America/New_York")
_CAPTURE_START = time(4, 0)
_DETECTION_START = time(4, 11)
_DETECTION_END = time(9, 30)
_CAPTURE_END = time(9, 40, 1)
_TRIGGER_MULTIPLIER = Decimal("1.20")
DETECTOR_SUSPECT_DETECTIONS = 10


@dataclass(frozen=True)
class AggregateBar:
    timestamp_ms: int
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class StrategyResult:
    strategy_code: str
    detections: int
    fills: int
    targets: int
    stops: int
    time_exits: int
    no_fill: int
    unanswerable: int
    path_rows: int
    union_prints: int
    tape_key_collisions: int
    suspect: bool


@dataclass(frozen=True)
class SessionResult:
    session_date: str
    candidate_symbols: int
    captured_prints: int
    normalized_prints: int
    eligible_prints: int
    excluded_prints: int
    malformed_prints: int
    strategy_results: tuple[StrategyResult, ...]
    symbol_detections: dict[str, int]


def detector_is_suspect(detections: int) -> bool:
    return detections > DETECTOR_SUSPECT_DETECTIONS


def _value(source: object, *names: str) -> object:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _epoch_ms(value: object) -> int | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp >= 1_000_000_000_000_000_000:
        return timestamp // 1_000_000
    if timestamp >= 1_000_000_000_000_000:
        return timestamp // 1_000
    if timestamp >= 1_000_000_000_000:
        return timestamp
    return None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounds(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, start, tzinfo=_ET).astimezone(UTC),
        datetime.combine(day, end, tzinfo=_ET).astimezone(UTC),
    )


def _previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in US_MARKET_HOLIDAYS:
        candidate -= timedelta(days=1)
    return candidate


def trading_sessions(end: date, count: int) -> tuple[date, ...]:
    if count <= 0:
        raise ValueError("session count must be positive")
    sessions: list[date] = []
    cursor = end
    while len(sessions) < count:
        if cursor.weekday() < 5 and cursor not in US_MARKET_HOLIDAYS:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(sessions))


def require_capture_window(now: datetime) -> None:
    local = now.astimezone(_ET)
    if local.weekday() < 5 and local.date() not in US_MARKET_HOLIDAYS and local.time() < time(16):
        raise RuntimeError("Momentum baseline capture is forbidden before 16:00 ET")


def aggregate_has_candidate(bars: Iterable[AggregateBar]) -> bool:
    """Return whether one-second bars contain a possible production detection.

    Aggregate highs can over-select a same-second move, but cannot remove a true
    raw-print candidate. Exact eligibility and ordering are decided only in replay.
    """

    ordered = sorted(bars, key=lambda row: row.timestamp_ms)
    minima = {window: deque() for window in MOMENTUM_STRATEGIES.values()}
    for bar in ordered:
        observed = datetime.fromtimestamp(bar.timestamp_ms / 1000, tz=UTC).astimezone(_ET)
        clock = observed.time().replace(tzinfo=None)
        for window, queue in minima.items():
            cutoff = bar.timestamp_ms - window * 1000
            while queue and queue[0].timestamp_ms < cutoff:
                queue.popleft()
            if _DETECTION_START <= clock < _DETECTION_END:
                # A low and later high can occur in the same aggregate second.
                # Including the current low may over-select if their order is reversed,
                # but it cannot hide a valid raw-print candidate.
                reference = min(bar.low, queue[0].low) if queue else bar.low
                if bar.high >= reference * _TRIGGER_MULTIPLIER:
                    return True
            while queue and queue[-1].low >= bar.low:
                queue.pop()
            queue.append(bar)
    return False


def _grouped_prices(rows: Iterable[object]) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    result: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for row in rows:
        symbol = str(_value(row, "ticker", "T") or "").upper()
        close = _decimal(_value(row, "close", "c"))
        high = _decimal(_value(row, "high", "h"))
        low = _decimal(_value(row, "low", "l"))
        if symbol and close is not None and high is not None and low is not None:
            result[symbol] = (close, high, low)
    return result


def broad_candidates(
    prior_rows: Iterable[object], session_rows: Iterable[object]
) -> dict[str, Decimal]:
    prior = _grouped_prices(prior_rows)
    current = _grouped_prices(session_rows)
    candidates: dict[str, Decimal] = {}
    for symbol, (prior_close, _, _) in prior.items():
        session = current.get(symbol)
        if (
            session is None
            or prior_close < Decimal("1")
            or not symbol_is_eligible(symbol)
            or session[1] < session[2] * _TRIGGER_MULTIPLIER
        ):
            continue
        candidates[symbol] = prior_close
    return candidates


def _aggregate_rows(client: object, symbol: str, day: date) -> list[AggregateBar]:
    start, end = _bounds(day, _CAPTURE_START, _DETECTION_END)
    rows: list[AggregateBar] = []
    for raw in client.list_aggs(
        symbol,
        1,
        "second",
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000) - 1,
        adjusted=True,
        sort="asc",
        limit=50_000,
    ):
        timestamp = _epoch_ms(_value(raw, "timestamp", "t"))
        high = _decimal(_value(raw, "high", "h"))
        low = _decimal(_value(raw, "low", "l"))
        if timestamp is not None and high is not None and low is not None:
            rows.append(AggregateBar(timestamp, high, low))
    return rows


def raw_trade_row(raw: object, symbol: str) -> dict[str, object] | None:
    sip_ms = _epoch_ms(_value(raw, "sip_timestamp", "t"))
    price = _decimal(_value(raw, "price", "p"))
    if sip_ms is None or price is None:
        return None
    participant_ms = _epoch_ms(_value(raw, "participant_timestamp", "y", "pt"))
    conditions = _value(raw, "conditions", "c")
    if conditions is None:
        codes: list[int] = []
    elif isinstance(conditions, (list, tuple)):
        codes = [int(code) for code in conditions]
    else:
        codes = [int(conditions)]
    exchange = _value(raw, "exchange", "x")
    trf_id = _value(raw, "trf_id", "trfi")
    return {
        "ev": "T",
        "sym": symbol.upper(),
        "t": sip_ms,
        "p": str(price),
        "s": int(_value(raw, "size", "s") or 0),
        "c": codes,
        "i": str(_value(raw, "id", "trade_id", "i") or ""),
        "x": int(exchange) if exchange is not None else None,
        "trfi": int(trf_id) if trf_id is not None else None,
        "y": participant_ms,
    }


def _trade_rows(client: object, symbol: str, day: date) -> list[dict[str, object]]:
    start, end = _bounds(day, _CAPTURE_START, _CAPTURE_END)
    rows: list[dict[str, object]] = []
    for raw in client.list_trades(
        symbol,
        timestamp_gte=int(start.timestamp() * 1_000_000_000),
        timestamp_lt=int(end.timestamp() * 1_000_000_000),
        order="asc",
        sort="timestamp",
        limit=50_000,
    ):
        row = raw_trade_row(raw, symbol)
        if row is not None:
            rows.append(row)
    return rows


def capture_session(
    client: object,
    day: date,
    *,
    condition_payload: Mapping[str, object],
    captured_at: datetime,
) -> dict[str, object]:
    prior_day = _previous_trading_day(day)
    prior_rows = list(client.get_grouped_daily_aggs(prior_day, adjusted=True))
    session_rows = list(client.get_grouped_daily_aggs(day, adjusted=True))
    broad = broad_candidates(prior_rows, session_rows)
    exact: dict[str, Decimal] = {}
    aggregate_counts: dict[str, int] = {}
    for symbol, prior_close in sorted(broad.items()):
        bars = _aggregate_rows(client, symbol, day)
        if aggregate_has_candidate(bars):
            exact[symbol] = prior_close
            aggregate_counts[symbol] = len(bars)

    trades = {symbol: _trade_rows(client, symbol, day) for symbol in exact}
    return {
        "schema_version": 1,
        "session_date": day.isoformat(),
        "prior_close_date": prior_day.isoformat(),
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "candidate_rule": "one_second_aggregate_superset_at_20pct",
        "broad_candidate_count": len(broad),
        "candidate_symbols": sorted(exact),
        "aggregate_counts": aggregate_counts,
        "prior_closes": {symbol: str(price) for symbol, price in exact.items()},
        "condition_snapshot": dict(condition_payload),
        "trades": trades,
    }


def write_capture(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    temporary.replace(path)


def read_capture(path: Path) -> dict[str, object]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError(f"unsupported Momentum capture: {path}")
    return payload


def capture_sessions(
    client: object,
    sessions: Sequence[date],
    output_dir: Path,
    *,
    now: datetime,
    force: bool = False,
) -> tuple[Path, ...]:
    require_capture_window(now)
    condition_rows = list(
        client.list_conditions(
            asset_class="stocks", data_type="trade", limit=1000, sort="id", order="asc"
        )
    )
    snapshot = build_condition_snapshot(condition_rows, retrieved_at=now.astimezone(UTC))
    written: list[Path] = []
    for day in sessions:
        path = output_dir / f"momentum-live-rule-{day.isoformat()}.json.gz"
        if path.exists() and not force:
            read_capture(path)
            written.append(path)
            print(f"capture exists session={day} path={path}; skipping")
            continue
        payload = capture_session(
            client,
            day,
            condition_payload=snapshot.payload(),
            captured_at=now,
        )
        write_capture(path, payload)
        written.append(path)
        print(
            f"captured session={day} candidates={len(payload['candidate_symbols'])} "
            f"raw_prints={sum(len(rows) for rows in payload['trades'].values())} path={path}"
        )
    return tuple(written)


def _raw_identity(trade: TradePrint) -> tuple[object, ...]:
    if trade.trade_id.strip():
        return (trade.trade_id.strip(), trade.exchange, trade.trf_id, trade.sip_ts_ms)
    return (
        "anonymous",
        trade.sip_ts_ms,
        trade.participant_ts_ms,
        trade.exchange,
        trade.trf_id,
        str(trade.price),
        trade.size,
        trade.conditions,
    )


def _union_print_count(
    trades: Sequence[TradePrint], terminal: Sequence[MomentumTapeRecord], strategy_code: str
) -> int:
    intervals: list[tuple[int, int]] = []
    for row in terminal:
        if row.strategy_code != strategy_code:
            continue
        path_range = row.payload.get("path_range")
        if isinstance(path_range, Mapping):
            intervals.append((int(path_range["start_sip_ts_ms"]), int(path_range["end_sip_ts_ms"])))
    identities: set[tuple[object, ...]] = set()
    for trade in trades:
        if any(start <= trade.sip_ts_ms <= end for start, end in intervals):
            payload = json.dumps(trade.payload(), sort_keys=True, separators=(",", ":"))
            identities.add((*_raw_identity(trade), payload))
    return len(identities)


def _replay_symbol(
    symbol: str,
    prior_close: Decimal,
    raw_rows: Sequence[Mapping[str, object]],
    condition_payload: Mapping[str, object],
    day: date,
) -> tuple[list[MomentumTapeRecord], list[TradePrint], int]:
    snapshot = condition_snapshot_from_payload(condition_payload)
    coverage_start, session_close = _bounds(day, _CAPTURE_START, _CAPTURE_END)
    engine = MomentumPaperEngine(
        prior_closes={symbol: prior_close},
        condition_version=snapshot.version,
        coverage_started_ms=int(coverage_start.timestamp() * 1000),
    )
    records: list[MomentumTapeRecord] = []
    trades: list[TradePrint] = []
    malformed = 0
    for raw in raw_rows:
        trade = normalize_raw_trade(raw, conditions=snapshot)
        if trade is None:
            malformed += 1
            continue
        records.extend(engine.advance_clock(trade.sip_ts_ms))
        records.extend(engine.ingest(trade))
        trades.append(trade)
    records.extend(engine.close_session(int(session_close.timestamp() * 1000)))
    return records, trades, malformed


def replay_capture(payload: Mapping[str, object]) -> SessionResult:
    day = date.fromisoformat(str(payload["session_date"]))
    prior_closes = payload.get("prior_closes")
    raw_trades = payload.get("trades")
    condition_payload = payload.get("condition_snapshot")
    if not isinstance(prior_closes, Mapping) or not isinstance(raw_trades, Mapping):
        raise RuntimeError(f"capture for {day} is missing prices or trades")
    if not isinstance(condition_payload, Mapping):
        raise RuntimeError(f"capture for {day} is missing condition metadata")

    all_records: list[MomentumTapeRecord] = []
    all_trades: dict[str, list[TradePrint]] = {}
    malformed = 0
    captured_prints = 0
    for symbol in sorted(prior_closes):
        rows = raw_trades.get(symbol, [])
        if not isinstance(rows, list):
            raise RuntimeError(f"capture for {day} {symbol} has malformed raw rows")
        captured_prints += len(rows)
        records, trades, bad = _replay_symbol(
            str(symbol),
            Decimal(str(prior_closes[symbol])),
            rows,
            condition_payload,
            day,
        )
        all_records.extend(records)
        all_trades[str(symbol)] = trades
        malformed += bad

    if malformed:
        raise RuntimeError(f"capture for {day} contains {malformed} malformed raw trade rows")
    terminals = [
        row for row in all_records if row.event_type in {"FINAL", "NO_FILL", "UNANSWERABLE"}
    ]
    symbol_detections = Counter(row.symbol for row in all_records if row.event_type == "DETECTED")
    strategy_results: list[StrategyResult] = []
    for strategy in MOMENTUM_STRATEGIES:
        selected = [row for row in all_records if row.strategy_code == strategy]
        final = [row for row in selected if row.event_type == "FINAL"]
        path_rows = sum(row.event_type == "PATH_PRINT" for row in selected)
        union_prints = sum(
            _union_print_count(
                trades,
                [row for row in terminals if row.symbol == symbol],
                strategy,
            )
            for symbol, trades in all_trades.items()
        )
        strategy_results.append(
            StrategyResult(
                strategy_code=strategy,
                detections=sum(row.event_type == "DETECTED" for row in selected),
                fills=sum(row.event_type == "FILLED" for row in selected),
                targets=sum(row.payload.get("exit_reason") == "TARGET" for row in final),
                stops=sum(row.payload.get("exit_reason") == "STOP" for row in final),
                time_exits=sum(row.payload.get("exit_reason") == "TIME" for row in final),
                no_fill=sum(row.event_type == "NO_FILL" for row in selected),
                unanswerable=sum(row.event_type == "UNANSWERABLE" for row in selected),
                path_rows=path_rows,
                union_prints=union_prints,
                tape_key_collisions=sum(
                    row.event_type == "PATH_PRINT" and ":COLLISION:" in row.event_key
                    for row in selected
                ),
                suspect=detector_is_suspect(sum(row.event_type == "DETECTED" for row in selected)),
            )
        )
    normalized = [trade for rows in all_trades.values() for trade in rows]
    return SessionResult(
        session_date=day.isoformat(),
        candidate_symbols=len(prior_closes),
        captured_prints=captured_prints,
        normalized_prints=len(normalized),
        eligible_prints=sum(trade.eligible for trade in normalized),
        excluded_prints=sum(not trade.eligible for trade in normalized),
        malformed_prints=malformed,
        strategy_results=tuple(strategy_results),
        symbol_detections=dict(sorted(symbol_detections.items())),
    )


def replay_directory(
    input_dir: Path, *, expected_sessions: int | None = None
) -> tuple[SessionResult, ...]:
    paths = sorted(input_dir.glob("momentum-live-rule-*.json.gz"))
    if not paths:
        raise RuntimeError(f"no Momentum capture files found in {input_dir}")
    if expected_sessions is not None and len(paths) != expected_sessions:
        raise RuntimeError(
            f"expected {expected_sessions} Momentum captures in {input_dir}, found {len(paths)}"
        )
    results = tuple(replay_capture(read_capture(path)) for path in paths)
    observed_days = tuple(date.fromisoformat(result.session_date) for result in results)
    if len(set(observed_days)) != len(observed_days):
        raise RuntimeError("Momentum capture directory contains duplicate session dates")
    if expected_sessions is not None:
        expected_days = trading_sessions(max(observed_days), expected_sessions)
        if observed_days != expected_days:
            raise RuntimeError(
                "Momentum captures are not the complete contiguous trading-session population: "
                f"expected {expected_days[0]}..{expected_days[-1]}"
            )
    for result in results:
        for row in result.strategy_results:
            if row.path_rows != row.union_prints:
                raise RuntimeError(
                    f"{result.session_date} {row.strategy_code}: PATH rows {row.path_rows} "
                    f"do not reconcile to union prints {row.union_prints}"
                )
    return results


def report_payload(results: Sequence[SessionResult]) -> dict[str, object]:
    strategy_totals: dict[str, Counter[str]] = {
        strategy: Counter() for strategy in MOMENTUM_STRATEGIES
    }
    suspect_sessions: dict[str, list[str]] = {strategy: [] for strategy in MOMENTUM_STRATEGIES}
    symbols = Counter()
    for session in results:
        symbols.update(session.symbol_detections)
        for row in session.strategy_results:
            totals = strategy_totals[row.strategy_code]
            for field in (
                "detections",
                "fills",
                "targets",
                "stops",
                "time_exits",
                "no_fill",
                "unanswerable",
                "path_rows",
                "union_prints",
                "tape_key_collisions",
            ):
                totals[field] += int(getattr(row, field))
            if row.suspect:
                suspect_sessions[row.strategy_code].append(session.session_date)
    return {
        "criterion": {
            "detector_suspect_when": "detections > 10 for either bot in one session",
            "threshold": DETECTOR_SUSPECT_DETECTIONS,
            "frozen_before_capture": True,
        },
        "sessions": len(results),
        "session_results": [asdict(result) for result in results],
        "strategy_totals": {key: dict(value) for key, value in strategy_totals.items()},
        "suspect_sessions": suspect_sessions,
        "symbol_concentration": dict(symbols.most_common()),
    }


def render_markdown(report: Mapping[str, object]) -> str:
    rows = [
        "# Momentum live-rule 30-session baseline",
        "",
        "Pre-registered criterion: `DETECTOR_SUSPECT` when either bot records more than "
        f"{DETECTOR_SUSPECT_DETECTIONS} detections in one session.",
        "",
        "| Session | Candidates | Normalized/captured | Eligible/normalized | Excluded/normalized | Malformed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for session in report["session_results"]:
        rows.append(
            f"| {session['session_date']} | {session['candidate_symbols']} | "
            f"{session['normalized_prints']}/{session['captured_prints']} | "
            f"{session['eligible_prints']}/{session['normalized_prints']} | "
            f"{session['excluded_prints']}/{session['normalized_prints']} | "
            f"{session['malformed_prints']} |"
        )
    rows.extend(
        [
            "",
            "| Session | Bot | Events | Fills | Target | Stop | Time | No fill | Unanswerable | PATH/union | Status |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for session in report["session_results"]:
        for result in session["strategy_results"]:
            status = "DETECTOR_SUSPECT" if result["suspect"] else "WITHIN_RATE"
            rows.append(
                f"| {session['session_date']} | {result['strategy_code']} | "
                f"{result['detections']} | {result['fills']} | {result['targets']} | "
                f"{result['stops']} | {result['time_exits']} | {result['no_fill']} | "
                f"{result['unanswerable']} | {result['path_rows']}/{result['union_prints']} | "
                f"{status} |"
            )
    rows.extend(["", "## Totals", ""])
    for strategy, totals in report["strategy_totals"].items():
        suspect = len(report["suspect_sessions"][strategy])
        rows.append(
            f"- `{strategy}`: detections {totals.get('detections', 0)}, fills "
            f"{totals.get('fills', 0)}, targets {totals.get('targets', 0)}, stops "
            f"{totals.get('stops', 0)}, no-fill {totals.get('no_fill', 0)}, "
            f"unanswerable {totals.get('unanswerable', 0)}, suspect sessions "
            f"{suspect}/{report['sessions']}."
        )
    rows.extend(["", "## Symbol concentration", ""])
    total = sum(int(value) for value in report["symbol_concentration"].values())
    for symbol, count in list(report["symbol_concentration"].items())[:20]:
        rows.append(f"- `{symbol}`: {count}/{total} detections.")
    return "\n".join(rows) + "\n"


def _capture_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.massive_api_key:
        raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required")
    from massive import RESTClient

    client = RESTClient(api_key=settings.massive_api_key)
    sessions = trading_sessions(args.end_date, args.sessions)
    capture_sessions(
        client, sessions, args.output_dir, now=datetime.now(UTC), force=bool(args.force)
    )
    return 0


def _replay_command(args: argparse.Namespace) -> int:
    results = replay_directory(args.input_dir, expected_sessions=args.expected_sessions)
    report = report_payload(results)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(f"sessions={len(results)} json={args.json} markdown={args.markdown}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture", help="capture candidate raw tapes after close")
    capture.add_argument("--end-date", type=date.fromisoformat, required=True)
    capture.add_argument("--sessions", type=int, default=30)
    capture.add_argument("--output-dir", type=Path, required=True)
    capture.add_argument("--force", action="store_true")
    capture.set_defaults(run=_capture_command)

    replay = subparsers.add_parser("replay", help="replay captured tapes offline")
    replay.add_argument("--input-dir", type=Path, required=True)
    replay.add_argument("--expected-sessions", type=int, default=30)
    replay.add_argument("--json", type=Path, required=True)
    replay.add_argument("--markdown", type=Path, required=True)
    replay.set_defaults(run=_replay_command)
    args = parser.parse_args()
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
