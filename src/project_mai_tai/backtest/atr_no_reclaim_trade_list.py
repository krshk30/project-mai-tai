"""Print the 22 locked ATR entries with no post-fill print above entry in bars 1-5."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_combination_study import _indicator_context
from project_mai_tai.backtest.data import Quote

EASTERN = ZoneInfo("America/New_York")

GROUP_NEVER_PLUS_1 = "14_never_above_never_plus_1"
GROUP_REACHED_PLUS_5 = "5_never_above_reached_plus_5"
GROUP_PARTIAL = "3_never_above_plus_1_not_plus_5"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def select_rows(
    trend_rows: Sequence[dict[str, str]],
    profile_rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    trend = {(row["symbol"], row["entry_ts"]): row for row in trend_rows}
    selected: list[dict[str, str]] = []
    for profile in profile_rows:
        if profile["first_five_shape"] != "never_reclaimed_entry":
            continue
        path = trend[(profile["symbol"], profile["entry_ts"])]
        if profile["population"] == "never_touched_plus_1":
            group = GROUP_NEVER_PLUS_1
        elif path["reached_5"].lower() == "true":
            group = GROUP_REACHED_PLUS_5
        else:
            max_up = float(path["atr_segment_max_up_pct"])
            if not 1.0 <= max_up < 5.0:
                raise RuntimeError(f"unexpected partial max-up for {profile['symbol']}: {max_up}")
            group = GROUP_PARTIAL
        selected.append({**profile, **path, "group": group})

    counts = Counter(row["group"] for row in selected)
    expected = {
        GROUP_NEVER_PLUS_1: 14,
        GROUP_REACHED_PLUS_5: 5,
        GROUP_PARTIAL: 3,
    }
    if counts != expected:
        raise RuntimeError(f"expected corrected 14/5/3 population, got {dict(counts)}")
    return sorted(selected, key=lambda row: row["entry_ts"])


def excursion_times(
    row: dict[str, str], quotes: Sequence[Quote]
) -> tuple[datetime, float, datetime, float]:
    entry_ts = datetime.fromisoformat(row["entry_ts"])
    exit_ts = datetime.fromisoformat(row["atr_sell_exit_ts"])
    observed = [quote for quote in quotes if entry_ts <= quote.ts <= exit_ts]
    if not observed:
        raise RuntimeError(f"no excursion quotes for {row['symbol']} {row['entry_ts']}")
    peak = max(observed, key=lambda quote: float(quote.bid))
    low = min(observed, key=lambda quote: float(quote.bid))
    entry_px = float(row["entry_px"])
    max_up = (float(peak.bid) / entry_px - 1.0) * 100.0
    max_down = (float(low.bid) / entry_px - 1.0) * 100.0
    if abs(max_up - float(row["atr_segment_max_up_pct"])) > 0.01:
        raise RuntimeError(f"max-up mismatch for {row['symbol']} {row['entry_ts']}")
    if abs(max_down - float(row["atr_segment_max_down_pct"])) > 0.01:
        raise RuntimeError(f"max-down mismatch for {row['symbol']} {row['entry_ts']}")
    return peak.ts, max_up, low.ts, max_down


def build_output_rows(source, settings, selected: Sequence[dict[str, str]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        grouped[(row["session_day_et"], row["symbol"])].append(row)

    output: list[dict[str, object]] = []
    for (day_text, symbol), rows in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        observation_start = datetime.combine(session_day, time(4), EASTERN)
        session_start = datetime.combine(session_day, time(7), EASTERN)
        session_end = datetime.combine(session_day, time(16), EASTERN)
        context = _indicator_context(
            symbol, source.schwab_bars(symbol, observation_start, session_end), settings
        )
        quotes = source.quotes(symbol, session_start, session_end)
        for row in rows:
            entry_ts = datetime.fromisoformat(row["entry_ts"])
            buy_signal_ts = datetime.fromisoformat(row["buy_signal_ts"])
            exit_ts = datetime.fromisoformat(row["atr_sell_exit_ts"])
            peak_ts, max_up, low_ts, max_down = excursion_times(row, quotes)
            entry_px = float(row["entry_px"])
            entry_vwap = float(row["vwap"])
            item: dict[str, object] = {
                "group": row["group"],
                "date": row["session_day_et"],
                "symbol": symbol,
                "atr_buy_time_et": buy_signal_ts.astimezone(EASTERN).strftime("%H:%M:%S"),
                "fill_time_et": entry_ts.astimezone(EASTERN).strftime("%H:%M:%S.%f"),
                "fill_price": entry_px,
                "exit_time_et": exit_ts.astimezone(EASTERN).strftime("%H:%M:%S.%f"),
                "exit_price": float(row["atr_sell_exit_px"]),
                "exit_trigger": row["atr_sell_trigger"],
                "exit_reason": row["atr_sell_exit_reason"],
                "max_down_pct": max_down,
                "max_down_time_et": low_ts.astimezone(EASTERN).strftime("%H:%M:%S.%f"),
                "minutes_to_max_down": (low_ts - entry_ts).total_seconds() / 60.0,
                "max_up_pct": max_up,
                "max_up_time_et": peak_ts.astimezone(EASTERN).strftime("%H:%M:%S.%f"),
                "minutes_to_max_up": (peak_ts - entry_ts).total_seconds() / 60.0,
                "fill_volume_ratio_20": float(row["volume_ratio_20"]),
                "fill_vwap": entry_vwap,
                "fill_price_vs_vwap_pct": (entry_px / entry_vwap - 1.0) * 100.0,
                "fill_macd_histogram": float(row["macd_histogram"]),
                "fill_stochastic": float(row["stochastic"]),
                "fill_rsi": float(row["rsi"]),
                "fill_dot_count": int(row["dot_consensus"]),
                "fill_atr_trailing_stop": float(row["atr_trailing_stop"]),
                "scanner_confirm_time_et": row["scanner_confirm_time_et"],
                "scanner_drop_type": row["scanner_removal"] or "none",
                "scanner_drop_time_et": row["scanner_removal_time_et"] or "none",
            }
            for number in range(1, 6):
                expected_close = buy_signal_ts + timedelta(minutes=number)
                indicator = context.get(expected_close)
                if indicator is None:
                    values = {name: None for name in ("open", "high", "low", "close", "volume")}
                else:
                    bar = indicator["bar"]
                    values = {
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume),
                    }
                item.update({f"bar{number}_{name}": value for name, value in values.items()})
            output.append(item)
    if len(output) != 22:
        raise RuntimeError(f"expected 22 output rows, got {len(output)}")
    return sorted(output, key=lambda row: (str(row["date"]), str(row["fill_time_et"])))


def _fmt(value: object, digits: int = 2) -> str:
    if value is None or value == "":
        return "NA"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _group_label(group: str) -> str:
    return {
        GROUP_NEVER_PLUS_1: "14: never above; never +1%",
        GROUP_REACHED_PLUS_5: "5: never above; reached +5%",
        GROUP_PARTIAL: "3: never above; +1% to <+5%",
    }[group]


def _exit_label(row: dict[str, object]) -> str:
    trigger = str(row["exit_trigger"])
    reason = str(row["exit_reason"])
    if trigger == reason:
        return trigger.replace("_", " ").upper()
    return f"{trigger.replace('_', ' ').upper()} / {reason.replace('_', ' ').upper()}"


def _scanner_drop_label(row: dict[str, object]) -> str:
    if row["scanner_drop_type"] == "none":
        return "not dropped"
    return f"{row['scanner_drop_time_et']} ET ({row['scanner_drop_type']})"


def write_report(path: Path, rows: Sequence[dict[str, object]]) -> None:
    lines = [
        "# 22 ATR Entries With No Trade Above Entry in Bars 1-5",
        "",
        "| Group | Date / symbol | ATR BUY / fill | Exit | Excursions | Fill state | Scanner | Bars 1-5 OHLCV |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        bars = "<br>".join(
            f"B{number}: O {_fmt(row[f'bar{number}_open'], 4)} / "
            f"H {_fmt(row[f'bar{number}_high'], 4)} / "
            f"L {_fmt(row[f'bar{number}_low'], 4)} / "
            f"C {_fmt(row[f'bar{number}_close'], 4)} / "
            f"V {_fmt(row[f'bar{number}_volume'], 0)}"
            for number in range(1, 6)
        )
        fill = (
            f"vol/avg {_fmt(row['fill_volume_ratio_20'])}x; VWAP {_fmt(row['fill_vwap'], 4)}; "
            f"fill-vs-VWAP {_fmt(row['fill_price_vs_vwap_pct'])}%; MACD hist "
            f"{_fmt(row['fill_macd_histogram'], 6)}; stoch {_fmt(row['fill_stochastic'])}; "
            f"RSI {_fmt(row['fill_rsi'])}; dot {_fmt(row['fill_dot_count'], 0)}; "
            f"ATR trail {_fmt(row['fill_atr_trailing_stop'], 4)}"
        )
        lines.append(
            f"| {_group_label(str(row['group']))} | {row['date']} {row['symbol']} | "
            f"BUY {row['atr_buy_time_et']} ET; fill {row['fill_time_et']} ET @ "
            f"{_fmt(row['fill_price'], 4)} | {row['exit_time_et']} ET @ "
            f"{_fmt(row['exit_price'], 4)}; {_exit_label(row)} | "
            f"down {_fmt(row['max_down_pct'])}% at {row['max_down_time_et']} ET "
            f"({_fmt(row['minutes_to_max_down'])}m); up {_fmt(row['max_up_pct'])}% at "
            f"{row['max_up_time_et']} ET ({_fmt(row['minutes_to_max_up'])}m) | {fill} | "
            f"confirmed {row['scanner_confirm_time_et']} ET; {_scanner_drop_label(row)} | {bars} |"
        )
    path.write_text("\n".join(lines) + "\n")


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trend-csv",
        type=Path,
        default=Path("analysis/reports/atr-trend-exit-2026-08-24-to-2026-09-01-trades.csv"),
    )
    parser.add_argument(
        "--profile-csv",
        type=Path,
        default=Path(
            "analysis/reports/atr-straight-down-profile-2026-08-24-to-2026-09-01-all-97.csv"
        ),
    )
    parser.add_argument(
        "--snapshots-csv",
        type=Path,
        default=Path(
            "analysis/reports/atr-combination-study-2026-08-24-to-2026-09-01-snapshots.csv"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    snapshots = {
        (row["symbol"], row["entry_ts"]): row
        for row in _read_csv(args.snapshots_csv)
        if row["checkpoint_minutes"] == "0"
    }
    selected = select_rows(_read_csv(args.trend_csv), _read_csv(args.profile_csv))
    selected = [{**row, **snapshots[(row["symbol"], row["entry_ts"])]} for row in selected]
    base = get_settings()
    rows = build_output_rows(
        DbMarketDataSource(build_session_factory(base)), build_replay_settings(base=base), selected
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-no-reclaim-22-trades-2026-08-24-to-2026-09-01"
    _write_csv(args.output_dir / f"{stem}.csv", rows)
    write_report(args.output_dir / f"{stem}.md", rows)
    print(f"rows={len(rows)} output={args.output_dir / stem}.*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
