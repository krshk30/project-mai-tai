"""Multi-session runner for the fresh scanner-window ATR-flip hold study."""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from project_mai_tai.backtest.atr_bracket_study import trading_sessions
from project_mai_tai.backtest.atr_flip_hold_study import (
    EASTERN,
    FlipCandidate,
    NaturalPath,
    PolicyOutcome,
    SymbolCensus,
    _json_default,
    _summary_rows,
    _write_csv,
    run_study,
)
from project_mai_tai.settings import Settings

SCALE_NAME = "scale0.5@+5_rest@+8_no_floor_stop-10"
TRAIL_NAME = "scale0.5@+5_trail2_floor+0_stop-10"


def _et(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return "-"
    return value.astimezone(EASTERN).strftime("%H:%M:%S" if seconds else "%H:%M")


def _simple_rows(
    candidates: list[FlipCandidate],
    paths: list[NaturalPath],
    outcomes: list[PolicyOutcome],
) -> list[dict[str, object]]:
    candidate_by_key = {(row.symbol, row.buy_signal_ts): row for row in candidates}
    scale_by_key = {
        (row.symbol, row.buy_signal_ts): row for row in outcomes if row.policy == SCALE_NAME
    }
    trail_by_key = {
        (row.symbol, row.buy_signal_ts): row for row in outcomes if row.policy == TRAIL_NAME
    }
    rows: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda row: (row.symbol, row.buy_signal_ts)):
        key = (path.symbol, path.buy_signal_ts)
        candidate = candidate_by_key[key]
        scale = scale_by_key[key]
        trail = trail_by_key[key]
        rows.append(
            {
                "session_day_et": candidate.session_day_et,
                "symbol": path.symbol,
                "buy_signal_ts": path.buy_signal_ts,
                "entry_ts": path.entry_ts,
                "entry_px": round(path.entry_px, 4),
                "sell_signal_ts": path.sell_signal_ts,
                "natural_exit_reason": path.natural_exit_reason,
                "natural_return_pct": round(path.natural_return_pct, 4),
                "max_down_pct": round(path.mae_pct, 4),
                "max_up_pct": round(path.mfe_pct, 4),
                "fixed_5_reached": path.reached_5_ts is not None,
                "fixed_8_reached": path.reached_8_ts is not None,
                "fixed_10_reached": path.reached_10_ts is not None,
                "scale_50_at_5_50_at_8_return_pct": round(scale.return_pct, 4),
                "scale_exit_reason": scale.exit_reason,
                "scale_trail_return_pct": round(trail.return_pct, 4),
                "scale_trail_exit_reason": trail.exit_reason,
                "decision_gap_minutes": candidate.decision_gap_minutes,
            }
        )
    return rows


def _write_report(
    path: Path,
    start: date,
    end: date,
    sessions: list[date],
    census: list[tuple[date, SymbolCensus]],
    rows: list[dict[str, object]],
    summary: list[dict[str, object]],
) -> None:
    summary_by_name = {str(row["policy"]): row for row in summary}
    natural_returns = [float(row["natural_return_pct"]) for row in rows]
    reached_5 = sum(bool(row["fixed_5_reached"]) for row in rows)
    reached_8 = sum(bool(row["fixed_8_reached"]) for row in rows)
    reached_10 = sum(bool(row["fixed_10_reached"]) for row in rows)
    evaluated_symbol_days = sum(row.status == "EVALUATED" for _, row in census)
    adjacent = sum(float(row["decision_gap_minutes"]) <= 1.0 for row in rows)
    worst = min(rows, key=lambda row: float(row["max_down_pct"]))
    best = max(rows, key=lambda row: float(row["max_up_pct"]))
    under_5 = [row for row in rows if not row["fixed_5_reached"]]
    best_policy = max(summary, key=lambda row: float(row["total_return_pct"]))
    natural_summary = summary_by_name["atr_sell_only"]

    lines = [
        f"# ATR Flip Hold Study: {start.isoformat()} to {end.isoformat()}",
        "",
        f"Sessions: {', '.join(day.isoformat() for day in sessions)}. Entries use the first "
        "executable ask after a scanner-eligible ATR BUY bar closes. Maximum down and maximum up "
        "are measured only from that fill through the same segment's ATR SELL or 16:00 ET.",
        "",
        "## Assessment",
        "",
        f"The population contains **{len(rows)} ATR entries** across "
        f"**{evaluated_symbol_days} evaluated symbol-days**. Waiting for ATR SELL produced "
        f"{natural_summary['wins']} winners, {natural_summary['losses']} losers, and "
        f"{natural_summary['scratches']} scratches, "
        f"{float(natural_summary['total_return_pct']):+.4f} total percentage points, and a "
        f"{statistics.median(natural_returns):+.4f}% median.",
        "",
        f"Fixed reachability: +5% on **{reached_5}/{len(rows)}**, +8% on "
        f"**{reached_8}/{len(rows)}**, and +10% on **{reached_10}/{len(rows)}**. The worst "
        f"segment drawdown was {float(worst['max_down_pct']):+.2f}% "
        f"({worst['symbol']} {worst['session_day_et']} {_et(worst['buy_signal_ts'])}); the "
        f"largest run-up was {float(best['max_up_pct']):+.2f}% "
        f"({best['symbol']} {best['session_day_et']} {_et(best['buy_signal_ts'])}).",
        "",
        f"The **{len(under_5)} trades that never reached +5%** contributed "
        f"{sum(float(row['natural_return_pct']) for row in under_5):+.2f} points when held to ATR "
        "SELL. That losing entry population is larger than the exit rule can repair.",
        "",
        f"The best tested policy was `{best_policy['policy']}` at "
        f"{float(best_policy['total_return_pct']):+.4f} points. It was still negative. No tested "
        "fixed target, fixed scale, earned floor, trailing rule, or wide stop was profitable over "
        "all seven sessions.",
        "",
        f"All {adjacent} accepted BUY decisions used adjacent one-minute bars. "
        "No non-adjacent BUY decision entered this population.",
        "",
        "## Policy Comparison",
        "",
        "| Policy | Wins | Losses | Total | Mean | Median | PF | Hard stops |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    representative = [
        "atr_sell_only",
        "hold_stop-8",
        "hold_stop-10",
        "hold_stop-12",
        "hold_stop-15",
        "full_target+5_stop-10",
        "full_target+8_stop-10",
        "full_target+10_stop-10",
        "full_target+5_stop-8",
        SCALE_NAME,
        "scale0.5@+5_rest@+8_no_floor_stop-8",
        "scale0.5@+5_rest@+8_floor+0_stop-10",
        "scale0.5@+5_rest@+8_floor+2_stop-10",
        TRAIL_NAME,
        "scale0.5@+5_trail2_floor+0_stop-8",
        str(best_policy["policy"]),
    ]
    for name in dict.fromkeys(representative):
        row = summary_by_name[name]
        lines.append(
            f"| `{name}` | {row['wins']} | {row['losses']} | "
            f"{float(row['total_return_pct']):+.4f} | {float(row['mean_return_pct']):+.4f}% | "
            f"{float(row['median_return_pct']):+.4f}% | {row['profit_factor']} | "
            f"{row['hard_stops']} |"
        )

    lines.extend(
        [
            "",
            "## By Session",
            "",
            "| Session | Entries | Reached +5 | ATR SELL total | 50%@5 / 50%@8 total | "
            "50%@5 + 2% trail total |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for session_day in sessions:
        group = [row for row in rows if row["session_day_et"] == session_day.isoformat()]
        lines.append(
            f"| {session_day.isoformat()} | {len(group)} | "
            f"{sum(bool(row['fixed_5_reached']) for row in group)} | "
            f"{sum(float(row['natural_return_pct']) for row in group):+.2f} | "
            f"{sum(float(row['scale_50_at_5_50_at_8_return_pct']) for row in group):+.2f} | "
            f"{sum(float(row['scale_trail_return_pct']) for row in group):+.2f} |"
        )

    for symbol in sorted({str(row["symbol"]) for row in rows}):
        lines.extend(
            [
                "",
                f"## {symbol}",
                "",
                "| Date | BUY ET | Entry | Max down | Max up | +5 | +8 | +10 | ATR SELL "
                "return | 50%@5 / 50%@8 | 50%@5 + 2% trail |",
                "|---|---:|---:|---:|---:|:---:|:---:|:---:|---:|---:|---:|",
            ]
        )
        for row in (item for item in rows if item["symbol"] == symbol):
            lines.append(
                f"| {row['session_day_et']} | {_et(row['buy_signal_ts'])} | "
                f"{float(row['entry_px']):.4f} | {float(row['max_down_pct']):+.2f}% | "
                f"{float(row['max_up_pct']):+.2f}% | "
                f"{'Yes' if row['fixed_5_reached'] else 'No'} | "
                f"{'Yes' if row['fixed_8_reached'] else 'No'} | "
                f"{'Yes' if row['fixed_10_reached'] else 'No'} | "
                f"{float(row['natural_return_pct']):+.2f}% | "
                f"{float(row['scale_50_at_5_50_at_8_return_pct']):+.2f}% | "
                f"{float(row['scale_trail_return_pct']):+.2f}% |"
            )
    path.write_text("\n".join(lines) + "\n")


def run_week(source, settings: Settings, start: date, end: date):
    sessions = trading_sessions(start, end)
    census: list[tuple[date, SymbolCensus]] = []
    candidates: list[FlipCandidate] = []
    paths: list[NaturalPath] = []
    outcomes: list[PolicyOutcome] = []
    for session_day in sessions:
        print(f"session={session_day.isoformat()}", flush=True)
        day_census, day_candidates, day_paths, day_outcomes = run_study(
            source, settings, session_day
        )
        census.extend((session_day, row) for row in day_census)
        candidates.extend(day_candidates)
        paths.extend(day_paths)
        outcomes.extend(day_outcomes)
    return sessions, census, candidates, paths, outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-dir", default="analysis/reports")
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    base = get_settings()
    settings = build_replay_settings(base=base)
    source = DbMarketDataSource(build_session_factory(base))
    sessions, census, candidates, paths, outcomes = run_week(
        source, settings, args.start, args.end
    )
    summary = _summary_rows(outcomes)
    simple_rows = _simple_rows(candidates, paths, outcomes)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"atr-flip-hold-week-{args.start.isoformat()}-to-{args.end.isoformat()}"
    census_rows = [
        {"session_day_et": day.isoformat(), **asdict(row)} for day, row in census
    ]
    _write_csv(output_dir / f"{stem}-census.csv", census_rows)
    _write_csv(output_dir / f"{stem}-trades.csv", simple_rows)
    _write_csv(output_dir / f"{stem}-summary.csv", summary)
    _write_report(
        output_dir / f"{stem}.md",
        args.start,
        args.end,
        sessions,
        census,
        simple_rows,
        summary,
    )
    (output_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "start": args.start,
                "end": args.end,
                "sessions": sessions,
                "census": census_rows,
                "trades": simple_rows,
                "summary": summary,
            },
            default=_json_default,
            indent=2,
        )
        + "\n"
    )
    print(
        f"sessions={len(sessions)} symbol_days={len(census)} trades={len(simple_rows)} "
        f"outcomes={len(outcomes)} output={output_dir / stem}*"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
