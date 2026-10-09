"""[codex] Offline joined-provider replay; chart data never drives the strategy."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail  # noqa: E402
from project_mai_tai.market_data.massive_atr_seed import MassiveAtrSeedClient  # noqa: E402
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from tests.line_restore_acceptance_factory import make_line_restore_case  # noqa: E402

# Read-only box settings at 08:xx ET 10-09; only LINE is enabled in this offline replay.
LIVE_CONTROLS = {
    "strategy_schwab_1m_v2_gap_hold_enabled": True,
    "strategy_schwab_1m_v2_gap_line_carry_enabled": True,
    "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
    "strategy_schwab_1m_v2_pm_flip_wait_enabled": True,
    "strategy_schwab_1m_v2_pm_rest_reprice_enabled": True,
    "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
    "strategy_schwab_1m_v2_retry_one_enabled": True,
    "strategy_schwab_1m_v2_retry_one_max_retries": 0,
    "strategy_schwab_1m_v2_false_flip_enabled": True,
    "strategy_schwab_1m_v2_slotclear_fresh_flip_enabled": True,
    "strategy_schwab_1m_v2_slotclear_fresh_sell_enabled": True,
    "strategy_schwab_1m_v2_keep_rest_after_buy_enabled": True,
    "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
    "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": True,
    "strategy_schwab_1m_v2_removed_wait_clear_enabled": True,
}


def oracle(rows):
    return compute_atr_trail([Bar(b["t"], *(b[key] for key in ("o", "h", "l", "c", "v")))
                              for b in rows])


def setup(row, current):
    case = make_line_restore_case(symbol=row["symbol"], now_ms=current + 61_000,
                                 settings_overrides=LIVE_CONTROLS)
    case.bot._rest_warmup_done.add(row["symbol"])
    # Empty classification journal is a control, not a recovered historical owner.
    case.strategy.configure_falseflip({}, readable=True)
    case.bot._persist_bar = Mock()
    rest = Mock(get_aggs=Mock(return_value=SimpleNamespace(data=json.dumps(row["preopen"]).encode())))
    case.bot._atr_massive_seed_client = MassiveAtrSeedClient("offline-record", rest_client=rest)
    case.bot.rest_client = SchwabV2RestClient(case.settings, on_chart_bar=None, on_quote=None)
    return case, rest


async def repair(row, case, current):
    with case.clock(current + 61_000):
        bars, proof = case.bot.rest_client._parse_session_history(
            row["symbol"], row["anchor_ms"], current, row["schwab"],
        )
        if proof is None:
            return False
        joined, proof = await case.bot._join_line_preopen(row["symbol"], row["anchor_ms"], bars, proof)
        ledger = case.bot._line_sessions[row["symbol"]]
        if not case.bot._accept_line_source(row["symbol"], ledger.epoch, joined, proof):
            return False
        return await case.rebuild()


async def replay(row):
    symbol, boundary = row["symbol"], row["boundary_ms"]
    current = boundary + 9 * 60_000
    case, provider = setup(row, current)
    rebuilt = await repair(row, case, current)
    actual = case.snapshot()
    chart = oracle(row["chart"].get("results", []))
    expected = next((r for r in chart if r["ts"] == current), None)
    matched = bool(rebuilt and expected and actual["state"] == expected["state"]
                   and round(actual["trail"], 4) == expected["trail"]
                   and actual["age"] == expected["state_age"])
    source = [b for b in row["schwab"]["candles"] if boundary <= b["datetime"] <= row["cutoff_ms"]]
    source.sort(key=lambda b: b["datetime"])
    joined_rows = row["preopen"].get("results", []) + [
        {"t": b["datetime"], **{k: b[name] for k, name in
         (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}}
        for b in source
    ]
    joined_flips = [(r["et"], r["flip"]) for r in oracle(joined_rows)
                    if r["ts"] >= boundary and r["flip"]]
    chart_flips = [(r["et"], r["flip"]) for r in chart if r["ts"] >= boundary and r["flip"]]
    control = oracle([b for b in joined_rows if b["t"] >= boundary])
    baseline = next((r for r in control if r["ts"] == current), None)
    already_agreed = bool(baseline and expected and baseline["state"] == expected["state"]
                          and baseline["trail"] == expected["trail"])
    waiting_at = None
    runtime, runtime_provider = setup(row, source[0]["datetime"] if source else current)
    runtime_flips = []
    if source:
        first = source[0]["datetime"]
        await repair(row, runtime, first)
        published = runtime.bot._line_published.get(symbol)
        signal = dict(published.snapshot.signal) if published is not None else {}
        if signal.get("flip"):
            runtime_flips.append((first, signal["flip"]))
        for candle in source[1:]:
            stamp = candle["datetime"]
            bar = {"symbol": symbol, "bar_time": datetime.fromtimestamp(stamp / 1000, UTC),
                **{name: candle[key] for name, key in (("open_price", "open"),
                    ("high_price", "high"), ("low_price", "low"), ("close_price", "close"),
                    ("volume", "volume"))}}
            with runtime.clock(stamp + 61_000):
                await runtime.feed_bar(bar, phase="live")
                if symbol in runtime.bot._line_source_pending:
                    runtime.bot.rest_client.fetch_session_history = lambda sym, anchor, end: (
                        runtime.bot.rest_client._parse_session_history(sym, anchor, end, row["schwab"])
                    )
                    await runtime.bot._line_source_events_pass()
                await runtime.rebuild()
                published = runtime.bot._line_published.get(symbol)
                signal = dict(published.snapshot.signal) if published is not None else {}
                if signal.get("flip"):
                    runtime_flips.append((stamp, signal["flip"]))
                if waiting_at is None and runtime.strategy.watchlist_state(symbol).resting_active:
                    waiting_at = stamp
    first_flip = joined_flips[0] if joined_flips else None
    chart_first = chart_flips[0] if chart_flips else None
    first_match = first_flip == chart_first
    expected_runtime = [(r["ts"], r["flip"]) for r in chart if r["ts"] >= boundary and r["flip"]]
    runtime_match = runtime_flips == expected_runtime
    result = ("PASS" if matched and first_match and runtime_match
              else "UNMEASURED" if expected is None else "FAIL")
    line_result = result
    if row["day"] == "2026-10-09" and symbol == "MI" and waiting_at is None:
        result = "FAIL"
    return {"day": row["day"], "symbol": symbol, "watchlist_open": row["watchlist_open"],
            "added_at": row["added_at"], "source_read_at": row["as_of"],
            "result": result, "line_result": line_result,
            "state": actual["state"], "level": actual["trail"],
            "age": actual["age"], "chart_state": expected["state"] if expected else None,
            "chart_level": expected["trail"] if expected else None,
            "chart_age": expected["state_age"] if expected else None,
            "first_flip_until_0800": first_flip, "chart_first_flip_until_0800": chart_first,
            "waiting_buy_bar_ms": waiting_at,
            "rebuild_buys": actual["buy_count"], "massive_seed_requests": provider.get_aggs.call_count,
            "runtime_seed_requests": runtime_provider.get_aggs.call_count,
            "runtime_flips_ms": runtime_flips,
            "runtime_chart_flip_parity": runtime_match,
            "waiting_buy_context": "isolated flat control; no historical owner/budget/book receipt or quotes replayed",
            "historical_rest_order_parity": "UNMEASURED",
            "already_agreed": already_agreed,
            "baseline_flips": [(r["et"], r["flip"]) for r in control if r["flip"]],
            "parity_flips": not already_agreed or joined_flips == [(r["et"], r["flip"])
                                                                   for r in control if r["flip"]]}


async def main():
    fixture = json.loads((ROOT / "tests/fixtures/line_repair_preopen_20261005_09.json").read_text())
    failures = 0
    for row in fixture["rows"]:
        if row.get("error"):
            result = {"day": row["day"], "symbol": row["symbol"], "result": "UNREADABLE",
                      "error": row["error"]}
        else:
            result = await replay(row)
        failures += result["result"] != "PASS" or not result.get("parity_flips", False)
        print(json.dumps(result), flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
