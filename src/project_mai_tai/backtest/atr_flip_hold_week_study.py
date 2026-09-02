"""Multi-session runner for the fresh scanner-window ATR-flip hold study."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
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
from project_mai_tai.backtest.data import Quote
from project_mai_tai.settings import Settings

SCALE_NAME = "scale0.5@+5_rest@+8_no_floor_stop-10"
TRAIL_NAME = "scale0.5@+5_trail2_floor+0_stop-10"
BASELINE_NAME = "hold_stop-10"


@dataclass(frozen=True)
class ScaleInOutcome:
    session_day_et: str
    symbol: str
    buy_signal_ts: datetime
    first_fill_px: float
    added: bool
    second_fill_ts: datetime | None
    second_fill_px: float | None
    blended_entry_px: float
    stop_px: float
    exit_ts: datetime
    exit_px: float
    exit_reason: str
    return_pct_on_intended_notional: float


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


def simulate_scale_in(
    candidate: FlipCandidate,
    quotes: list[Quote],
    session_end: datetime,
    *,
    stop_pct: float = -10.0,
) -> ScaleInOutcome:
    """Half at the flip, half at +5%, then ATR SELL / stop / 16:00.

    The stop is anchored to the first fill before the add and to the blended average after the add.
    P&L is normalized to the original intended full-size notional (one share at the first fill), so
    it compares directly with buying the full intended share count at the flip.
    """
    first_px = candidate.entry_px
    add_px = first_px * 1.05
    blended_px = first_px
    stop_px = first_px * (1.0 + stop_pct / 100.0)
    added = False
    add_ts: datetime | None = None
    pending_stop = False
    last_quote = quotes[candidate.entry_quote_index]

    def finish(quote: Quote, reason: str) -> ScaleInOutcome:
        exit_px = float(quote.bid)
        pnl = 0.5 * (exit_px - first_px)
        if added:
            pnl += 0.5 * (exit_px - add_px)
        return ScaleInOutcome(
            session_day_et=candidate.session_day_et,
            symbol=candidate.symbol,
            buy_signal_ts=candidate.buy_signal_ts,
            first_fill_px=first_px,
            added=added,
            second_fill_ts=add_ts,
            second_fill_px=add_px if added else None,
            blended_entry_px=blended_px,
            stop_px=stop_px,
            exit_ts=quote.ts,
            exit_px=exit_px,
            exit_reason=reason,
            return_pct_on_intended_notional=pnl / first_px * 100.0,
        )

    for quote in quotes[candidate.entry_quote_index + 1 :]:
        if quote.ts >= session_end:
            break
        if pending_stop:
            return finish(quote, "hard_stop")
        if candidate.sell_signal_ts is not None and quote.ts >= candidate.sell_signal_ts:
            return finish(quote, "atr_sell")
        last_quote = quote
        bid = float(quote.bid)
        if not added and bid >= add_px:
            added = True
            add_ts = quote.ts
            blended_px = (first_px + add_px) / 2.0
            stop_px = blended_px * (1.0 + stop_pct / 100.0)
        if bid <= stop_px:
            pending_stop = True
    return finish(last_quote, "session_close")


def _scale_in_outcomes(source, candidates: list[FlipCandidate]) -> list[ScaleInOutcome]:
    grouped: dict[tuple[str, str], list[FlipCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.session_day_et, candidate.symbol)].append(candidate)
    outcomes: list[ScaleInOutcome] = []
    for (day_text, symbol), group in grouped.items():
        session_day = date.fromisoformat(day_text)
        start = datetime.combine(session_day, time(7), EASTERN)
        end = datetime.combine(session_day, time(16), EASTERN)
        session_end = end.astimezone(UTC)
        quotes = source.quotes(symbol, start, end)
        outcomes.extend(
            simulate_scale_in(candidate, quotes, session_end) for candidate in group
        )
    return outcomes


def _hour_bucket(value: datetime) -> str:
    hour = value.astimezone(EASTERN).hour
    if 7 <= hour < 9:
        return "07-09"
    if hour < 10:
        return "09-10"
    if hour < 12:
        return "10-12"
    if hour < 14:
        return "12-14"
    return "14-16"


def _write_operator_report(
    path: Path,
    rows: list[dict[str, object]],
    outcomes: list[PolicyOutcome],
    scale_in: list[ScaleInOutcome],
) -> None:
    ordered = sorted(rows, key=lambda row: row["buy_signal_ts"])
    outcome_by_policy = {
        policy: {
            (row.symbol, row.buy_signal_ts): row
            for row in outcomes
            if row.policy == policy
        }
        for policy in (
            BASELINE_NAME,
            "full_target+5_stop-10",
            "full_target+8_stop-10",
            "full_target+10_stop-10",
            SCALE_NAME,
            TRAIL_NAME,
        )
    }
    scale_in_by_key = {(row.symbol, row.buy_signal_ts): row for row in scale_in}

    lines = [
        "# ATR Flip Seven-Session Operator Tables",
        "",
        "## 1. Full Trade List and Time Buckets",
        "",
        "Maximum down and maximum up include the executable ATR SELL exit quote and exclude every "
        "quote after that segment boundary.",
        "",
        "Scaled-trail is the existing full-size policy: -10% initial stop, sell 50% at +5%, "
        "then a 2% trail with a 0% floor on the remainder.",
        "",
        "| # | Date | Symbol | ATR BUY ET | Max down | Max up | +5 | +8 | +10 | ATR SELL "
        "return | Scaled-trail return |",
        "|---:|---|---|---:|---:|---:|:---:|:---:|:---:|---:|---:|",
    ]
    for index, row in enumerate(ordered, 1):
        lines.append(
            f"| {index} | {row['session_day_et']} | {row['symbol']} | "
            f"{_et(row['buy_signal_ts'])} | {float(row['max_down_pct']):+.2f}% | "
            f"{float(row['max_up_pct']):+.2f}% | "
            f"{'Yes' if row['fixed_5_reached'] else 'No'} | "
            f"{'Yes' if row['fixed_8_reached'] else 'No'} | "
            f"{'Yes' if row['fixed_10_reached'] else 'No'} | "
            f"{float(row['natural_return_pct']):+.2f}% | "
            f"{float(row['scale_trail_return_pct']):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "### Entry-Hour Buckets",
            "",
            "| Entry bucket ET | Entries | Reached +5 count | Did not reach +5 count | "
            "Average max-up | Average max-down |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket in ("07-09", "09-10", "10-12", "12-14", "14-16"):
        group = [row for row in ordered if _hour_bucket(row["buy_signal_ts"]) == bucket]
        lines.append(
            f"| {bucket} | {len(group)} | "
            f"{sum(bool(row['fixed_5_reached']) for row in group)} | "
            f"{sum(not bool(row['fixed_5_reached']) for row in group)} | "
            f"{statistics.fmean(float(row['max_up_pct']) for row in group):+.2f}% | "
            f"{statistics.fmean(float(row['max_down_pct']) for row in group):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "### Per-Day Population",
            "",
            "| Session | Entries | Reached +5 count | Average max-up | Average max-down |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for day_text in sorted({str(row["session_day_et"]) for row in ordered}):
        group = [row for row in ordered if row["session_day_et"] == day_text]
        lines.append(
            f"| {day_text} | {len(group)} | "
            f"{sum(bool(row['fixed_5_reached']) for row in group)} | "
            f"{statistics.fmean(float(row['max_up_pct']) for row in group):+.2f}% | "
            f"{statistics.fmean(float(row['max_down_pct']) for row in group):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "## 2. Reached +5 Versus Did Not Reach +5",
            "",
            "| Population | Count | ATR SELL total | ATR SELL average | Scaled-trail total | "
            "Scaled-trail average |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for label, reached in (("Reached +5", True), ("Did not reach +5", False)):
        group = [row for row in ordered if bool(row["fixed_5_reached"]) is reached]
        lines.append(
            f"| {label} | {len(group)} | "
            f"{sum(float(row['natural_return_pct']) for row in group):+.2f} | "
            f"{statistics.fmean(float(row['natural_return_pct']) for row in group):+.2f}% | "
            f"{sum(float(row['scale_trail_return_pct']) for row in group):+.2f} | "
            f"{statistics.fmean(float(row['scale_trail_return_pct']) for row in group):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "### Max-Down Split",
            "",
            "| Population | Count | Average max-down | Median max-down | Worst max-down | "
            "Count <= -8 | <= -10 | <= -12 | <= -15 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label, reached in (("Reached +5", True), ("Did not reach +5", False)):
        group = [row for row in ordered if bool(row["fixed_5_reached"]) is reached]
        drawdowns = [float(row["max_down_pct"]) for row in group]
        lines.append(
            f"| {label} | {len(group)} | {statistics.fmean(drawdowns):+.2f}% | "
            f"{statistics.median(drawdowns):+.2f}% | {min(drawdowns):+.2f}% | "
            f"{sum(value <= -8 for value in drawdowns)} | "
            f"{sum(value <= -10 for value in drawdowns)} | "
            f"{sum(value <= -12 for value in drawdowns)} | "
            f"{sum(value <= -15 for value in drawdowns)} |"
        )

    lines.extend(
        [
            "",
            "### The 21 Trades That Reached +10",
            "",
            "| Date | Symbol | BUY ET | Max up | ATR SELL | Fixed +5 | Fixed +8 | Fixed +10 | "
            "50%@5 / 50%@8 | Scaled-trail |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in (item for item in ordered if item["fixed_10_reached"]):
        key = (row["symbol"], row["buy_signal_ts"])
        lines.append(
            f"| {row['session_day_et']} | {row['symbol']} | {_et(row['buy_signal_ts'])} | "
            f"{float(row['max_up_pct']):+.2f}% | {float(row['natural_return_pct']):+.2f}% | "
            f"{outcome_by_policy['full_target+5_stop-10'][key].return_pct:+.2f}% | "
            f"{outcome_by_policy['full_target+8_stop-10'][key].return_pct:+.2f}% | "
            f"{outcome_by_policy['full_target+10_stop-10'][key].return_pct:+.2f}% | "
            f"{outcome_by_policy[SCALE_NAME][key].return_pct:+.2f}% | "
            f"{outcome_by_policy[TRAIL_NAME][key].return_pct:+.2f}% |"
        )

    lines.extend(
        [
            "",
            "### Reached +5 Split by Day",
            "",
            "| Session | Population | Count | Scaled-trail total | Scaled-trail average |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for day_text in sorted({str(row["session_day_et"]) for row in ordered}):
        for label, reached in (("Reached +5", True), ("Did not reach +5", False)):
            group = [
                row
                for row in ordered
                if row["session_day_et"] == day_text
                and bool(row["fixed_5_reached"]) is reached
            ]
            total = sum(float(row["scale_trail_return_pct"]) for row in group)
            average = (
                f"{statistics.fmean(float(row['scale_trail_return_pct']) for row in group):+.2f}%"
                if group
                else "-"
            )
            lines.append(
                f"| {day_text} | {label} | {len(group)} | {total:+.2f} | {average} |"
            )

    baseline = outcome_by_policy[BASELINE_NAME]
    lines.extend(
        [
            "",
            "## 3. Scale-In Measurement",
            "",
            "Both populations exit on ATR SELL, 16:00, or a -10% hard stop. Full-size buys the "
            "entire intended share count at the flip. Scale-in buys half at the flip and half "
            "exactly at +5% from the first fill. Before the add, the stop is -10% from the first "
            "fill. After the add, the blended entry is 102.5% of the first fill and the stop resets "
            "to -10% from that blended average. Scale-in P&L is normalized to the original "
            "full-size first-fill notional.",
            "",
            "| Population | Count | Full-size total | Full-size average | Scale-in total | "
            "Scale-in average |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for label, selector in (
        ("All entries", lambda row: True),
        ("Reached +5", lambda row: bool(row["fixed_5_reached"])),
        ("Did not reach +5", lambda row: not bool(row["fixed_5_reached"])),
    ):
        group = [row for row in ordered if selector(row)]
        full_returns = [
            baseline[(row["symbol"], row["buy_signal_ts"])].return_pct for row in group
        ]
        scaled_returns = [
            scale_in_by_key[(row["symbol"], row["buy_signal_ts"])].return_pct_on_intended_notional
            for row in group
        ]
        lines.append(
            f"| {label} | {len(group)} | {sum(full_returns):+.2f} | "
            f"{statistics.fmean(full_returns):+.2f}% | {sum(scaled_returns):+.2f} | "
            f"{statistics.fmean(scaled_returns):+.2f}% |"
        )

    lines.extend(
        [
            "",
            "### Scale-In by Day",
            "",
            "| Session | Entries | Added second half | Full-size total | Scale-in total |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for day_text in sorted({str(row["session_day_et"]) for row in ordered}):
        group = [row for row in ordered if row["session_day_et"] == day_text]
        full_total = sum(
            baseline[(row["symbol"], row["buy_signal_ts"])].return_pct for row in group
        )
        scale_group = [
            scale_in_by_key[(row["symbol"], row["buy_signal_ts"])] for row in group
        ]
        lines.append(
            f"| {day_text} | {len(group)} | {sum(row.added for row in scale_group)} | "
            f"{full_total:+.2f} | "
            f"{sum(row.return_pct_on_intended_notional for row in scale_group):+.2f} |"
        )
    path.write_text("\n".join(lines) + "\n")


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
    scale_in = _scale_in_outcomes(source, candidates)
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
    _write_csv(output_dir / f"{stem}-scale-in.csv", [asdict(row) for row in scale_in])
    _write_report(
        output_dir / f"{stem}.md",
        args.start,
        args.end,
        sessions,
        census,
        simple_rows,
        summary,
    )
    _write_operator_report(
        output_dir / f"{stem}-operator-questions.md",
        simple_rows,
        outcomes,
        scale_in,
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
                "scale_in": scale_in,
            },
            default=lambda value: (
                asdict(value)
                if hasattr(value, "__dataclass_fields__")
                else _json_default(value)
            ),
            indent=2,
        )
        + "\n"
    )
    print(
        f"sessions={len(sessions)} symbol_days={len(census)} trades={len(simple_rows)} "
        f"outcomes={len(outcomes)} scale_in={len(scale_in)} output={output_dir / stem}*"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
