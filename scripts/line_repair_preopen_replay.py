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
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook  # noqa: E402
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
REST_RECEIPTS = {(r["day"], r["symbol"]): r for r in json.loads(
    (ROOT / "tests/fixtures/line_repair_watch_first_rests_20261005_09.json").read_text(),
)["rows"]}


def oracle(rows):
    return compute_atr_trail([Bar(b["t"], *(b[key] for key in ("o", "h", "l", "c", "v")))
                              for b in rows])


def setup(row, current, *, admission_control=False):
    case = make_line_restore_case(symbol=row["symbol"], now_ms=current + 61_000,
                                 settings_overrides=LIVE_CONTROLS)
    case.bot._rest_warmup_done.add(row["symbol"])
    # Empty classification journal is a control, not a recovered historical owner.
    case.strategy.configure_falseflip({}, readable=True)
    if admission_control:
        case.strategy.configure_fanout_identity_persistence(lambda *_: None)
        case.strategy.configure_flip_entry_ownership(lambda *_a, **_k: None,
            retry_budget_persist=lambda *_: None)
        case.strategy.configure_removed_wait(lambda *_: None, restored={}, readable=True)
        case.strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
        feed = case.feed_bar
        async def fresh_flat_feed(*args, **kwargs):
            case.strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
            return await feed(*args, **kwargs)
        case.feed_bar = fresh_flat_feed
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


async def replay(row, *, admission_control=True):
    symbol, boundary = row["symbol"], row["boundary_ms"]
    watched = max(boundary, int(datetime.fromisoformat(row["added_at"]).timestamp() * 1000))
    source = [b for b in row["schwab"]["candles"] if boundary <= b["datetime"] <= row["cutoff_ms"]]
    source.sort(key=lambda b: b["datetime"])
    first_watched = next((b["datetime"] for b in source if b["datetime"] + 60_000 > watched), None)
    current = boundary + 9 * 60_000
    if not any(b["datetime"] == current for b in source):
        current = first_watched or current
    case, provider = setup(row, current, admission_control=admission_control)
    rebuilt = await repair(row, case, current)
    actual = case.snapshot()
    chart = oracle(row["chart"].get("results", []))
    expected = next((r for r in chart if r["ts"] == current), None)
    matched = bool(rebuilt and expected and actual["state"] == expected["state"]
                   and round(actual["trail"], 4) == expected["trail"]
                   and actual["age"] == expected["state_age"])
    unseeded = bool(expected and expected["state"] is None and not rebuilt
                    and actual["state"] is None and not actual["entry_allowed"]
                    and actual["buy_count"] == 0)
    matched = matched or unseeded
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
    waiting_trigger = None
    watched_source = [b for b in source if first_watched is not None and b["datetime"] >= first_watched]
    runtime, runtime_provider = setup(row, first_watched or current, admission_control=admission_control)
    runtime_flips = []
    if watched_source:
        first = watched_source[0]["datetime"]
        await repair(row, runtime, first)
        published = runtime.bot._line_published.get(symbol)
        signal = dict(published.snapshot.signal) if published is not None else {}
        if signal.get("flip"):
            runtime_flips.append((first, signal["flip"]))
        previous_stamp = first
        for candle in watched_source[1:]:
            stamp = candle["datetime"]
            if stamp - previous_stamp > 90_000:
                runtime.hold(detected_at_ms=stamp + 61_000,
                             last_bar_age_s=(stamp + 61_000 - previous_stamp) / 1000,
                             last_print_age_s=0)
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
                    waiting_trigger = runtime.strategy.watchlist_state(symbol).resting_trigger
            previous_stamp = stamp
    first_flip = joined_flips[0] if joined_flips else None
    chart_first = chart_flips[0] if chart_flips else None
    first_match = first_flip == chart_first
    expected_runtime = [(r["ts"], r["flip"]) for r in chart
                        if first_watched is not None and r["ts"] > first_watched and r["flip"]]
    runtime_flips = [(t, side) for t, side in runtime_flips if t > (first_watched or current)]
    runtime_match = runtime_flips == expected_runtime
    result = ("PASS" if matched and first_match and runtime_match
              else "UNMEASURED" if expected is None else "FAIL")
    line_result = result
    if row["day"] == "2026-10-09" and symbol == "MI" and waiting_at is None:
        result = "FAIL"
    return {"day": row["day"], "symbol": symbol, "watchlist_open": row["watchlist_open"],
            "added_at": row["added_at"], "source_read_at": row["as_of"],
            "reading_bar_ms": current, "first_watched_bar_ms": first_watched,
            "reading_status": "HELD_UNSEEDED" if unseeded else "RESTORED" if rebuilt else "HELD",
            "result": result, "line_result": line_result,
            "state": actual["state"], "level": actual["trail"],
            "age": actual["age"], "chart_state": expected["state"] if expected else None,
            "chart_level": expected["trail"] if expected else None,
            "chart_age": expected["state_age"] if expected else None,
            "first_flip_until_0800": first_flip, "chart_first_flip_until_0800": chart_first,
            "waiting_buy_bar_ms": waiting_at,
            "waiting_buy_trigger": waiting_trigger,
            "rebuild_buys": actual["buy_count"], "massive_seed_requests": provider.get_aggs.call_count,
            "reading_entry_allowed": actual["entry_allowed"],
            "runtime_intents": [{"symbol": draft.symbol, "side": draft.side,
                "intent_type": draft.intent_type, "quantity": str(draft.quantity),
                "reason": draft.reason, "metadata": draft.metadata}
                for draft in runtime.snapshot()["intents"]],
            "runtime_seed_requests": runtime_provider.get_aggs.call_count,
            "runtime_flips_ms": runtime_flips,
            "runtime_chart_flip_parity": runtime_match,
            "waiting_buy_context": "readable unused-owner/fresh-flat control; historical quotes/receipts not certified",
            "historical_rest_order_parity": "UNMEASURED",
            "historical_first_rest_receipt": REST_RECEIPTS[(row["day"], symbol)]["first_rest"],
            "historical_watch_intervals_ms": REST_RECEIPTS[(row["day"], symbol)]["watch_intervals_ms"],
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
