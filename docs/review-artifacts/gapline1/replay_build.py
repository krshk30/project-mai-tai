"""Carry-path replay over retained bars; no historical delivery/fill claims."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

ROOT = Path(__file__).resolve().parents[3]
ET = ZoneInfo("America/New_York")


def bars_for(fixture, day, symbol):
    return [OHLCVBar(int(datetime.fromisoformat(row[0]).timestamp() * 1000),
                     *map(float, row[1:5]), int(row[5]))
            for row in fixture["sessions"].get(day + ":" + symbol, [])]


def main():
    fixture = json.loads((ROOT / "tests/fixtures/gapline1_recorded_holds.json").read_text())
    own = json.loads((Path(__file__).parent / "OWN_REPLAY_2026-10-07.json").read_text())
    rows = []
    for case in own["table"]:
        if case["hold"][:10] < "2026-09-28" or not case.get("replay"):
            continue
        detected = int(datetime.fromisoformat(case["hold"]).timestamp() * 1000)
        symbol = case["symbol"]
        day = datetime.fromtimestamp(detected / 1000, UTC).astimezone(ET).date().isoformat()
        bars = bars_for(fixture, day, symbol)
        engine = SchwabV2Strategy(Settings(
            strategy_schwab_1m_v2_gap_hold_enabled=True,
            strategy_schwab_1m_v2_gap_line_carry_enabled=True,
            strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        ))
        state = engine.watchlist_state(symbol)
        engine._now_ms = lambda: detected
        engine._line_readiness = lambda _: False
        # Eventual stored-prefix control, NOT evidence these backfills had
        # arrived before the historical detection/decision timestamp.
        for bar in bars:
            if bar.timestamp_ms >= detected // 60_000 * 60_000:
                break
            engine._update_atr_state(state, bar, state_only=True)
            state.bars.append(bar)
        engine.begin_gap_hold(symbol, detected_at_ms=detected,
                              last_bar_age_s=91, last_print_age_s=1)
        flips = []
        evaluated = None
        for bar in bars:
            if bar.timestamp_ms < detected // 60_000 * 60_000:
                continue
            before = state.atr_state
            engine._now_ms = lambda bar=bar: bar.timestamp_ms + 61_000
            engine.on_observed_bar(symbol, bar, observation_phase="live")
            if before == "short" and state.atr_state == "long":
                flips.append(bar.timestamp_ms)
            if bar.timestamp_ms == case["evaluated_bar_ms"]:
                evaluated = {"state": state.atr_state, "trail": state.atr_trail,
                             "age": state.atr_state_age, "held": state.gap_hold_active}
        first = case.get("first_buy")
        rows.append({
            "symbol": symbol, "hold": case["hold"], "source": case["hold_source"],
            "recorded_resume": bool(case["resume"]), "carry_at_reading": evaluated,
            "oracle_state": case["replay"]["oracle_state"],
            "same_colour": bool(evaluated and evaluated["state"] == case["replay"]["oracle_state"]),
            "oracle_first_buy_ms": first["ts"] if first else None,
            "first_buy_mathematically_visible": first["ts"] in flips if first else None,
            "bot_saw_original_first_buy": case["bot_saw_first_buy"],
            "open_drafts": sum(d.intent_type == "open" for d in
                               engine._pending_intents + engine._pending_webull_direct_intents
                               + engine._pending_webull_fanout_intents),
        })
    print(json.dumps({
        "basis": "eventual stored bars; LINE incomplete admission controlled FALSE; not repair latency or fill replay",
        "raw_sha256": fixture["raw_sha256"], "as_of_utc": fixture["as_of_utc"],
        "retained_initial_holds": 96, "retained_recovery_holds": 37,
        "readings": rows,
        "entry_controls": own["entry_controls"],
        "entry_control_basis": "independent stored-bar math from Step 0; actual order emission UNMEASURED",
    }, indent=2))


if __name__ == "__main__":
    main()
