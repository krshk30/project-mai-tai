"""Same deterministic strategy inputs on frozen main and head; stdout-only proof."""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import argparse
import json
from uuid import UUID

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture")
    args = parser.parse_args()
    with open(args.fixture) as handle:
        rows = json.load(handle)["queries"]["orders"]
    results = []
    module.uuid4 = lambda: UUID(int=7)  # Identity control only; no replayed client order submitted.
    for row in rows:
        md = row["payload"]
        amount = Decimal(md.get("entry_notional_target_usd", "0"))
        settings = Settings(_env_file=None,
            strategy_schwab_1m_v2_resting_buy_round_up_enabled=False,
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct=0.5,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_webull_account_name="live:orb",
            strategy_schwab_1m_v2_entry_notional_usd=amount if row["account"] == "live:schwab_1m_v2" else 0,
            strategy_schwab_1m_v2_webull_entry_notional_usd=amount if row["account"] == "live:orb" else 0,
            oms_v2_emit_native_oco_bracket_enabled=True)
        strategy = module.SchwabV2Strategy(settings)
        strategy._now_ms = lambda: int(datetime.fromisoformat(row["submitted_at"]).timestamp() * 1000)
        strategy._resting_session_is_eh = lambda now=None: row["order_type"].lower() == "limit"
        strategy._eh_resting_enabled = True
        state = strategy.watchlist_state(row["symbol"])
        state.fanout_segment_id = int(md.get("fanout_segment_id") or strategy._now_ms())
        strategy._queue_resting_place(state, float(md["cw_flip_level"]), slot=md.get("cw_entry_slot", "first"))
        results.append({"id": row["id"], "primary": [asdict(x) for x in strategy.drain_pending_intents()],
                        "webull": [asdict(x) for x in strategy.drain_webull_direct_intents()]})
    print(json.dumps(results, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
