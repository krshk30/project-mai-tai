"""Negative control only: remove restored-ticket veto in this child process."""
import pytest
from project_mai_tai.strategy_core.schwab_1m_v2 import Schwab1mV2Strategy

Schwab1mV2Strategy._rpg_entry_owned = lambda self, state, **kwargs: False
raise SystemExit(pytest.main([
    "docs/review-artifacts/slotclear1/test_both_paths.py::"
    "test_present_durable_sxtc_tickets_veto_entry_after_recorded_sell_even_handoff_off",
    "-q", "--tb=short",
]))
