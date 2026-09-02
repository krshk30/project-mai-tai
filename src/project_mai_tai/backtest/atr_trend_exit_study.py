"""Measure TOS dot and MACD-histogram exits on the fixed 97-entry ATR population.

This is research-only. It ports the operator's exact one-minute thinkScript study and uses
captured executable quotes; it does not alter any live strategy or order path.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from project_mai_tai.backtest.atr_flip_hold_study import (
    EASTERN,
    FlipCandidate,
    NaturalPath,
    PolicyOutcome,
    _json_default,
    _write_csv,
)
from project_mai_tai.backtest.atr_flip_hold_week_study import run_week
from project_mai_tai.backtest.data import Quote, SchwabBar
from project_mai_tai.backtest.dot_entry import build_rows, macd_histogram
from project_mai_tai.backtest.replay import BAR_CLOSE_OFFSET_MS

FIXED_POLICY = "full_target+5_stop-10"
RULES = ("fixed_5", "atr_sell", "dot_bearish", "macd_hist_below_zero", "first", "second")


@dataclass(frozen=True)
class ExitOutcome:
    rule: str
    signal_ts: datetime | None
    trigger: str
    exit_ts: datetime
    exit_px: float
    exit_reason: str
    return_pct: float
    duration_minutes: float


def indicator_exit_signals(
    bars: list[SchwabBar],
) -> tuple[list[datetime], list[datetime]]:
    """Return bearish-dot transitions and MACD-histogram downcrosses.

    The dot has three votes because the MACDHistogram vote in the supplied source is commented
    out. A bearish dot is ``consensus <= 1``. ``turns bearish`` means the edge from >1 to <=1.
    The separate histogram exit is the TOS DownSignal edge from >=0 to <0.
    """
    ordered = sorted(bars, key=lambda row: row.ts)
    rows = build_rows(
        [float(row.high) for row in ordered],
        [float(row.low) for row in ordered],
        [float(row.close) for row in ordered],
    )
    histogram = macd_histogram([float(row.close) for row in ordered])
    dots: list[datetime] = []
    hist: list[datetime] = []
    for index in range(1, len(ordered)):
        decision_ts = datetime.fromtimestamp(
            (ordered[index].ts + BAR_CLOSE_OFFSET_MS) / 1000.0, UTC
        )
        if rows.all_red(index) and not rows.all_red(index - 1):
            dots.append(decision_ts)
        if histogram[index] < 0.0 <= histogram[index - 1]:
            hist.append(decision_ts)
    return dots, hist


def _first_after(signals: list[datetime], entry_signal_ts: datetime) -> datetime | None:
    index = bisect.bisect_right(signals, entry_signal_ts)
    return signals[index] if index < len(signals) else None


def _trigger_for(
    signal_ts: datetime | None,
    dot_ts: datetime | None,
    hist_ts: datetime | None,
) -> str:
    if signal_ts is None:
        return "none"
    if signal_ts == dot_ts == hist_ts:
        return "dot+hist"
    if signal_ts == dot_ts:
        return "dot"
    return "hist"


def _trend_outcome(
    rule: str,
    signal_ts: datetime | None,
    trigger: str,
    candidate: FlipCandidate,
    quotes: list[Quote],
    session_end: datetime,
) -> ExitOutcome:
    available = quotes[candidate.entry_quote_index :]
    before_close = [quote for quote in available if quote.ts < session_end]
    if not before_close:
        raise ValueError(f"{candidate.symbol} {candidate.buy_signal_ts}: no exit quote")

    reason = "session_close"
    quote = before_close[-1]
    if signal_ts is not None:
        quote_times = [item.ts for item in quotes]
        index = max(candidate.entry_quote_index, bisect.bisect_left(quote_times, signal_ts))
        if index < len(quotes) and quotes[index].ts < session_end:
            quote = quotes[index]
            reason = "signal"

    return ExitOutcome(
        rule=rule,
        signal_ts=signal_ts,
        trigger=trigger,
        exit_ts=quote.ts,
        exit_px=float(quote.bid),
        exit_reason=reason,
        return_pct=(float(quote.bid) / candidate.entry_px - 1.0) * 100.0,
        duration_minutes=(quote.ts - candidate.entry_ts).total_seconds() / 60.0,
    )


def trend_outcomes(
    candidate: FlipCandidate,
    quotes: list[Quote],
    session_end: datetime,
    dot_signals: list[datetime],
    hist_signals: list[datetime],
) -> dict[str, ExitOutcome]:
    dot_ts = _first_after(dot_signals, candidate.buy_signal_ts)
    hist_ts = _first_after(hist_signals, candidate.buy_signal_ts)
    present = [value for value in (dot_ts, hist_ts) if value is not None]
    first_ts = min(present) if present else None
    second_ts = max(dot_ts, hist_ts) if dot_ts is not None and hist_ts is not None else None
    specs = {
        "dot_bearish": (dot_ts, "dot" if dot_ts else "none"),
        "macd_hist_below_zero": (hist_ts, "hist" if hist_ts else "none"),
        "first": (first_ts, _trigger_for(first_ts, dot_ts, hist_ts)),
        "second": (second_ts, _trigger_for(second_ts, dot_ts, hist_ts)),
    }
    return {
        rule: _trend_outcome(rule, signal_ts, trigger, candidate, quotes, session_end)
        for rule, (signal_ts, trigger) in specs.items()
    }


def _capture_pct(return_pct: float, max_up_pct: float) -> float | None:
    if max_up_pct <= 0.0:
        return None
    return return_pct / max_up_pct * 100.0


def _session_max_up(candidate: FlipCandidate, quotes: list[Quote], end: datetime) -> float:
    bids = [
        float(quote.bid)
        for quote in quotes[candidate.entry_quote_index :]
        if quote.ts < end
    ]
    if not bids:
        raise ValueError(f"{candidate.symbol} {candidate.buy_signal_ts}: no MFE quotes")
    return (max(bids) / candidate.entry_px - 1.0) * 100.0


def _population_keys(path: Path) -> set[tuple[str, datetime]]:
    with path.open(newline="") as handle:
        return {
            (row["symbol"], datetime.fromisoformat(row["buy_signal_ts"]))
            for row in csv.DictReader(handle)
        }


def _fmt_ts(value: datetime | None) -> str:
    return value.astimezone(EASTERN).strftime("%H:%M") if value else "-"


def _rule_return(row: dict[str, object], rule: str) -> float:
    return float(row[f"{rule}_return_pct"])


def _rule_duration(row: dict[str, object], rule: str) -> float:
    return float(row[f"{rule}_duration_minutes"])


def _summary_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for population, reached in (("Reached +5", True), ("Did not reach +5", False)):
        group = [row for row in rows if bool(row["reached_5"]) is reached]
        for rule in RULES:
            returns = [_rule_return(row, rule) for row in group]
            durations = [_rule_duration(row, rule) for row in group]
            captures = [
                float(row[f"{rule}_capture_pct"])
                for row in group
                if row[f"{rule}_capture_pct"] is not None
            ]
            summaries.append(
                {
                    "population": population,
                    "rule": rule,
                    "trades": len(group),
                    "before_close_exits": sum(
                        row[f"{rule}_exit_reason"] != "session_close" for row in group
                    ),
                    "session_close_exits": sum(
                        row[f"{rule}_exit_reason"] == "session_close" for row in group
                    ),
                    "total_return_pct": round(sum(returns), 4),
                    "mean_return_pct": round(statistics.fmean(returns), 4),
                    "median_return_pct": round(statistics.median(returns), 4),
                    "mean_duration_minutes": round(statistics.fmean(durations), 2),
                    "median_duration_minutes": round(statistics.median(durations), 2),
                    "mean_capture_pct": round(statistics.fmean(captures), 2) if captures else None,
                    "median_capture_pct": round(statistics.median(captures), 2) if captures else None,
                    "capture_denominator_trades": len(captures),
                }
            )
    return summaries


def _report(
    path: Path,
    rows: list[dict[str, object]],
    summaries: list[dict[str, object]],
) -> None:
    labels = {
        "fixed_5": "Fixed +5 / -10 stop",
        "atr_sell": "ATR SELL",
        "dot_bearish": "Dot bearish",
        "macd_hist_below_zero": "MACD hist < 0",
        "first": "First of dot/hist",
        "second": "Second of dot/hist",
    }
    lines = [
        "# ATR Trend-Exit Measurement: 2026-08-24 to 2026-09-01",
        "",
        "Population: the exact 97 scanner-confirmed ATR BUY entries from the prior study; the "
        "runner refuses output if any `(symbol, BUY time)` differs.",
        "",
        "The supplied source is thinkScript, despite the Pine label. The operator's local TOS "
        "workspace records this exact study on a one-minute chart. The dot has exactly three "
        "votes (MACD Value 6/13, StochasticFast 10/3 Simple FastK, RSI 14 Wilder); the commented "
        "MACDHistogram line is not a vote. Dot exit is the first transition from consensus >1 "
        "to <=1 after entry. The separate histogram exit is the default 12/26/9 exponential "
        "Diff crossing from >=0 to <0.",
        "",
        "Every trend signal is observed at the one-minute bar close and filled at the first "
        "captured executable bid at or after that close. Missing signals fall back to the last "
        "bid before 16:00 ET. `First` waits for either signal; `Second` requires both, so if one "
        "never occurs it falls back to 16:00.",
        "",
        "`ATR-segment max-up` is the previously reported entry-to-ATR-SELL value. Capture "
        "fractions use a common entry-to-16:00 max-up because the replacement trend exits may "
        "occur after ATR SELL. Capture is undefined where that common max-up is <=0.",
        "",
        "## Population Split",
        "",
        "| Population | Rule | N | Before-close exits | 16:00 exits | Total | Mean | Median | "
        "Mean hold min | Median hold min | Mean capture | Median capture | Capture N |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        mean_capture = summary["mean_capture_pct"]
        median_capture = summary["median_capture_pct"]
        line = (
            f"| {summary['population']} | {labels[str(summary['rule'])]} | "
            f"{summary['trades']} | {summary['before_close_exits']} | "
            f"{summary['session_close_exits']} | {float(summary['total_return_pct']):+.2f}% | "
            f"{float(summary['mean_return_pct']):+.2f}% | "
            f"{float(summary['median_return_pct']):+.2f}% | "
            f"{float(summary['mean_duration_minutes']):.1f} | "
            f"{float(summary['median_duration_minutes']):.1f} | "
        )
        if mean_capture is None:
            line += "- | - | 0 |"
        else:
            line += (
                f"{float(mean_capture):+.1f}% | "
                f"{float(median_capture):+.1f}% | {summary['capture_denominator_trades']} |"
            )
        lines.append(line)

    both = sum(
        row["dot_bearish_signal_ts"] is not None
        and row["macd_hist_below_zero_signal_ts"] is not None
        for row in rows
    )
    dot_only = sum(
        row["dot_bearish_signal_ts"] is not None
        and row["macd_hist_below_zero_signal_ts"] is None
        for row in rows
    )
    hist_only = sum(
        row["dot_bearish_signal_ts"] is None
        and row["macd_hist_below_zero_signal_ts"] is not None
        for row in rows
    )
    neither = len(rows) - both - dot_only - hist_only
    first_triggers = {
        trigger: sum(row["first_trigger"] == trigger for row in rows)
        for trigger in ("dot", "hist", "dot+hist", "none")
    }
    lines.extend(
        [
            "",
            "### Signal Availability and Order",
            "",
            "| Both signals | Dot only | Histogram only | Neither | First dot | First histogram | "
            "Same bar | No first signal |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {both} | {dot_only} | {hist_only} | {neither} | "
            f"{first_triggers['dot']} | {first_triggers['hist']} | "
            f"{first_triggers['dot+hist']} | {first_triggers['none']} |",
        ]
    )

    lines.extend(
        [
            "",
            "## By Session",
            "",
            "| Session | Entries | Reached +5 | Fixed +5 total | ATR SELL total | Dot total | "
            "Histogram total | First total | Second total |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for session in sorted({str(row["session_day_et"]) for row in rows}):
        group = [row for row in rows if row["session_day_et"] == session]
        lines.append(
            f"| {session} | {len(group)} | {sum(bool(row['reached_5']) for row in group)} | "
            + " | ".join(
                f"{sum(_rule_return(row, rule) for row in group):+.2f}%"
                for rule in RULES
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## All 97 Entries",
            "",
            "Times are ET. Fixed and ATR cells are `return / capture of common max-up`. Each "
            "trend cell is `exit time / return / capture`; `SC` means the signal never produced "
            "an executable exit before session close.",
            "",
            "| # | Date | Symbol | BUY | Pop | ATR-segment max-up | To-16 max-up | Fixed +5 | "
            "ATR SELL | Dot | MACD hist | First | Second |",
            "|---:|---|---|---:|---|---:|---:|---:|---:|---|---|---|---|",
        ]
    )
    for index, row in enumerate(sorted(rows, key=lambda item: item["buy_signal_ts"]), 1):
        def baseline_cell(rule: str) -> str:
            capture = row[f"{rule}_capture_pct"]
            capture_text = f"{float(capture):+.1f}%" if capture is not None else "-"
            return f"{_rule_return(row, rule):+.2f}% / {capture_text}"

        def cell(rule: str) -> str:
            exit_time = _fmt_ts(row[f"{rule}_exit_ts"])
            if row[f"{rule}_exit_reason"] == "session_close":
                exit_time += " SC"
            capture = row[f"{rule}_capture_pct"]
            capture_text = f"{float(capture):+.1f}%" if capture is not None else "-"
            return f"{exit_time} / {_rule_return(row, rule):+.2f}% / {capture_text}"

        lines.append(
            f"| {index} | {row['session_day_et']} | {row['symbol']} | "
            f"{_fmt_ts(row['buy_signal_ts'])} | {'+5' if row['reached_5'] else '<+5'} | "
            f"{float(row['atr_segment_max_up_pct']):+.2f}% | "
            f"{float(row['session_max_up_pct']):+.2f}% | "
            f"{baseline_cell('fixed_5')} | {baseline_cell('atr_sell')} | "
            f"{cell('dot_bearish')} | "
            f"{cell('macd_hist_below_zero')} | {cell('first')} | {cell('second')} |"
        )
    path.write_text("\n".join(lines) + "\n")


def run_measurement(source, settings, start: date, end: date, population_csv: Path):
    _, _, candidates, paths, outcomes = run_week(source, settings, start, end)
    expected = _population_keys(population_csv)
    actual = {(row.symbol, row.buy_signal_ts) for row in candidates}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"97-entry population drift: expected={len(expected)} actual={len(actual)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )

    path_by_key = {(row.symbol, row.buy_signal_ts): row for row in paths}
    fixed_by_key = {
        (row.symbol, row.buy_signal_ts): row
        for row in outcomes
        if row.policy == FIXED_POLICY
    }
    grouped: dict[tuple[str, str], list[FlipCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.session_day_et, candidate.symbol)].append(candidate)

    result: list[dict[str, object]] = []
    for (day_text, symbol), group in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        observation_start = datetime.combine(session_day, time(4), EASTERN)
        session_start = datetime.combine(session_day, time(7), EASTERN)
        session_end = datetime.combine(session_day, time(16), EASTERN).astimezone(UTC)
        bars = source.schwab_bars(symbol, observation_start, session_end)
        quotes = source.quotes(symbol, session_start, session_end)
        dot_signals, hist_signals = indicator_exit_signals(bars)
        for candidate in group:
            key = (symbol, candidate.buy_signal_ts)
            path: NaturalPath = path_by_key[key]
            fixed: PolicyOutcome = fixed_by_key[key]
            trends = trend_outcomes(
                candidate, quotes, session_end, dot_signals, hist_signals
            )
            session_mfe = _session_max_up(candidate, quotes, session_end)
            row: dict[str, object] = {
                "session_day_et": day_text,
                "symbol": symbol,
                "buy_signal_ts": candidate.buy_signal_ts,
                "entry_ts": candidate.entry_ts,
                "entry_px": round(candidate.entry_px, 4),
                "reached_5": path.reached_5_ts is not None,
                "atr_segment_max_up_pct": round(path.mfe_pct, 4),
                "session_max_up_pct": round(session_mfe, 4),
            }
            baselines = {
                "fixed_5": (
                    fixed.exit_ts,
                    fixed.exit_px,
                    fixed.exit_reason,
                    fixed.return_pct,
                    fixed.duration_seconds / 60.0,
                ),
                "atr_sell": (
                    path.natural_exit_ts,
                    path.natural_exit_px,
                    path.natural_exit_reason,
                    path.natural_return_pct,
                    (path.natural_exit_ts - path.entry_ts).total_seconds() / 60.0,
                ),
            }
            for rule, (exit_ts, exit_px, reason, return_pct, duration) in baselines.items():
                row.update(
                    {
                        f"{rule}_signal_ts": None,
                        f"{rule}_trigger": reason,
                        f"{rule}_exit_ts": exit_ts,
                        f"{rule}_exit_px": round(float(exit_px), 4),
                        f"{rule}_exit_reason": reason,
                        f"{rule}_return_pct": round(float(return_pct), 4),
                        f"{rule}_duration_minutes": round(float(duration), 2),
                        f"{rule}_capture_pct": (
                            round(value, 2)
                            if (value := _capture_pct(float(return_pct), session_mfe)) is not None
                            else None
                        ),
                    }
                )
            for rule, outcome in trends.items():
                row.update(
                    {
                        f"{rule}_signal_ts": outcome.signal_ts,
                        f"{rule}_trigger": outcome.trigger,
                        f"{rule}_exit_ts": outcome.exit_ts,
                        f"{rule}_exit_px": round(outcome.exit_px, 4),
                        f"{rule}_exit_reason": outcome.exit_reason,
                        f"{rule}_return_pct": round(outcome.return_pct, 4),
                        f"{rule}_duration_minutes": round(outcome.duration_minutes, 2),
                        f"{rule}_capture_pct": (
                            round(value, 2)
                            if (
                                value := _capture_pct(outcome.return_pct, session_mfe)
                            )
                            is not None
                            else None
                        ),
                    }
                )
            result.append(row)
    return sorted(result, key=lambda row: row["buy_signal_ts"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 24))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 1))
    parser.add_argument(
        "--population-csv",
        type=Path,
        default=Path(
            "analysis/reports/atr-flip-hold-week-2026-08-24-to-2026-09-01-trades.csv"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    base = get_settings()
    settings = build_replay_settings(base=base)
    source = DbMarketDataSource(build_session_factory(base))
    rows = run_measurement(
        source, settings, args.start, args.end, args.population_csv
    )
    summaries = _summary_rows(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"atr-trend-exit-{args.start.isoformat()}-to-{args.end.isoformat()}"
    _write_csv(args.output_dir / f"{stem}-trades.csv", rows)
    _write_csv(args.output_dir / f"{stem}-summary.csv", summaries)
    _report(args.output_dir / f"{stem}.md", rows, summaries)
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(
            {"start": args.start, "end": args.end, "trades": rows, "summary": summaries},
            default=_json_default,
            indent=2,
        )
        + "\n"
    )
    print(f"trades={len(rows)} summaries={len(summaries)} output={args.output_dir / stem}*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
