"""Print and describe the 24 ATR entries that never touched +1%.

This report is descriptive. It performs no rule search, combination test, scoring, or build/holdout
split. The other 73 locked entries are used only as a column-by-column comparison population.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ScannerEvent:
    event_type: str
    event_at: datetime
    confirm_path: str | None
    rank_score: float | None
    force_watchlist: bool | None
    price: float | None
    day_volume: int | None
    float_used: int | None
    change_pct: float | None
    reconfirm_seq: int


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def load_scanner_events(session_factory) -> dict[tuple[str, str], list[ScannerEvent]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT trade_date, symbol, event_type, event_at, confirm_path, rank_score, "
                "force_watchlist, price, day_volume, float_used, change_pct, reconfirm_seq "
                "FROM scanner_confirmed_events "
                "WHERE trade_date BETWEEN '2026-08-24' AND '2026-09-01' "
                "AND (event_type!='CONFIRM' OR "
                "abs(extract(epoch from (created_at-event_at)))<=120) "
                "ORDER BY trade_date, symbol, event_at"
            )
        ).all()
    result: dict[tuple[str, str], list[ScannerEvent]] = defaultdict(list)
    for day, symbol, event_type, event_at, path, rank, forced, price, volume, float_used, change, seq in rows:
        result[(str(day), str(symbol))].append(
            ScannerEvent(
                event_type=str(event_type),
                event_at=event_at,
                confirm_path=str(path) if path is not None else None,
                rank_score=float(rank) if rank is not None else None,
                force_watchlist=bool(forced) if forced is not None else None,
                price=float(price) if price is not None else None,
                day_volume=int(volume) if volume is not None else None,
                float_used=int(float_used) if float_used is not None else None,
                change_pct=float(change) if change is not None else None,
                reconfirm_seq=int(seq),
            )
        )
    return result


def scanner_at_entry(
    events: Sequence[ScannerEvent], entry_ts: datetime
) -> tuple[ScannerEvent | None, ScannerEvent | None]:
    active: ScannerEvent | None = None
    for event in events:
        if event.event_at > entry_ts:
            break
        if event.event_type == "CONFIRM" and active is None:
            active = event
        elif event.event_type in ("FADE", "RETENTION_DROP"):
            active = None
    if active is None:
        prior = [event for event in events if event.event_type == "CONFIRM" and event.event_at <= entry_ts]
        active = prior[-1] if prior else None
    removal = next(
        (
            event for event in events
            if event.event_at > entry_ts and event.event_type in ("FADE", "RETENTION_DROP")
        ),
        None,
    )
    return active, removal


def _f(value: str) -> float | None:
    return float(value) if value else None


def _b(value: str) -> bool | None:
    return None if value == "" else value == "True"


def build_profiles(
    trend_rows: Sequence[dict[str, str]],
    loser_rows: Sequence[dict[str, str]],
    snapshot_rows: Sequence[dict[str, str]],
    bar_rows: Sequence[dict[str, str]],
    scanner_events: dict[tuple[str, str], list[ScannerEvent]],
) -> list[dict[str, object]]:
    target_keys = {
        (row["symbol"], row["buy_signal_ts"])
        for row in loser_rows
        if float(row["max_up_pct"]) < 1.0
    }
    if len(target_keys) != 24:
        raise RuntimeError(f"expected 24 target entries, got {len(target_keys)}")
    trend = {(row["symbol"], row["buy_signal_ts"]): row for row in trend_rows}
    fills = {
        (row["symbol"], row["buy_signal_ts"]): row
        for row in snapshot_rows
        if row["checkpoint_minutes"] == "0"
    }
    bars: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in bar_rows:
        bars[(row["symbol"], row["buy_signal_ts"])].append(row)
    profiles: list[dict[str, object]] = []
    for key, path in sorted(trend.items(), key=lambda item: item[1]["entry_ts"]):
        fill = fills[key]
        entry_ts = datetime.fromisoformat(path["entry_ts"])
        entry_px = float(path["entry_px"])
        low_ts = datetime.fromisoformat(path["low_ts"])
        confirm, removal = scanner_at_entry(
            scanner_events.get((path["session_day_et"], path["symbol"]), []), entry_ts
        )
        vwap = _f(fill["vwap"])
        trail = _f(fill["atr_trailing_stop"])
        profile: dict[str, object] = {
            "population": "never_touched_plus_1" if key in target_keys else "other_73",
            "symbol": path["symbol"],
            "date": path["session_day_et"],
            "entry_time_et": entry_ts.astimezone(EASTERN).strftime("%H:%M:%S"),
            "entry_ts": entry_ts,
            "entry_px": entry_px,
            "max_down_pct": float(path["atr_segment_max_down_pct"]),
            "low_time_et": low_ts.astimezone(EASTERN).strftime("%H:%M:%S"),
            "minutes_to_low": float(path["minutes_to_low"]),
            "entry_volume_ratio_20": _f(fill["volume_ratio_20"]),
            "entry_vs_vwap_pct": (
                (entry_px / vwap - 1.0) * 100.0 if vwap is not None and vwap > 0 else None
            ),
            "entry_macd_histogram": _f(fill["macd_histogram"]),
            "entry_macd_histogram_pct": _f(fill["macd_histogram_pct"]),
            "entry_macd_direction": fill["macd_histogram_direction"] or None,
            "entry_stochastic": _f(fill["stochastic"]),
            "entry_rsi": _f(fill["rsi"]),
            "entry_dot_count": int(fill["dot_consensus"]) if fill["dot_consensus"] else None,
            "entry_atr_trail": trail,
            "entry_atr_stop_vs_fill_pct": (
                (trail / entry_px - 1.0) * 100.0 if trail is not None else None
            ),
            "entry_atr_stop_position": (
                "above" if trail is not None and trail > entry_px else "below"
                if trail is not None and trail < entry_px else "equal" if trail is not None else None
            ),
            "scanner_confirm_time_et": (
                confirm.event_at.astimezone(EASTERN).strftime("%H:%M:%S") if confirm else None
            ),
            "scanner_age_minutes": (
                (entry_ts - confirm.event_at).total_seconds() / 60.0 if confirm else None
            ),
            "scanner_confirm_path": confirm.confirm_path if confirm else None,
            "scanner_rank_score": confirm.rank_score if confirm else None,
            "scanner_force_watchlist": confirm.force_watchlist if confirm else None,
            "scanner_price": confirm.price if confirm else None,
            "scanner_day_volume": confirm.day_volume if confirm else None,
            "scanner_float_used": confirm.float_used if confirm else None,
            "scanner_change_pct": confirm.change_pct if confirm else None,
            "scanner_reconfirm_seq": confirm.reconfirm_seq if confirm else None,
            "scanner_removal": removal.event_type if removal else None,
            "scanner_removal_time_et": (
                removal.event_at.astimezone(EASTERN).strftime("%H:%M:%S") if removal else None
            ),
        }
        trade_above_values: list[bool | None] = []
        for bar in sorted(bars[key], key=lambda row: int(row["bar_number"])):
            number = int(bar["bar_number"])
            prefix = f"bar{number}_"
            values: dict[str, object] = {
                "close_vs_entry_pct": _f(bar["close_vs_entry_pct"]),
                "close_above_entry": _b(bar["close_above_entry"]),
                "traded_above_entry": _b(bar["traded_above_entry"]),
                "direction": bar["bar_direction"] or None,
                "running_low_pct": _f(bar["running_low_pct"]),
                "volume_ratio_20": _f(bar["volume_ratio_20"]),
                "macd_histogram": _f(bar["macd_histogram"]),
                "macd_histogram_pct": _f(bar["macd_histogram_pct"]),
                "macd_direction": bar["macd_histogram_direction"] or None,
                "stochastic": _f(bar["stochastic"]),
                "rsi": _f(bar["rsi"]),
                "dot_count": int(bar["dot_consensus"]) if bar["dot_consensus"] else None,
                "price_vs_vwap_pct": _f(bar["price_vs_vwap_pct"]),
                "above_vwap": _b(bar["above_vwap"]),
                "atr_stop_vs_price_pct": _f(bar["atr_stop_vs_price_pct"]),
                "atr_stop_position": bar["atr_stop_position"] or None,
                "post_fill_trade_prints": int(bar["post_fill_trade_prints"]),
                "missing_state": bar["missing_state"],
            }
            trade_above_values.append(values["traded_above_entry"])
            profile.update({prefix + name: value for name, value in values.items()})
        if any(value is None for value in trade_above_values):
            shape = "incomplete_first_five"
        elif any(trade_above_values):
            shape = "sub_1pct_failed_bounce"
        else:
            shape = "never_reclaimed_entry"
        profile["first_five_shape"] = shape
        profiles.append(profile)
    if len(profiles) != 97:
        raise RuntimeError(f"expected 97 profiles, got {len(profiles)}")
    return profiles


def _fmt(value: object, digits: int = 2, suffix: str = "") -> str:
    if value is None or value == "":
        return "NA"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (float, int)):
        return f"{float(value):.{digits}f}{suffix}"
    return str(value)


def bar_narrative(row: dict[str, object], number: int) -> str:
    p = f"bar{number}_"
    print_state = row[p + "traded_above_entry"]
    print_text = "print>entry" if print_state is True else "no print>entry" if print_state is False else "prints NA"
    return (
        f"B{number}: close {_fmt(row[p+'close_vs_entry_pct'], suffix='%')}; {print_text}; "
        f"{row[p+'direction'] or 'direction NA'}; low {_fmt(row[p+'running_low_pct'], suffix='%')}; "
        f"vol {_fmt(row[p+'volume_ratio_20'], suffix='x')}; hist "
        f"{_fmt(row[p+'macd_histogram_pct'], suffix='%')} {row[p+'macd_direction'] or 'NA'}; "
        f"stoch {_fmt(row[p+'stochastic'])}; RSI {_fmt(row[p+'rsi'])}; "
        f"dot {_fmt(row[p+'dot_count'], digits=0)}; VWAP "
        f"{_fmt(row[p+'price_vs_vwap_pct'], suffix='%')}; ATR {row[p+'atr_stop_position'] or 'NA'}"
    )


def _range_median(values: Sequence[float]) -> str:
    return f"{min(values):.2f} to {max(values):.2f}; median {statistics.median(values):.2f}"


def _numeric_summary(
    profiles: Sequence[dict[str, object]], field: str
) -> tuple[str, str]:
    output = []
    for population in ("never_touched_plus_1", "other_73"):
        values = [
            float(row[field]) for row in profiles
            if row["population"] == population and row[field] is not None
        ]
        output.append(f"{_range_median(values)} (n={len(values)})" if values else "NA")
    return output[0], output[1]


def _bin_counts(values: Sequence[float], cuts: Sequence[float], labels: Sequence[str]) -> str:
    counts = [0] * labels.__len__()
    for value in values:
        index = next((i for i, cut in enumerate(cuts) if value < cut), len(cuts))
        counts[index] += 1
    return "; ".join(f"{label}: {count}" for label, count in zip(labels, counts, strict=True))


def _population_values(profiles, population, field) -> list[float]:
    return [
        float(row[field]) for row in profiles
        if row["population"] == population and row[field] is not None
    ]


def _category_counts(profiles, population, field) -> str:
    values = [
        str(row[field]) for row in profiles
        if row["population"] == population and row[field] is not None
    ]
    counts = Counter(values)
    return "; ".join(f"{key}: {counts[key]}" for key in sorted(counts)) + f" (n={len(values)})"


def write_report(path: Path, profiles: Sequence[dict[str, object]]) -> None:
    targets = [row for row in profiles if row["population"] == "never_touched_plus_1"]
    others = [row for row in profiles if row["population"] == "other_73"]
    lines = [
        "# The 24 ATR Entries That Never Touched +1%",
        "",
        "This is the requested descriptive table. There is no build/holdout split, filter, "
        "combination search, score, or proposed rule. The other 73 entries appear only after the "
        "24 have been printed and described.",
        "",
        "## All 24 Trades",
        "",
        "| # | Date | Symbol | Entry ET | Fell / low | Fill state | ATR at fill | Bars 1-5 | Scanner |",
        "|---:|---|---|---:|---|---|---|---|---|",
    ]
    for index, row in enumerate(targets, 1):
        fill = (
            f"vol {_fmt(row['entry_volume_ratio_20'], suffix='x')}; VWAP "
            f"{_fmt(row['entry_vs_vwap_pct'], suffix='%')}; hist "
            f"{_fmt(row['entry_macd_histogram_pct'], suffix='%')} "
            f"{row['entry_macd_direction'] or 'NA'}; stoch {_fmt(row['entry_stochastic'])}; "
            f"RSI {_fmt(row['entry_rsi'])}; dot {_fmt(row['entry_dot_count'], digits=0)}"
        )
        atr = (
            f"{row['entry_atr_stop_position'] or 'NA'} by "
            f"{_fmt(row['entry_atr_stop_vs_fill_pct'], suffix='%')} "
            f"(trail {_fmt(row['entry_atr_trail'], digits=4)})"
        )
        bars = "<br>".join(bar_narrative(row, number) for number in range(1, 6))
        scanner = (
            f"CONFIRM {row['scanner_confirm_time_et'] or 'NA'}; "
            f"path {row['scanner_confirm_path'] or 'NA'}; rank {_fmt(row['scanner_rank_score'])}; "
            f"change {_fmt(row['scanner_change_pct'], suffix='%')}; "
            f"day vol {_fmt(row['scanner_day_volume'], digits=0)}; "
            f"float {_fmt(row['scanner_float_used'], digits=0)}; "
            f"age {_fmt(row['scanner_age_minutes'], suffix='m')}; removal "
            f"{row['scanner_removal'] or 'none'} {row['scanner_removal_time_et'] or ''}"
        )
        lines.append(
            f"| {index} | {row['date']} | {row['symbol']} | {row['entry_time_et']} | "
            f"{float(row['max_down_pct']):.2f}% at {row['low_time_et']} "
            f"({float(row['minutes_to_low']):.2f}m) | {fill} | {atr} | {bars} | {scanner} |"
        )

    numeric = [
        ("Maximum fall %", "max_down_pct", (-8, -3), ("past -8", "-8 to -3", "-3 to 0")),
        ("Minutes to low", "minutes_to_low", (5, 15), ("under 5", "5 to 15", "over 15")),
        ("Entry volume / 20-bar average", "entry_volume_ratio_20", (0.75, 1.25), ("under .75x", ".75-1.25x", "over 1.25x")),
        ("Entry price vs VWAP %", "entry_vs_vwap_pct", (-2, 0), ("below -2", "-2 to 0", "above 0")),
        ("Entry MACD histogram %", "entry_macd_histogram_pct", (0,), ("negative", "zero/positive")),
        ("Entry stochastic", "entry_stochastic", (30, 70), ("under 30", "30-70", "over 70")),
        ("Entry RSI", "entry_rsi", (30, 70), ("under 30", "30-70", "over 70")),
        ("ATR stop vs fill %", "entry_atr_stop_vs_fill_pct", (-5, 0), ("below -5", "-5 to 0", "above fill")),
        ("Scanner age minutes", "scanner_age_minutes", (15, 60), ("under 15", "15-60", "over 60")),
        ("Scanner rank", "scanner_rank_score", (40, 60), ("under 40", "40-60", "over 60")),
        ("Scanner change %", "scanner_change_pct", (20, 50), ("under 20", "20-50", "over 50")),
    ]
    lines.extend([
        "",
        "## Column Description",
        "",
        "| Column | The 24: range and median | The 24: obvious groups | The 73: range and median | The 73: obvious groups |",
        "|---|---|---|---|---|",
    ])
    for label, field, cuts, labels in numeric:
        target_values = _population_values(profiles, "never_touched_plus_1", field)
        other_values = _population_values(profiles, "other_73", field)
        target_summary, other_summary = _numeric_summary(profiles, field)
        lines.append(
            f"| {label} | {target_summary} | {_bin_counts(target_values, cuts, labels)} | "
            f"{other_summary} | {_bin_counts(other_values, cuts, labels)} |"
        )
    lines.extend([
        "",
        "| Categorical column | The 24 | The 73 |",
        "|---|---|---|",
    ])
    for label, field in (
        ("Entry MACD direction", "entry_macd_direction"),
        ("Entry dot count", "entry_dot_count"),
        ("Entry ATR stop position", "entry_atr_stop_position"),
        ("Scanner confirm path", "scanner_confirm_path"),
        ("First-five shape", "first_five_shape"),
    ):
        lines.append(
            f"| {label} | {_category_counts(profiles, 'never_touched_plus_1', field)} | "
            f"{_category_counts(profiles, 'other_73', field)} |"
        )

    lines.extend([
        "",
        "## First Five Bars: 24 Versus 73",
        "",
        "Counts are direct states, not fitted cuts. Medians use only available values.",
        "",
        "| Bar | State | The 24 | The 73 |",
        "|---:|---|---:|---:|",
    ])
    for number in range(1, 6):
        for label, field, wanted in (
            ("Close above entry", "close_above_entry", True),
            ("Any post-fill print above entry", "traded_above_entry", True),
            ("Up bar", "direction", "up"),
            ("MACD histogram rising", "macd_direction", "rising"),
            ("Close above VWAP", "above_vwap", True),
            ("ATR stop above close", "atr_stop_position", "above"),
        ):
            full_field = f"bar{number}_{field}"
            target_available = [row for row in targets if row[full_field] is not None]
            other_available = [row for row in others if row[full_field] is not None]
            lines.append(
                f"| {number} | {label} | "
                f"{sum(row[full_field] == wanted for row in target_available)}/{len(target_available)} | "
                f"{sum(row[full_field] == wanted for row in other_available)}/{len(other_available)} |"
            )
        for label, field in (
            ("Median close vs entry %", "close_vs_entry_pct"),
            ("Median running low %", "running_low_pct"),
            ("Median volume ratio", "volume_ratio_20"),
            ("Median MACD histogram %", "macd_histogram_pct"),
            ("Median stochastic", "stochastic"),
            ("Median RSI", "rsi"),
            ("Median dot count", "dot_count"),
            ("Median price vs VWAP %", "price_vs_vwap_pct"),
            ("Median ATR stop vs price %", "atr_stop_vs_price_pct"),
        ):
            full_field = f"bar{number}_{field}"
            target_values = [float(row[full_field]) for row in targets if row[full_field] is not None]
            other_values = [float(row[full_field]) for row in others if row[full_field] is not None]
            lines.append(
                f"| {number} | {label} | {statistics.median(target_values):.2f} (n={len(target_values)}) | "
                f"{statistics.median(other_values):.2f} (n={len(other_values)}) |"
            )

    shape_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in targets:
        shape_groups[str(row["first_five_shape"])].append(row)
    lines.extend([
        "",
        "## One Thing or Several?",
        "",
        "The 24 are not one monotonic shape. The first-five-print record contains two observable "
        "path types plus an incomplete-data group:",
        "",
    ])
    for shape, description in (
        ("never_reclaimed_entry", "Never reclaimed the ask fill in any of bars 1-5"),
        ("sub_1pct_failed_bounce", "Printed above the fill but never reached +1%"),
        ("incomplete_first_five", "At least one first-five print window was unavailable"),
    ):
        rows = shape_groups[shape]
        names = ", ".join(
            f"{row['date']} {row['symbol']} {row['entry_time_et']}" for row in rows
        )
        lines.append(f"- **{description}: {len(rows)}.** {names or 'None.'}")

    lines.extend([
        "",
        "## Single-Measure Comparison",
        "",
        "No thresholds were searched. The clearest direct count difference is whether a captured "
        "trade printed above the ask fill in bars 1-2; the exact per-bar counts are above. Fill-time "
        "volume, MACD, stochastic, RSI, dot count, VWAP and ATR position retain broad overlap; "
        "their full ranges and medians are shown rather than converted into a rule.",
        "",
        "The companion wide CSV contains one row per trade and every explicit fill/bar/scanner "
        "column for all 97 entries. This report makes no recommendation and proposes no rule.",
    ])
    path.write_text("\n".join(lines) + "\n")


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in row.items()
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trend-csv", type=Path,
        default=Path("analysis/reports/atr-trend-exit-2026-08-24-to-2026-09-01-trades.csv"),
    )
    parser.add_argument(
        "--loser-paths-csv", type=Path,
        default=Path("analysis/reports/atr-combination-study-2026-08-24-to-2026-09-01-loser-paths.csv"),
    )
    parser.add_argument(
        "--snapshots-csv", type=Path,
        default=Path("analysis/reports/atr-combination-study-2026-08-24-to-2026-09-01-snapshots.csv"),
    )
    parser.add_argument(
        "--bar-states-csv", type=Path,
        default=Path("analysis/reports/atr-straight-down-study-2026-08-24-to-2026-09-01-bar-states.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    scanner_events = load_scanner_events(build_session_factory(get_settings()))
    profiles = build_profiles(
        _read_csv(args.trend_csv),
        _read_csv(args.loser_paths_csv),
        _read_csv(args.snapshots_csv),
        _read_csv(args.bar_states_csv),
        scanner_events,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-straight-down-profile-2026-08-24-to-2026-09-01"
    _write_csv(args.output_dir / f"{stem}-all-97.csv", profiles)
    _write_csv(
        args.output_dir / f"{stem}-24-trades.csv",
        [row for row in profiles if row["population"] == "never_touched_plus_1"],
    )
    write_report(args.output_dir / f"{stem}.md", profiles)
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(
            profiles,
            default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value),
            indent=2,
        ) + "\n"
    )
    print(f"profiles={len(profiles)} target=24 output={args.output_dir / stem}*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
