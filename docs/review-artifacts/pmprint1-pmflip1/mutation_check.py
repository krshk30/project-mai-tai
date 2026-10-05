"""Isolated, in-memory method mutations; no source or production writes."""
import inspect
import sys
import textwrap

import pytest

from project_mai_tai.services import schwab_1m_v2_bot
from project_mai_tai.strategy_core import entry_gate, schwab_1m_v2

mutations = {
    "lower_check": (schwab_1m_v2.SchwabV2Strategy, "_eh_resting_cross_check", "elif ask < trigger:", "elif False:"),
    "rest_freshness": (schwab_1m_v2.SchwabV2Strategy, "_resting_ask_evidence", 'if age_ms > max_age_ms:', "if False:"),
    "stream_ask_lost": (schwab_1m_v2.SchwabV2Strategy, "on_stream_trade", 'ask_price=ask if self._pm_rest_feature(state, "pm_print_ask_confirm") else 0.0,', "ask_price=0.0,"),
    "flip_blinds": (schwab_1m_v2.SchwabV2Strategy, "_cw_v2_resting_track", 'if self._pm_rest_feature(state, "pm_flip_wait"):', "if False:"),
    "wait_takedown_removed": (schwab_1m_v2.SchwabV2Strategy, "_cw_v2_resting_track", "if state.pm_resting_flip_seen_ms and not state.resting_flip_ms:", "if False:"),
    "cross_latch_removed": (schwab_1m_v2.SchwabV2Strategy, "_eh_resting_cross_check", "state.resting_flip_ms = self._now_ms()", "state.resting_flip_ms = 0"),
    "webull_uses_schwab_amount": (schwab_1m_v2.SchwabV2Strategy, "_build_webull_fanout_draft", 'leg="webull"', 'leg="schwab"'),
    "routing_ask_lost": (entry_gate, "route_extended_hours", 'confirming_ask = draft.metadata.get("pm_confirming_ask") if side == "buy" else None', "confirming_ask = None"),
    "emit_stale_ask": (schwab_1m_v2_bot.SchwabV2BotService, "_apply_extended_hours_routing", 'if not 0 <= age <= max_age or extended_hours_session(now) != "AM":', "if False:"),
    "silent_webull_drop": (schwab_1m_v2.SchwabV2Strategy, "_log_pm_size_refused", "logger.error(", "logger.info("),
}
name = sys.argv[1]
owner, method, before, after = mutations[name]
original = getattr(owner, method)
source = textwrap.dedent(inspect.getsource(original))
assert source.count(before) == 1, (name, source.count(before))
namespace = dict(original.__globals__)
exec(compile(source.replace(before, after), original.__code__.co_filename, "exec"), namespace)
setattr(owner, method, namespace[method])
rc = pytest.main(["tests/unit/test_pmprint1_pmflip1.py", "-q"])
print(f"MUTATION {name} pytest_rc={rc} killed={int(rc == 1)}")
raise SystemExit(0 if rc == 1 else 1)
