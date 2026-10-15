from copy import deepcopy

import pytest

from project_mai_tai.services import control_plane as control
from project_mai_tai.services.control_plane import _atr_episode_fill_ledger
from project_mai_tai.services.control_plane import ControlPlaneRepository
from project_mai_tai.settings import Settings


def fill(side, quantity, price, coid, stamp="2026-10-14T13:53:20+00:00", account="live:v2"):
    return {"strategy_code": "schwab_1m_v2", "broker_account_name": account,
            "symbol": "TEST", "side": side, "quantity": str(quantity), "price": str(price),
            "client_order_id": coid, "filled_at": stamp}


def test_oco_exit_tie_counts_without_any_successful_duplicate_close():
    rows = [fill("sell", 150, "4.19", "entry-ocoexit-native"),
            fill("buy", 150, "3.99", "entry")]
    closed, pnl = _atr_episode_fill_ledger(rows)
    assert len(closed) == 1 and pnl == 30
    assert closed[0]["entry_client_order_id"] == "entry"


def test_distinct_entries_never_lend_inventory_to_an_oco_child():
    rows = [fill("buy", 10, 4, "old"), fill("buy", 20, 3, "new"),
            fill("sell", 20, "3.5", "new-ocoexit-native")]
    closed, pnl = _atr_episode_fill_ledger(rows)
    assert pnl == 10 and len(closed) == 1
    assert closed[0]["entry_client_order_id"] == "new"
    assert closed[0]["entry_price"] == 3


def test_unlinked_exit_with_multiple_live_episodes_is_unknown_not_fifo():
    assert _atr_episode_fill_ledger([
        fill("buy", 10, 4, "old"), fill("buy", 20, 3, "new"),
        fill("sell", 10, "3.5", "close"),
    ]) is None


def test_partial_fills_use_actual_entry_and_exit_vwap():
    rows = [fill("buy", 10, 3, "entry"), fill("buy", 10, 4, "entry"),
            fill("sell", 5, 4, "entry-ocoexit-a"), fill("sell", 15, 5, "entry-ocoexit-b")]
    closed, pnl = _atr_episode_fill_ledger(rows)
    assert pnl == 25 and closed[0]["quantity"] == 20
    assert closed[0]["entry_price"] == 3.5 and closed[0]["exit_price"] == 4.75


def test_accounts_do_not_share_inventory():
    assert _atr_episode_fill_ledger([
        fill("buy", 10, 4, "entry", account="one"),
        fill("sell", 10, 5, "entry-ocoexit-native", account="two"),
    ]) is None


@pytest.mark.parametrize("stamp", ["2026-10-14T09:00:00+00:00", "2026-10-14T15:00:00+00:00"])
@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
def test_same_episode_accounting_in_premarket_and_rth_on_both_accounts(stamp, account):
    closed, pnl = _atr_episode_fill_ledger([
        fill("buy", 10, 4, "entry", stamp, account),
        fill("sell", 10, 5, "entry-ocoexit-native", stamp, account),
    ])
    assert pnl == 10 and closed[0]["broker_account_name"] == account


def test_overfilled_child_cannot_borrow_another_entry_quantity():
    assert _atr_episode_fill_ledger([
        fill("buy", 10, 4, "old"), fill("buy", 20, 3, "new"),
        fill("sell", 30, 5, "new-ocoexit-native"),
    ]) is None


def test_truncated_display_history_is_unknown():
    assert _atr_episode_fill_ledger([fill("buy", 1, 4, str(n)) for n in range(1000)]) is None


def test_helper_does_not_modify_fill_evidence():
    rows = [fill("buy", 10, 4, "entry"), fill("sell", 10, 5, "entry-ocoexit-native")]
    original = deepcopy(rows)
    assert _atr_episode_fill_ledger(rows)[1] == 10
    assert rows == original


def test_actual_atr_bot_view_replaces_wrong_runtime_cash_and_keeps_oco_row():
    repository = ControlPlaneRepository(
        Settings(_env_file=None, legacy_api_base_url="", strategy_schwab_1m_v2_enabled=True),
        session_factory=None, redis=None,
    )
    views = repository._build_bot_views(
        strategy_runtime={"bots": {"schwab_1m_v2": {"daily_pnl": -999, "closed_today": []}}},
        legacy_shadow={}, recent_intents=[], recent_orders=[],
        recent_fills=[fill("buy", 150, "3.99", "entry"),
                      fill("sell", 150, "4.19", "entry-ocoexit-native")],
        recent_trade_coach_reviews=[], recent_bar_decisions=[], open_orders=[], persisted_snapshots={},
    )
    atr = next(row for row in views if row["strategy_code"] == "schwab_1m_v2")
    assert atr["daily_pnl"] == 30 and len(atr["closed_today"]) == 1
    assert atr["closed_today_source"] == "ATTRIBUTED_EPISODE_FILLS"


def page_data(rows):
    repository = ControlPlaneRepository(
        Settings(_env_file=None, legacy_api_base_url="", strategy_schwab_1m_v2_enabled=True),
        session_factory=None, redis=None,
    )
    bots = repository._build_bot_views(
        strategy_runtime={"bots": {"schwab_1m_v2": {"daily_pnl": -999, "closed_today": []}}},
        legacy_shadow={}, recent_intents=[], recent_orders=[], recent_fills=rows,
        recent_trade_coach_reviews=[], recent_bar_decisions=[], open_orders=[], persisted_snapshots={},
    )
    return {"bots": bots, "recent_fills": rows, "recent_orders": [], "account_positions": [],
            "virtual_positions": []}


def test_actual_html_keeps_episode_identity_and_never_calls_fifo(monkeypatch):
    rows = [fill("buy", 10, 4, "old"), fill("buy", 20, 3, "new"),
            fill("sell", 20, "3.5", "new-ocoexit-native"),
            fill("sell", 10, "4.5", "old-ocoexit-native")]
    data = page_data(rows)
    def forbidden_fifo(**kwargs):
        raise AssertionError("attributed ATR must not reconstruct FIFO cycles")
    monkeypatch.setattr(control, "collect_completed_trade_cycles", forbidden_fifo)
    html = control._render_bot_detail_page(data, "schwab_1m_v2")
    assert "$+15.00" in html
    assert "OCO bracket (target/stop)" in html
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] == 15 and len(payload["closed_today"]) == 2


def test_actual_html_and_api_show_unknown_not_stale_runtime_numbers():
    data = page_data([fill("buy", 10, 4, "old"), fill("buy", 20, 3, "new"),
                      fill("sell", 10, 5, "close")])
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] is None and payload["closed_today"] == []
    assert payload["closed_today_source"] == "UNKNOWN_FILL_EPISODE_COVERAGE"
    html = control._render_bot_detail_page(data, "schwab_1m_v2")
    assert "UNKNOWN: entry-episode fill coverage is incomplete" in html
    assert "$-999.00" not in html
    atr = next(row for row in data["bots"] if row["strategy_code"] == "schwab_1m_v2")
    assert atr["daily_pnl"] is None and atr["closed_today"] == []


def test_actual_html_uses_broker_precision_and_half_up_cash_rounding():
    data = page_data([fill("buy", 163, "3.645", "entry"),
                      fill("sell", 163, "3.60", "entry-ocoexit-native")])
    html = control._render_bot_detail_page(data, "schwab_1m_v2")
    assert "$-7.34" in html and "$3.645" in html
    assert "$-7.33" not in html


def test_partial_realized_pnl_matches_api_and_html_without_a_completed_episode():
    data = page_data([fill("buy", 10, 4, "entry"), fill("sell", 5, 5, "entry-ocoexit-native")])
    payload = control._build_bot_api_payload(data, "schwab_1m_v2")
    assert payload["daily_pnl"] == 5 and payload["closed_today"] == []
    html = control._render_bot_detail_page(data, "schwab_1m_v2")
    assert "$+5.00" in html and "No completed positions" in html
