"""PAGE1020 controlled synthetic fills, NOT today's vendor packet or SQL replay.

All execution is deferred to the integrator's authorized after-16:00 slot.
"""

from copy import deepcopy

from project_mai_tai.services import control_plane as control
from tests.unit.test_atrpnl1 import fill, page_data


def synthetic_fill(symbol, side, qty, price, coid, minute, account="live:orb"):
    return {**fill(side, qty, price, coid, f"2026-10-20T14:{minute:02d}:00+00:00", account),
            "symbol": symbol}


def synthetic_seven_fills():
    return [
        synthetic_fill("WORX", "buy", 10, 2, "worx-a", 0),
        synthetic_fill("WORX", "sell", 10, "2.4", "worx-a-ocoexit-target", 1),
        synthetic_fill("WORX", "buy", 3, 4, "worx-b", 2),
        synthetic_fill("WORX", "sell", 3, 6, "worx-b-ocoexit-target", 3),
        synthetic_fill("SAIQ", "buy", 4, 5, "saiq-old", 4),
        synthetic_fill("SAIQ", "buy", 6, 4, "saiq-new", 6),
        synthetic_fill("SAIQ", "sell", 6, "4.5", "saiq-software-close", 7),
    ]


def test_synthetic_seven_fills_keep_two_worx_rows_and_one_unknown_saiq_group():
    rows = synthetic_seven_fills()
    original = deepcopy(rows)
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert pnl == 10 and [row["symbol"] for row in closed] == ["WORX", "WORX"]
    assert len(unresolved) == 1
    assert unresolved[0]["symbol"] == "SAIQ" and unresolved[0]["quantity"] == 6
    assert unresolved[0]["broker_account_name"] == "live:orb"
    assert unresolved[0]["exit_events"][0]["client_order_id"] == "saiq-software-close"
    assert rows == original


def test_synthetic_repaired_external_lot_restores_exact_attribution_not_sql_execution():
    rows = synthetic_seven_fills()
    # Explicit synthetic evidence of the old lot's close BEFORE the new entry.
    rows.append(synthetic_fill("SAIQ", "sell", 4, "4.8", "saiq-old-ocoexit-external", 5))
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert unresolved == [] and len(closed) == 4
    assert pnl == 12.2
    assert {row["entry_client_order_id"] for row in closed} == {
        "worx-a", "worx-b", "saiq-old", "saiq-new"}
    assert next(row for row in closed if row["entry_client_order_id"] == "saiq-new")["quantity"] == 6


def test_partial_html_api_keep_attributable_pnl_rows_and_unresolved_exit_identity(monkeypatch):
    data = page_data(synthetic_seven_fills())
    def forbidden_fifo(**kwargs):
        raise AssertionError("ATR partial attribution must not fall back to FIFO")
    monkeypatch.setattr(control, "collect_completed_trade_cycles", forbidden_fifo)
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] == 10 and payload["daily_pnl_partial"] is True
    assert len(payload["closed_today"]) == 2 and len(payload["unattributed_exits"]) == 1
    html = control._render_bot_detail_page(data, "schwab_1m_v2")
    assert "$+10.00 (partial: 1 unattributed)" in html
    assert "6 sh exit not attributable" in html and "SAIQ" in html and "live:orb" in html
    assert "OCO bracket (target/stop)" in html
    assert "entry-episode fill coverage is incomplete" not in html and "$-999.00" not in html
    assert html.count("exit not attributable") == 1


def test_unknown_bucket_does_not_hide_same_symbol_other_account():
    rows = synthetic_seven_fills() + [
        synthetic_fill("SAIQ", "buy", 2, 3, "other", 8, "live:schwab_1m_v2"),
        synthetic_fill("SAIQ", "sell", 2, 4, "other-ocoexit-target", 9, "live:schwab_1m_v2"),
    ]
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert pnl == 12 and len(closed) == 3 and len(unresolved) == 1
    assert closed[-1]["broker_account_name"] == "live:schwab_1m_v2"


def test_ambiguous_exit_never_leaves_inventory_available_for_later_guess():
    rows = synthetic_seven_fills() + [
        synthetic_fill("SAIQ", "sell", 4, 6, "saiq-old-ocoexit-later", 8),
        synthetic_fill("SAIQ", "sell", 6, 7, "saiq-next-software", 9),
    ]
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert pnl == 10 and len(closed) == 2
    assert len(unresolved) == 1 and unresolved[0]["quantity"] == 16
    assert len(unresolved[0]["exit_events"]) == 3


def test_new_exact_oco_episode_remains_attributable_after_other_lots_ambiguous():
    rows = synthetic_seven_fills() + [
        synthetic_fill("SAIQ", "buy", 2, 3, "independent", 8),
        synthetic_fill("SAIQ", "sell", 2, 4, "independent-ocoexit-target", 9),
    ]
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert pnl == 12 and len(closed) == 3
    assert closed[-1]["entry_client_order_id"] == "independent"
    assert unresolved[0]["quantity"] == 6


def test_overfilled_named_child_does_not_consume_or_taint_another_named_lot():
    rows = [synthetic_fill("TEST", "buy", 2, 3, "a", 0),
            synthetic_fill("TEST", "buy", 5, 4, "b", 1),
            synthetic_fill("TEST", "sell", 3, 5, "a-ocoexit-overfill", 2),
            synthetic_fill("TEST", "sell", 5, 5, "b-ocoexit-target", 3)]
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert pnl == 5 and len(closed) == 1 and closed[0]["entry_client_order_id"] == "b"
    assert unresolved[0]["quantity"] == 3


def test_previously_proven_scaleout_pnl_survives_later_unattributed_exit():
    rows = [synthetic_fill("TEST", "buy", 10, 3, "a", 0),
            synthetic_fill("TEST", "sell", 2, 4, "a-ocoexit-scaleout", 1),
            synthetic_fill("TEST", "buy", 5, 4, "b", 2),
            synthetic_fill("TEST", "sell", 5, 5, "unlinked", 3)]
    unresolved = []
    closed, pnl = control._atr_episode_fill_ledger(rows, unattributed_exits=unresolved)
    assert closed == [] and pnl == 2 and unresolved[0]["quantity"] == 5
    data = page_data(rows)
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] == 2 and payload["daily_pnl_partial"] is True


def test_multiple_unresolved_groups_count_groups_not_fill_events():
    rows = synthetic_seven_fills() + [
        synthetic_fill("EXYN", "sell", 2, 3, "missing-parent-ocoexit-target", 8),
        synthetic_fill("EXYN", "sell", 1, 3, "missing-parent-ocoexit-second", 9),
    ]
    data = page_data(rows)
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert len(payload["unattributed_exits"]) == 2 and payload["daily_pnl"] == 10
    assert "partial: 2 unattributed" in control._render_bot_detail_page(data, "schwab_1m_v2")


def test_truncated_coverage_still_does_not_claim_partial_known_inventory():
    data = page_data([synthetic_fill("TEST", "buy", 1, 3, str(n), 0) for n in range(1000)])
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] is None and payload["closed_today"] == []
    assert payload["closed_today_source"] == "UNKNOWN_FILL_EPISODE_COVERAGE"
