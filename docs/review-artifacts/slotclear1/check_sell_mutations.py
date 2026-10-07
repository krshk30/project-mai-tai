"""In-memory SELL guard controls; no checkout source is rewritten."""
import inspect
import subprocess
import sys
import textwrap

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


TEST = "tests/unit/test_slotclear1_fresh_sell.py::"
POSITIVE = "test_recorded_sxtc_sell_releases_reconstruction_then_first_rest_at_age_three_once[True-True]"
MUTATIONS = {
    "F1": (SchwabV2Strategy, "_cw_v2_track", "self._slotclear_fresh_sell(state, atr_signal)", "pass", POSITIVE),
    "F2": (SchwabV2Strategy, "_slotclear_live_sell_close_after_watch",
           "and bar_ms + 60000 > max(watch_ms, self._boot_ms)", "and True",
           "test_sell_requires_live_completed_bar_after_watch_and_boot[cold_boot]"),
    "F3": (SchwabV2Strategy, "_slotclear_live_sell_close_after_watch",
           'and self._bar_observation_phase == "live"', "and True",
           "test_sell_requires_live_completed_bar_after_watch_and_boot[replay_handler]"),
    "F4": (SchwabV2Strategy, "_slotclear_fresh_sell", "or not self._flip_owner_evidence_fresh(state)", "or False",
           "test_fresh_sell_never_releases_held_working_unknown_or_unready_claims[stale_book]"),
    "F5": (SchwabV2Strategy, "_slotclear_fresh_sell", "or state.position_qty_held", "or False",
           "test_fresh_sell_never_releases_held_working_unknown_or_unready_claims[held_qty]"),
    "F6": (SchwabV2BotService, "_consume_reconstructed_slots",
           "st.slotclear_reconstructed_watch_start_ms = watch_start", "pass", POSITIVE),
    "F7": (SchwabV2Strategy, "_cw_v2_resting_track", "if state_age < self._resting_min_short_bars:", "if False:", POSITIVE),
    "F8": (SchwabV2Strategy, "_cw_v2_track", "or bar_ms <= state.slotclear_last_fresh_sell_bar_ms", "or False",
           "test_delayed_duplicate_recorded_sell_cannot_retire_the_new_first_rest_owner[300000]"),
    "F9": (SchwabV2Strategy, "_slotclear_fresh_sell", "or state.resting_active", "or False",
           "test_keeprest_sell_cancels_existing_order_without_granting_another_slot"),
    "F10": (SchwabV2Strategy, "_slotclear_fresh_sell", "or self.gap_hold_active(state.symbol)", "or False",
            "test_fresh_sell_never_releases_held_working_unknown_or_unready_claims[gap]"),
}


name = sys.argv[1]
if name == "F11":
    # Restore the actual d68 SELL dispatcher, not an invented approximate legacy path.
    source = subprocess.check_output(["git", "show", "d68d25e0bf8cca33d6c22da39fa550b5977b5da3:src/project_mai_tai/strategy_core/schwab_1m_v2.py"], text=True)
    start = source.index("    def _cw_v2_track(")
    end = source.index("    def _cw_state_probe(", start)
    original = SchwabV2Strategy._cw_v2_track
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + textwrap.dedent(source[start:end]),
                 "<slotclear-original-d68-dispatch>", "exec"), namespace)
    SchwabV2Strategy._cw_v2_track = namespace["_cw_v2_track"]
    test = POSITIVE
else:
    cls, method, old, new, test = MUTATIONS[name]
    original = getattr(cls, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source.replace(old, new),
                 f"<slotclear-sell-mutation-{name}>", "exec"), namespace)
    setattr(cls, method, staticmethod(namespace[method]) if cls is SchwabV2BotService else namespace[method])
result = pytest.main(["-p", "no:cacheprovider", "-q", TEST + test])
print(f"MUTATION {name} pytest_rc={result} {'RED' if result == 1 else 'NOT_RED'}")
raise SystemExit(0 if result == 1 else 1)
