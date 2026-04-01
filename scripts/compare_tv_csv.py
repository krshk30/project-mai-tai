from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.indicators import IndicatorEngine


@dataclass
class ComparisonRow:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    tv_vwap: float | None
    tv_ema9: float | None
    tv_ema20: float | None
    tv_histogram: float | None
    tv_macd: float | None
    tv_signal: float | None


def _maybe_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def load_tv_csv(path: Path) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                ComparisonRow(
                    timestamp=float(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["Volume"] or 0)),
                    tv_vwap=_maybe_float(row.get("Session VWAP")),
                    tv_ema9=_maybe_float(row.get("EMA 9")),
                    tv_ema20=_maybe_float(row.get("EMA 20")),
                    tv_histogram=_maybe_float(row.get("Histogram")),
                    tv_macd=_maybe_float(row.get("MACD")),
                    tv_signal=_maybe_float(row.get("Signal line")),
                )
            )
    return rows


def compare_rows(rows: list[ComparisonRow]) -> dict[str, dict[str, float | int | None]]:
    engine = IndicatorEngine(IndicatorConfig())
    max_diffs = {
        "ema9": 0.0,
        "ema20": 0.0,
        "macd": 0.0,
        "signal": 0.0,
        "histogram": 0.0,
        "vwap": 0.0,
    }
    last_values: dict[str, dict[str, float | int | None]] = {}

    for index in range(len(rows)):
        bars = [
            {
                "timestamp": row.timestamp,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for row in rows[: index + 1]
        ]
        result = engine.calculate(bars)
        if result is None:
            continue

        current = rows[index]
        pairs = {
            "ema9": current.tv_ema9,
            "ema20": current.tv_ema20,
            "macd": current.tv_macd,
            "signal": current.tv_signal,
            "histogram": current.tv_histogram,
            "vwap": current.tv_vwap,
        }
        current_values: dict[str, float | int | None] = {"timestamp": current.timestamp, "index": index}
        for key, tv_value in pairs.items():
            local_value = float(result[key])
            current_values[f"local_{key}"] = local_value
            current_values[f"tv_{key}"] = tv_value
            if tv_value is not None:
                diff = abs(local_value - tv_value)
                current_values[f"diff_{key}"] = diff
                if diff > max_diffs[key]:
                    max_diffs[key] = diff
            else:
                current_values[f"diff_{key}"] = None
        last_values = current_values

    return {
        "max_diffs": max_diffs,
        "last_values": last_values,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare TradingView CSV indicator columns against Mai Tai calculations.")
    parser.add_argument("csv_path", type=Path, help="TradingView-exported CSV path")
    args = parser.parse_args()

    rows = load_tv_csv(args.csv_path)
    results = compare_rows(rows)

    print("Rows:", len(rows))
    print("Max diffs:")
    for key, value in results["max_diffs"].items():
        print(f"  {key}: {value:.10f}")

    print("Last comparable row:")
    for key, value in results["last_values"].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
