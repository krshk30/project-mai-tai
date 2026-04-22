from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from project_mai_tai.db.models import Strategy, TradeIntent
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ

from reclaim_live_day_whatif import (
    _analyze_blockers,
    _analyze_closed_trades,
    _analyze_scenarios,
    _build_cold_pause_windows,
    _et_window,
    _forward_outcome,
    _load_bars,
    _load_reclaim_closed_today,
    _scenario_cold_pause_only,
    _simplify_reason,
    BarSample,
)


@dataclass
class MissedCandidate:
    symbol: str
    bar_time: str
    reason: str
    category: str
    price: float
    pop1: bool
    pop2: bool
    stop_first: bool


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a reclaim live-day markdown report.")
    parser.add_argument("--db-url", default=os.getenv("MAI_TAI_DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.getenv("MAI_TAI_REDIS_URL", ""))
    parser.add_argument("--strategy", default="macd_30s_reclaim")
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD format")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional symbol filter")
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target1-pct", type=float, default=1.0)
    parser.add_argument("--target2-pct", type=float, default=2.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp_replay/reclaim_live_day_report.md"),
        help="Markdown output path",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Optional JSON companion output",
    )
    return parser.parse_args()


def _iso_et(timestamp) -> str:
    return timestamp.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


def _parse_trade_time(day_text: str, value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.removesuffix(" ET").strip()
    try:
        local_dt = datetime.strptime(f"{day_text} {text}", "%Y-%m-%d %I:%M:%S %p")
    except ValueError:
        return None
    return local_dt.replace(tzinfo=EASTERN_TZ).astimezone(UTC)


def _load_trade_intents(
    session: Session,
    *,
    strategy_code: str,
    start_utc: datetime,
    end_utc: datetime,
    symbols: set[str] | None,
) -> list[dict[str, object]]:
    strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == strategy_code))
    if strategy_id is None:
        return []
    rows = session.execute(
        select(
            TradeIntent.symbol,
            TradeIntent.intent_type,
            TradeIntent.reason,
            TradeIntent.status,
            TradeIntent.payload,
            TradeIntent.created_at,
            TradeIntent.updated_at,
        )
        .where(
            TradeIntent.strategy_id == strategy_id,
            TradeIntent.created_at >= start_utc,
            TradeIntent.created_at < end_utc,
        )
        .order_by(TradeIntent.created_at.asc())
    ).all()
    results: list[dict[str, object]] = []
    for row in rows:
        symbol = str(row.symbol or "").upper()
        if symbols and symbol not in symbols:
            continue
        payload = dict(row.payload or {})
        results.append(
            {
                "symbol": symbol,
                "intent_type": str(row.intent_type or ""),
                "reason": str(row.reason or ""),
                "status": str(row.status or ""),
                "payload": payload,
                "created_at": row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC),
                "updated_at": row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC),
            }
        )
    return results


def _enrich_closed_trades_from_intents(
    closed_today: list[dict[str, object]],
    intents: list[dict[str, object]],
    *,
    day_text: str,
) -> list[dict[str, object]]:
    by_symbol: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in intents:
        by_symbol[str(item.get("symbol", "")).upper()].append(item)

    enriched: list[dict[str, object]] = []
    for trade in closed_today:
        item = dict(trade)
        symbol = str(item.get("ticker", "") or "").upper()
        entry_dt = _parse_trade_time(day_text, item.get("entry_time"))
        exit_dt = _parse_trade_time(day_text, item.get("exit_time"))
        symbol_intents = by_symbol.get(symbol, [])

        if symbol_intents:
            if str(item.get("reason", "") or "") in {"", "OMS_FILL"} and exit_dt is not None:
                close_candidates = [
                    intent
                    for intent in symbol_intents
                    if str(intent.get("intent_type", "")).lower() == "close"
                    and intent["created_at"] <= exit_dt + timedelta(minutes=1)
                ]
                if close_candidates:
                    nearest_close = min(
                        close_candidates,
                        key=lambda intent: abs((exit_dt - intent["created_at"]).total_seconds()),
                    )
                    item["reason"] = str(nearest_close.get("reason", "") or item.get("reason", ""))

            current_path = str(item.get("path", "") or "")
            if current_path in {"", "DB_RECONCILE"}:
                if entry_dt is None:
                    entry_dt = exit_dt
                if entry_dt is not None:
                    open_candidates = [
                        intent
                        for intent in symbol_intents
                        if str(intent.get("intent_type", "")).lower() == "open"
                        and intent["created_at"] <= entry_dt + timedelta(minutes=1)
                    ]
                    if open_candidates:
                        nearest_open = min(
                            open_candidates,
                            key=lambda intent: abs((entry_dt - intent["created_at"]).total_seconds()),
                        )
                        payload = dict(nearest_open.get("payload", {}) or {})
                        metadata = dict(payload.get("metadata", {}) or {})
                        item["path"] = str(
                            metadata.get("path")
                            or str(nearest_open.get("reason", "")).removeprefix("ENTRY_")
                            or current_path
                        )

        enriched.append(item)
    return enriched


def _collect_missed_candidates(
    bars_by_symbol: dict[str, list[BarSample]],
    *,
    lookahead_bars: int,
    target1_pct: float,
    target2_pct: float,
    stop_pct: float,
) -> list[MissedCandidate]:
    results: list[MissedCandidate] = []
    for symbol, bars in bars_by_symbol.items():
        for index, bar in enumerate(bars):
            if bar.decision_status != "blocked":
                continue
            outcome = _forward_outcome(
                bars,
                index,
                lookahead_bars=lookahead_bars,
                target1_pct=target1_pct,
                target2_pct=target2_pct,
                stop_pct=stop_pct,
            )
            if not outcome["hit_target1"]:
                continue
            results.append(
                MissedCandidate(
                    symbol=symbol,
                    bar_time=_iso_et(bar.bar_time),
                    reason=bar.decision_reason,
                    category=_simplify_reason(bar.decision_reason),
                    price=round(bar.close_price, 4),
                    pop1=bool(outcome["hit_target1"]),
                    pop2=bool(outcome["hit_target2"]),
                    stop_first=bool(outcome["stop_first"]),
                )
            )
    results.sort(key=lambda item: (item.symbol, item.bar_time))
    return results


def _summarize_missed(candidates: list[MissedCandidate]) -> dict[str, object]:
    by_reason: Counter[str] = Counter()
    by_symbol: Counter[str] = Counter()
    clean_hits: list[dict[str, object]] = []
    for item in candidates:
        by_reason[item.category] += 1
        by_symbol[item.symbol] += 1
        if item.pop1 and not item.stop_first:
            clean_hits.append(asdict(item))
    clean_hits.sort(key=lambda item: (item["symbol"], item["bar_time"]))
    return {
        "count": len(candidates),
        "by_reason": dict(by_reason.most_common()),
        "by_symbol": dict(by_symbol.most_common()),
        "clean_samples": clean_hits[:15],
    }


def _close_reason_breakdown(closed_today: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    wins: Counter[str] = Counter()
    losses: Counter[str] = Counter()
    flats: Counter[str] = Counter()
    for trade in closed_today:
        reason = str(trade.get("reason", "") or "")
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        if pnl > 0.001:
            wins[reason] += 1
        elif pnl < -0.001:
            losses[reason] += 1
        else:
            flats[reason] += 1
    return {
        "wins": dict(wins.most_common()),
        "losses": dict(losses.most_common()),
        "flat": dict(flats.most_common()),
    }


def _trade_samples(closed_today: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    gave_back: list[dict[str, object]] = []
    cold_losses: list[dict[str, object]] = []
    best_wins: list[dict[str, object]] = []
    for trade in closed_today:
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        peak = float(trade.get("peak_profit_pct", 0.0) or 0.0)
        sample = {
            "ticker": str(trade.get("ticker", "") or ""),
            "entry_time": str(trade.get("entry_time", "") or ""),
            "exit_time": str(trade.get("exit_time", "") or ""),
            "reason": str(trade.get("reason", "") or ""),
            "path": str(trade.get("path", "") or ""),
            "pnl": round(pnl, 2),
            "peak_profit_pct": round(peak, 2),
        }
        if pnl < -0.001 and peak >= 1.0:
            gave_back.append(sample)
        elif pnl < -0.001 and peak < 1.0:
            cold_losses.append(sample)
        elif pnl > 0.001:
            best_wins.append(sample)
    gave_back.sort(key=lambda item: (-item["peak_profit_pct"], item["pnl"]))
    cold_losses.sort(key=lambda item: item["pnl"])
    best_wins.sort(key=lambda item: item["pnl"], reverse=True)
    return {
        "gave_back_losses": gave_back[:12],
        "cold_losses": cold_losses[:12],
        "best_wins": best_wins[:12],
    }


def _format_table(rows: list[list[object]], headers: list[str]) -> str:
    if not rows:
        return "_None_"
    table = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        table.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(table)


def _build_markdown(result: dict[str, object]) -> str:
    trade_summary = result["trade_analysis"]["summary"]
    blocker_rows = result["blocked_overall"][:8]
    scenarios = result["scenarios"]
    scenario_by_name = {str(item.get("scenario", "")): item for item in scenarios}
    ordered_scenario_names = [
        "pause_off",
        "pullback_025",
        "below_vwap_and_ema9",
        "below_ema9",
        "below_vwap_and_ema9_plus_pause",
        "below_ema9_plus_pause",
        "soft_location",
        "pause_only_on_cold_losses_1pct",
    ]
    top_scenarios = [
        scenario_by_name[name]
        for name in ordered_scenario_names
        if name in scenario_by_name and name != "pause_only_on_cold_losses_1pct"
    ]
    pause_row = scenario_by_name.get("pause_only_on_cold_losses_1pct")
    missed = result["missed_summary"]
    trade_samples = result["trade_samples"]
    close_breakdown = result["close_reason_breakdown"]

    lines: list[str] = []
    lines.append(f"# Reclaim Live Day Report - {result['date']}")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(
        f"- Closed trades: `{trade_summary['total']}` | wins `{trade_summary['wins']}` | "
        f"losses `{trade_summary['losses']}` | flat `{trade_summary['flat']}`"
    )
    lines.append(
        f"- Losses that had room first: `{trade_summary['losses_peak_ge_1']}` | "
        f"cold losses: `{trade_summary['losses_peak_eq_0']}`"
    )
    lines.append(
        f"- Blocked bars reviewed: `{sum(int(row['count']) for row in blocker_rows)}` in top blocker groups | "
        f"missed pop1 candidates: `{missed['count']}`"
    )
    lines.append("")

    lines.append("## Top Blockers")
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    row["category"],
                    row["count"],
                    row["pop1_rate"],
                    row["pop2_rate"],
                    row["stop_first_rate"],
                ]
                for row in blocker_rows
            ],
            ["Blocker", "Count", "Pop +1%", "Pop +2%", "Stop First"],
        )
    )
    lines.append("")

    lines.append("## What-If Scenarios")
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    row["scenario"],
                    row["candidate_bars"],
                    row["pop1_rate"],
                    row["pop2_rate"],
                    row["stop_first_rate"],
                ]
                for row in top_scenarios
            ],
            ["Scenario", "Bars", "Pop +1%", "Pop +2%", "Stop First"],
        )
    )
    if pause_row:
        lines.append("")
        lines.append(
            f"- Current live pause model `pause_only_on_cold_losses_1pct`: "
            f"`{pause_row['candidate_bars']}` bars | pop `+1%` `{pause_row['pop1_rate']}` | "
            f"stop-first `{pause_row['stop_first_rate']}`"
        )
    lines.append("")

    lines.append("## Missed Setup Clusters")
    lines.append("")
    lines.append(
        _format_table(
            [[key, value] for key, value in list(missed["by_reason"].items())[:8]],
            ["Reason", "Count"],
        )
    )
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    item["symbol"],
                    item["bar_time"],
                    item["category"],
                    item["price"],
                    item["pop2"],
                    item["reason"],
                ]
                for item in missed["clean_samples"][:8]
            ],
            ["Ticker", "Bar Time", "Blocker", "Price", "Pop +2%", "Reason"],
        )
    )
    lines.append("")

    lines.append("## Trade Management Samples")
    lines.append("")
    lines.append("### Gave-Back Losers")
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    item["ticker"],
                    item["peak_profit_pct"],
                    item["pnl"],
                    item["reason"],
                    item["path"],
                ]
                for item in trade_samples["gave_back_losses"][:8]
            ],
            ["Ticker", "Peak %", "PnL", "Exit Reason", "Path"],
        )
    )
    lines.append("")
    lines.append("### Cold Losses")
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    item["ticker"],
                    item["peak_profit_pct"],
                    item["pnl"],
                    item["reason"],
                    item["path"],
                ]
                for item in trade_samples["cold_losses"][:8]
            ],
            ["Ticker", "Peak %", "PnL", "Exit Reason", "Path"],
        )
    )
    lines.append("")
    lines.append("### Best Wins")
    lines.append("")
    lines.append(
        _format_table(
            [
                [
                    item["ticker"],
                    item["peak_profit_pct"],
                    item["pnl"],
                    item["reason"],
                    item["path"],
                ]
                for item in trade_samples["best_wins"][:8]
            ],
            ["Ticker", "Peak %", "PnL", "Exit Reason", "Path"],
        )
    )
    lines.append("")

    lines.append("## Exit Reason Breakdown")
    lines.append("")
    lines.append("### Losses")
    lines.append("")
    lines.append(
        _format_table([[key, value] for key, value in close_breakdown["losses"].items()], ["Reason", "Count"])
    )
    lines.append("")
    lines.append("### Wins")
    lines.append("")
    lines.append(
        _format_table([[key, value] for key, value in close_breakdown["wins"].items()], ["Reason", "Count"])
    )
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    args = _parse_args()
    if not args.db_url or not args.redis_url:
        settings = get_settings()
        if not args.db_url:
            args.db_url = settings.database_url
        if not args.redis_url:
            args.redis_url = settings.redis_url
    if not args.db_url:
        raise SystemExit("A database URL is required via --db-url or MAI_TAI_DATABASE_URL.")

    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = _et_window(target_day)
    selected_symbols = {symbol.upper() for symbol in (args.symbols or [])}

    engine = create_engine(args.db_url)
    with Session(engine) as session:
        bars = _load_bars(
            session,
            strategy_code=args.strategy,
            start_utc=start_utc,
            end_utc=end_utc,
            symbols=selected_symbols or None,
        )
        intents = _load_trade_intents(
            session,
            strategy_code=args.strategy,
            start_utc=start_utc,
            end_utc=end_utc,
            symbols=selected_symbols or None,
        )

    bars_by_symbol: dict[str, list[BarSample]] = defaultdict(list)
    for bar in bars:
        bars_by_symbol[bar.symbol].append(bar)

    blocker_rows, blocker_by_symbol = _analyze_blockers(
        bars_by_symbol,
        lookahead_bars=args.lookahead_bars,
        target1_pct=args.target1_pct,
        target2_pct=args.target2_pct,
        stop_pct=args.stop_pct,
    )
    scenarios = _analyze_scenarios(
        bars_by_symbol,
        lookahead_bars=args.lookahead_bars,
        target1_pct=args.target1_pct,
        target2_pct=args.target2_pct,
        stop_pct=args.stop_pct,
    )
    closed_today = _load_reclaim_closed_today(args.redis_url, args.strategy)
    closed_today = _enrich_closed_trades_from_intents(closed_today, intents, day_text=args.date)
    trade_analysis = _analyze_closed_trades(closed_today)
    close_reason_breakdown = _close_reason_breakdown(closed_today)
    trade_samples = _trade_samples(closed_today)
    missed_candidates = _collect_missed_candidates(
        bars_by_symbol,
        lookahead_bars=args.lookahead_bars,
        target1_pct=args.target1_pct,
        target2_pct=args.target2_pct,
        stop_pct=args.stop_pct,
    )
    missed_summary = _summarize_missed(missed_candidates)

    cold_pause_windows = _build_cold_pause_windows(closed_today, day_text=args.date)
    cold_pause_counts: Counter[str] = Counter()
    for symbol, symbol_bars in bars_by_symbol.items():
        for index, bar in enumerate(symbol_bars):
            if not _scenario_cold_pause_only(bar, cold_pause_windows):
                continue
            cold_pause_counts["candidate_bars"] += 1
            outcome = _forward_outcome(
                symbol_bars,
                index,
                lookahead_bars=args.lookahead_bars,
                target1_pct=args.target1_pct,
                target2_pct=args.target2_pct,
                stop_pct=args.stop_pct,
            )
            if outcome["hit_target1"]:
                cold_pause_counts["pop1"] += 1
            if outcome["hit_target2"]:
                cold_pause_counts["pop2"] += 1
            if outcome["stop_first"]:
                cold_pause_counts["stop_first"] += 1
    cold_total = int(cold_pause_counts["candidate_bars"])
    scenarios.append(
        {
            "scenario": "pause_only_on_cold_losses_1pct",
            "candidate_bars": cold_total,
            "pop1_rate": round(cold_pause_counts["pop1"] / cold_total, 3) if cold_total else 0.0,
            "pop2_rate": round(cold_pause_counts["pop2"] / cold_total, 3) if cold_total else 0.0,
            "stop_first_rate": round(cold_pause_counts["stop_first"] / cold_total, 3) if cold_total else 0.0,
        }
    )

    result = {
        "date": args.date,
        "strategy": args.strategy,
        "symbols": sorted(bars_by_symbol),
        "lookahead_bars": args.lookahead_bars,
        "target1_pct": args.target1_pct,
        "target2_pct": args.target2_pct,
        "stop_pct": args.stop_pct,
        "blocked_overall": blocker_rows,
        "blocked_by_symbol": blocker_by_symbol,
        "scenarios": scenarios,
        "trade_analysis": trade_analysis,
        "close_reason_breakdown": close_reason_breakdown,
        "trade_samples": trade_samples,
        "missed_summary": missed_summary,
    }

    markdown = _build_markdown(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(markdown)


if __name__ == "__main__":
    main()
