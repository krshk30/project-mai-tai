"""Retain the original broad RPG regression scope, then add this card and PMREST."""
import subprocess
import sys

names = """
backtest_replay entry_gate expected_flags_check nfq1_mirror_fresh_price
oms_webull_mirror_deferred_resubmit rpg1_buy_readback rpg1_nfq1_composition
rpg1_pending_first_quote rpg1_piece3_14 rpg1_reprice_handoff rpg1_review_guards
rpg1_runtime rpg1_runtime_nfq schwab_1m_v2_armed_segment_safety
schwab_1m_v2_atr_arm_adjacency schwab_1m_v2_atr_bar_gap schwab_1m_v2_atr_flip
schwab_1m_v2_bot schwab_1m_v2_confirmed_window schwab_1m_v2_cw_v2
schwab_1m_v2_eh_resting_entry schwab_1m_v2_eh_stream_cross schwab_1m_v2_fanout_entry_n
schwab_1m_v2_gap_hold schwab_1m_v2_gap_hold_prints schwab_1m_v2_hold_confirm
schwab_1m_v2_liquidity_floor_coverage schwab_1m_v2_loop_resilience
schwab_1m_v2_reactive_eh_guard schwab_1m_v2_reportable_state schwab_1m_v2_resting_entry
schwab_1m_v2_resting_orphan schwab_1m_v2_resting_trigger_offset
schwab_1m_v2_retry_one_recovery schwab_1m_v2_session_time_roll schwab_1m_v2_watch_start
v2_dual_broker_fanout v2_entry_sizing pmrest1 rpgstuck1
""".split()
raise SystemExit(subprocess.run([sys.executable, "-m", "pytest", "-q",
    *[f"tests/unit/test_{name}.py" for name in names],
    "tests/composition/test_rpg1_nfq1.py", *sys.argv[1:]]).returncode)
