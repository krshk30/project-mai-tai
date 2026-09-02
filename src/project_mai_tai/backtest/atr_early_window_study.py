"""Measure what was visible 3, 5, and 10 minutes after the fixed 97 ATR fills.

This is research-only. It locks the prior study's fills and reached-5 labels, then measures
single features without changing or simulating an entry or exit rule.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from project_mai_tai.backtest.atr_flip_hold_study import EASTERN, _json_default, _write_csv
from project_mai_tai.backtest.data import Quote, SchwabBar
from project_mai_tai.backtest.replay import BAR_CLOSE_OFFSET_MS

CHECKPOINTS = (3, 5, 10)
POPULATIONS = (("Reached +5", True), ("Did not reach +5", False))


@dataclass(frozen=True)
class LockedEntry:
    session_day_et: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    reached_5: bool


@dataclass(frozen=True)
class EarlySnapshot:
    session_day_et: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    reached_5: bool
    checkpoint_minutes: int
    checkpoint_ts: datetime
    quote_ts: datetime
    current_return_pct: float
    max_up_so_far_pct: float
    max_down_so_far_pct: float
    touched_plus_2: bool
    touched_minus_3: bool
    last_bar_ts: datetime
    last_bar_direction: str
    latest_minute_new_low: bool
    every_minute_new_low: bool
    new_low_streak_minutes: int


@dataclass(frozen=True)
class ExcludedEntry:
    session_day_et: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    reached_5: bool
    missing_bar_closes: tuple[datetime, ...]


def load_locked_entries(path: Path) -> list[LockedEntry]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    entries = [
        LockedEntry(
            session_day_et=row["session_day_et"],
            symbol=row["symbol"],
            buy_signal_ts=datetime.fromisoformat(row["buy_signal_ts"]),
            entry_ts=datetime.fromisoformat(row["entry_ts"]),
            entry_px=float(row["entry_px"]),
            reached_5=row["reached_5"].lower() == "true",
        )
        for row in rows
    ]
    if len(entries) != 97 or len({(row.symbol, row.buy_signal_ts) for row in entries}) != 97:
        raise RuntimeError(f"locked population must contain 97 unique entries, got {len(entries)}")
    return entries


def _bar_close(bar: SchwabBar) -> datetime:
    return datetime.fromtimestamp((bar.ts + BAR_CLOSE_OFFSET_MS) / 1000.0, UTC)


def missing_first_ten_bars(entry: LockedEntry, bars: list[SchwabBar]) -> tuple[datetime, ...]:
    available = {_bar_close(bar) for bar in bars}
    expected = tuple(entry.buy_signal_ts + timedelta(minutes=index) for index in range(1, 11))
    return tuple(close for close in expected if close not in available)


def _minute_low_progression(
    quotes: list[Quote], entry_ts: datetime, checkpoint_minutes: int
) -> tuple[bool, bool, int]:
    lows: list[float] = []
    for minute in range(1, checkpoint_minutes + 1):
        start = entry_ts + timedelta(minutes=minute - 1)
        end = entry_ts + timedelta(minutes=minute)
        values = [
            float(quote.bid)
            for quote in quotes
            if (quote.ts >= start if minute == 1 else quote.ts > start) and quote.ts <= end
        ]
        if not values:
            raise ValueError(f"no executable quote in elapsed minute {minute}")
        lows.append(min(values))

    running_low = lows[0]
    new_low = [False]
    for low in lows[1:]:
        made_new_low = low < running_low
        new_low.append(made_new_low)
        running_low = min(running_low, low)
    streak = 0
    for made_new_low in reversed(new_low):
        if not made_new_low:
            break
        streak += 1
    return new_low[-1], all(new_low[1:]), streak


def early_snapshot(
    entry: LockedEntry,
    quotes: list[Quote],
    bars: list[SchwabBar],
    checkpoint_minutes: int,
) -> EarlySnapshot:
    checkpoint = entry.entry_ts + timedelta(minutes=checkpoint_minutes)
    quote_times = [quote.ts for quote in quotes]
    start_index = bisect.bisect_left(quote_times, entry.entry_ts)
    end_index = bisect.bisect_right(quote_times, checkpoint)
    observed = quotes[start_index:end_index]
    if not observed or observed[-1].ts <= checkpoint - timedelta(minutes=1):
        raise ValueError("no current executable quote in the checkpoint minute")

    expected_bar_close = entry.buy_signal_ts + timedelta(minutes=checkpoint_minutes)
    bar_by_close = {_bar_close(bar): bar for bar in bars}
    last_bar = bar_by_close.get(expected_bar_close)
    if last_bar is None:
        raise ValueError(f"missing checkpoint bar at {expected_bar_close.isoformat()}")
    if last_bar.close > last_bar.open:
        direction = "up"
    elif last_bar.close < last_bar.open:
        direction = "down"
    else:
        direction = "flat"

    bids = [float(quote.bid) for quote in observed]
    current = bids[-1] / entry.entry_px * 100.0 - 100.0
    max_up = max(bids) / entry.entry_px * 100.0 - 100.0
    max_down = min(bids) / entry.entry_px * 100.0 - 100.0
    latest_new_low, every_new_low, streak = _minute_low_progression(
        observed, entry.entry_ts, checkpoint_minutes
    )
    return EarlySnapshot(
        session_day_et=entry.session_day_et,
        symbol=entry.symbol,
        buy_signal_ts=entry.buy_signal_ts,
        entry_ts=entry.entry_ts,
        entry_px=entry.entry_px,
        reached_5=entry.reached_5,
        checkpoint_minutes=checkpoint_minutes,
        checkpoint_ts=checkpoint,
        quote_ts=observed[-1].ts,
        current_return_pct=current,
        max_up_so_far_pct=max_up,
        max_down_so_far_pct=max_down,
        touched_plus_2=max_up >= 2.0,
        touched_minus_3=max_down <= -3.0,
        last_bar_ts=expected_bar_close,
        last_bar_direction=direction,
        latest_minute_new_low=latest_new_low,
        every_minute_new_low=every_new_low,
        new_low_streak_minutes=streak,
    )


def run_measurement(source, entries: list[LockedEntry]):
    grouped: dict[tuple[str, str], list[LockedEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.session_day_et, entry.symbol)].append(entry)

    snapshots: list[EarlySnapshot] = []
    excluded: list[ExcludedEntry] = []
    for (day_text, symbol), group in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        observation_start = datetime.combine(session_day, time(4), EASTERN)
        session_start = datetime.combine(session_day, time(7), EASTERN)
        session_end = datetime.combine(session_day, time(16), EASTERN).astimezone(UTC)
        bars = source.schwab_bars(symbol, observation_start, session_end)
        quotes = source.quotes(symbol, session_start, session_end)
        for entry in group:
            missing = missing_first_ten_bars(entry, bars)
            if missing:
                excluded.append(
                    ExcludedEntry(
                        session_day_et=entry.session_day_et,
                        symbol=entry.symbol,
                        buy_signal_ts=entry.buy_signal_ts,
                        entry_ts=entry.entry_ts,
                        reached_5=entry.reached_5,
                        missing_bar_closes=missing,
                    )
                )
                continue
            snapshots.extend(
                early_snapshot(entry, quotes, bars, checkpoint) for checkpoint in CHECKPOINTS
            )
    return snapshots, excluded


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, statistics.median(values), q3


def distribution_rows(snapshots: list[EarlySnapshot]) -> list[dict[str, object]]:
    measures = (
        ("Price vs entry", "current_return_pct"),
        ("Highest point so far", "max_up_so_far_pct"),
        ("Lowest point so far", "max_down_so_far_pct"),
        ("Consecutive new-low minutes", "new_low_streak_minutes"),
    )
    result: list[dict[str, object]] = []
    for checkpoint in CHECKPOINTS:
        at_checkpoint = [row for row in snapshots if row.checkpoint_minutes == checkpoint]
        for label, field in measures:
            output: dict[str, object] = {"checkpoint_minutes": checkpoint, "measure": label}
            for prefix, reached in (("winner", True), ("loser", False)):
                values = [
                    float(getattr(row, field)) for row in at_checkpoint if row.reached_5 is reached
                ]
                q1, median, q3 = _quartiles(values)
                output.update(
                    {
                        f"{prefix}_count": len(values),
                        f"{prefix}_q1": round(q1, 4),
                        f"{prefix}_median": round(median, 4),
                        f"{prefix}_q3": round(q3, 4),
                    }
                )
            result.append(output)
    return result


def category_rows(snapshots: list[EarlySnapshot]) -> list[dict[str, object]]:
    categories = (
        ("Touched +2%", lambda row: row.touched_plus_2, "Yes", "No"),
        ("Touched -3%", lambda row: row.touched_minus_3, "Yes", "No"),
        ("Last bar up", lambda row: row.last_bar_direction == "up", "Yes", "No"),
        ("Last bar down", lambda row: row.last_bar_direction == "down", "Yes", "No"),
        ("Last bar flat", lambda row: row.last_bar_direction == "flat", "Yes", "No"),
        ("Latest minute made new low", lambda row: row.latest_minute_new_low, "Yes", "No"),
        ("Every minute made new low", lambda row: row.every_minute_new_low, "Yes", "No"),
    )
    result: list[dict[str, object]] = []
    for checkpoint in CHECKPOINTS:
        at_checkpoint = [row for row in snapshots if row.checkpoint_minutes == checkpoint]
        for label, predicate, yes_label, no_label in categories:
            output: dict[str, object] = {
                "checkpoint_minutes": checkpoint,
                "measure": label,
                "side_true": yes_label,
                "side_false": no_label,
            }
            for prefix, reached in (("winner", True), ("loser", False)):
                group = [row for row in at_checkpoint if row.reached_5 is reached]
                true_count = sum(predicate(row) for row in group)
                output.update(
                    {
                        f"{prefix}_count": len(group),
                        f"{prefix}_true": true_count,
                        f"{prefix}_false": len(group) - true_count,
                    }
                )
            result.append(output)
    return result


def threshold_rows(snapshots: list[EarlySnapshot]) -> list[dict[str, object]]:
    numeric = (
        ("Price vs entry", "current_return_pct"),
        ("Highest point so far", "max_up_so_far_pct"),
        ("Lowest point so far", "max_down_so_far_pct"),
        ("Consecutive new-low minutes", "new_low_streak_minutes"),
    )
    binary = (
        ("Touched +2%", "touched_plus_2"),
        ("Touched -3%", "touched_minus_3"),
        ("Latest minute made new low", "latest_minute_new_low"),
        ("Every minute made new low", "every_minute_new_low"),
    )
    result: list[dict[str, object]] = []

    def append_counts(
        checkpoint: int,
        at_checkpoint: list[EarlySnapshot],
        label: str,
        cut: str,
        predicate,
        threshold_value: object,
    ) -> None:
        output: dict[str, object] = {
            "checkpoint_minutes": checkpoint,
            "measure": label,
            "cut": cut,
            "threshold_value": threshold_value,
        }
        for prefix, reached in (("winner", True), ("loser", False)):
            group = [row for row in at_checkpoint if row.reached_5 is reached]
            side = sum(predicate(row) for row in group)
            output.update(
                {
                    f"{prefix}_denominator": len(group),
                    f"{prefix}_on_cut_side": side,
                    f"{prefix}_other_side": len(group) - side,
                }
            )
        output["separates"] = (
            int(output["loser_on_cut_side"]) >= 25 and int(output["winner_other_side"]) >= 35
        )
        result.append(output)

    for checkpoint in CHECKPOINTS:
        at_checkpoint = [row for row in snapshots if row.checkpoint_minutes == checkpoint]
        for label, field in numeric:
            values = sorted({float(getattr(row, field)) for row in at_checkpoint})
            for threshold in values:
                append_counts(
                    checkpoint,
                    at_checkpoint,
                    label,
                    f"<= {threshold:+.6g}",
                    lambda row, field=field, threshold=threshold: (
                        float(getattr(row, field)) <= threshold
                    ),
                    threshold,
                )
                append_counts(
                    checkpoint,
                    at_checkpoint,
                    label,
                    f">= {threshold:+.6g}",
                    lambda row, field=field, threshold=threshold: (
                        float(getattr(row, field)) >= threshold
                    ),
                    threshold,
                )
        for label, field in binary:
            for side in (True, False):
                append_counts(
                    checkpoint,
                    at_checkpoint,
                    label,
                    f"is {side}",
                    lambda row, field=field, side=side: bool(getattr(row, field)) is side,
                    side,
                )
        for direction in ("up", "down", "flat"):
            append_counts(
                checkpoint,
                at_checkpoint,
                "Last bar direction",
                f"is {direction}",
                lambda row, direction=direction: row.last_bar_direction == direction,
                direction,
            )
    return result


def best_cut_rows(thresholds: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[int, str], list[dict[str, object]]] = defaultdict(list)
    for row in thresholds:
        if int(row["winner_other_side"]) >= 35:
            grouped[(int(row["checkpoint_minutes"]), str(row["measure"]))].append(row)
    return [
        max(
            grouped[key],
            key=lambda row: (
                int(row["loser_on_cut_side"]),
                -int(row["winner_on_cut_side"]),
            ),
        )
        for key in sorted(grouped)
    ]


def _report(
    path: Path,
    entries: list[LockedEntry],
    snapshots: list[EarlySnapshot],
    excluded: list[ExcludedEntry],
    distributions: list[dict[str, object]],
    categories: list[dict[str, object]],
    thresholds: list[dict[str, object]],
) -> None:
    winner_total = sum(row.reached_5 for row in entries)
    loser_total = len(entries) - winner_total
    winner_excluded = sum(row.reached_5 for row in excluded)
    loser_excluded = len(excluded) - winner_excluded
    lines = [
        "# ATR Early-Window Measurement: 2026-08-24 to 2026-09-01",
        "",
        f"Locked population: 97 entries ({winner_total} reached +5%; {loser_total} did not). "
        f"Excluded for a missing ATR bar in the first 10 minutes: {len(excluded)} "
        f"({winner_excluded} winners; {loser_excluded} losers). Every table denominator is "
        "therefore shown explicitly.",
        "",
        "Checkpoints are exactly 3, 5, and 10 elapsed minutes from the executable ask fill. "
        "Price, highest point, and lowest point use executable bids observed from fill through "
        "the checkpoint. The current price is the latest bid in the checkpoint minute. Last-bar "
        "direction uses the Schwab minute bar closing at that checkpoint's signal-relative "
        "minute. New-low progression uses consecutive elapsed-minute quote buckets; ties do not "
        "count as a new low.",
        "",
        "## Excluded Entries",
        "",
        "| Date | Symbol | Fill ET | Population | Missing bar closes ET |",
        "|---|---|---:|---|---|",
    ]
    for row in sorted(excluded, key=lambda item: item.entry_ts):
        missing = ", ".join(
            value.astimezone(EASTERN).strftime("%H:%M") for value in row.missing_bar_closes
        )
        lines.append(
            f"| {row.session_day_et} | {row.symbol} | "
            f"{row.entry_ts.astimezone(EASTERN).strftime('%H:%M:%S')} | "
            f"{'Reached +5' if row.reached_5 else 'Did not reach +5'} | {missing} |"
        )

    lines.extend(
        [
            "",
            "## Continuous Measures",
            "",
            "Values are `Q1 / median / Q3`; price measures are percentages and new-low streak "
            "is elapsed-minute buckets.",
            "",
            "| Minute | Measure | Winners Q1 / median / Q3 | Winner N | Losers Q1 / median / Q3 | Loser N |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for row in distributions:
        lines.append(
            f"| {row['checkpoint_minutes']} | {row['measure']} | "
            f"{float(row['winner_q1']):+.2f} / {float(row['winner_median']):+.2f} / "
            f"{float(row['winner_q3']):+.2f} | {row['winner_count']} | "
            f"{float(row['loser_q1']):+.2f} / {float(row['loser_median']):+.2f} / "
            f"{float(row['loser_q3']):+.2f} | {row['loser_count']} |"
        )

    lines.extend(
        [
            "",
            "## Categorical Splits",
            "",
            "| Minute | Measure | Side | Winners | Losers | Other side | Winners | Losers |",
            "|---:|---|---|---:|---:|---|---:|---:|",
        ]
    )
    for row in categories:
        lines.append(
            f"| {row['checkpoint_minutes']} | {row['measure']} | {row['side_true']} | "
            f"{row['winner_true']}/{row['winner_count']} | {row['loser_true']}/{row['loser_count']} | "
            f"{row['side_false']} | {row['winner_false']}/{row['winner_count']} | "
            f"{row['loser_false']}/{row['loser_count']} |"
        )

    separating = [row for row in thresholds if row["separates"]]
    best = best_cut_rows(thresholds)
    lines.extend(
        [
            "",
            "## Single-Measure Cuts Meeting the Stated Separation Test",
            "",
            "A row appears only when its cut side contains at least 25 losers and its other side "
            "retains at least 35 winners. The census tests both sides of every observed numeric "
            "boundary and every categorical value; it is not a combined score.",
            "",
            "| Minute | Measure | Cut side | Winners on side | Losers on side | Winners retained | Losers retained |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    if separating:
        for row in separating:
            lines.append(
                f"| {row['checkpoint_minutes']} | {row['measure']} | {row['cut']} | "
                f"{row['winner_on_cut_side']}/{row['winner_denominator']} | "
                f"{row['loser_on_cut_side']}/{row['loser_denominator']} | "
                f"{row['winner_other_side']}/{row['winner_denominator']} | "
                f"{row['loser_other_side']}/{row['loser_denominator']} |"
            )
    else:
        lines.append(
            "| - | No single observed cut met both count requirements | - | - | - | - | - |"
        )

    lines.extend(
        [
            "",
            "## Best Count Separation While Retaining at Least 35 Winners",
            "",
            "Each row is the cut that removes the most losers for that one measure and minute, "
            "subject to retaining at least 35 winners. `Meets test` still requires at least 25 "
            "losers removed.",
            "",
            "| Minute | Measure | Cut side | Winners removed | Losers removed | Winners retained | Losers retained | Meets test |",
            "|---:|---|---|---:|---:|---:|---:|:---:|",
        ]
    )
    for row in best:
        lines.append(
            f"| {row['checkpoint_minutes']} | {row['measure']} | {row['cut']} | "
            f"{row['winner_on_cut_side']}/{row['winner_denominator']} | "
            f"{row['loser_on_cut_side']}/{row['loser_denominator']} | "
            f"{row['winner_other_side']}/{row['winner_denominator']} | "
            f"{row['loser_other_side']}/{row['loser_denominator']} | "
            f"{'Yes' if row['separates'] else 'No'} |"
        )

    lines.extend(
        [
            "",
            "The complete observed-boundary census and both sides of every cut are in the companion "
            "`-thresholds.csv`. Exact per-entry snapshots are in `-snapshots.csv`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population-csv",
        type=Path,
        default=Path("analysis/reports/atr-trend-exit-2026-08-24-to-2026-09-01-trades.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    entries = load_locked_entries(args.population_csv)
    source = DbMarketDataSource(build_session_factory(get_settings()))
    snapshots, excluded = run_measurement(source, entries)
    distributions = distribution_rows(snapshots)
    categories = category_rows(snapshots)
    thresholds = threshold_rows(snapshots)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-early-window-2026-08-24-to-2026-09-01"
    _write_csv(args.output_dir / f"{stem}-snapshots.csv", [asdict(row) for row in snapshots])
    _write_csv(args.output_dir / f"{stem}-excluded.csv", [asdict(row) for row in excluded])
    _write_csv(args.output_dir / f"{stem}-distributions.csv", distributions)
    _write_csv(args.output_dir / f"{stem}-categories.csv", categories)
    _write_csv(args.output_dir / f"{stem}-thresholds.csv", thresholds)
    _report(
        args.output_dir / f"{stem}.md",
        entries,
        snapshots,
        excluded,
        distributions,
        categories,
        thresholds,
    )
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "entries": [asdict(row) for row in entries],
                "snapshots": [asdict(row) for row in snapshots],
                "excluded": [asdict(row) for row in excluded],
                "distributions": distributions,
                "categories": categories,
                "thresholds": thresholds,
            },
            default=_json_default,
            indent=2,
        )
        + "\n"
    )
    print(
        f"entries={len(entries)} snapshots={len(snapshots)} excluded={len(excluded)} "
        f"output={args.output_dir / stem}*"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
