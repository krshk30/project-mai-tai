"""Run one in-memory guard mutation without changing the checkout."""
from __future__ import annotations

import inspect
import sys
import textwrap

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService


MUTATIONS = {
    "S1": (SchwabV2Strategy, "_slotclear_live_close_after_watch",
           "and bar_ms + 60000 > max(watch_ms, self._boot_ms)", "and True",
           "test_lpcn_recorded_bar_with_later_watch_boundary_cannot_become_a_fresh_buy"),
    "S2": (SchwabV2Strategy, "_slotclear_fresh_buy", 'state.flip_owner_phase != "idle"', "False",
           "test_seed_cap_is_not_permission_to_release_real_or_unknown_ownership[unknown]"),
    "S3": (SchwabV2Strategy, "_slotclear_fresh_buy", "or state.retry_one_closes_in_segment != 0", "or False",
           "test_seed_cap_is_not_permission_to_release_real_or_unknown_ownership[closed]"),
    "S4": (SchwabV2Strategy, "_cw_v2_quote", "or ask < trig or ask > cap", "or ask < trig",
           "test_lpcn_fresh_exception_never_chases_above_half_percent"),
    "S5": (SchwabV2Strategy, "_slotclear_fresh_buy",
           'or signal.get("observation_phase", "live") != "live"', "or False",
           "test_lpcn_historical_delivery_never_turns_seed_flip_into_a_live_first_entry"),
    "S6": (SchwabV2Strategy, "_remove_waiting_buy", "state.slotclear_fresh_buy_bar_ms = 0", "pass",
           "test_fresh_buy_permission_is_revoked_on_scanner_removal"),
    "S7": (SchwabV2BotService, "_cap_reconstructed_segment",
           "st.atr_short_flip_bar_ts <= watch_start", "False",
           "test_recorded_ftft_stale_sell_stays_capped_with_slotclear_flag_on"),
}


def main():
    name = sys.argv[1]
    cls, method, old, new, test = MUTATIONS[name]
    original = getattr(cls, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source.replace(old, new),
                 f"<slotclear1-mutation-{name}>", "exec"), namespace)
    setattr(cls, method, namespace[method])
    result = pytest.main(["-p", "no:cacheprovider", "-q",
                          "tests/unit/test_slotclear1_fresh_flip.py::" + test])
    print(f"MUTATION {name} pytest_rc={result} {'RED' if result == 1 else 'NOT_RED'}")
    return 0 if result == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
