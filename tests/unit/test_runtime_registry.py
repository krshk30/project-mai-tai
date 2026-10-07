from __future__ import annotations

from project_mai_tai.runtime_registry import configured_strategy_registrations
from project_mai_tai.services.control_plane import BOT_PAGE_META
from project_mai_tai.settings import Settings


def test_configured_strategy_registrations_default_to_core_30s_only() -> None:
    registrations = configured_strategy_registrations(Settings())

    assert [registration.code for registration in registrations] == ["macd_30s"]


def test_configured_strategy_registrations_include_optional_runtimes_when_enabled() -> None:
    registrations = configured_strategy_registrations(
        Settings(
            strategy_schwab_1m_enabled=True,
            strategy_macd_30s_probe_enabled=True,
            strategy_macd_30s_reclaim_enabled=True,
            strategy_macd_30s_retest_enabled=True,
            strategy_macd_1m_enabled=True,
            strategy_tos_enabled=True,
            strategy_runner_enabled=True,
        )
    )

    assert [registration.code for registration in registrations] == [
        "macd_30s",
        "schwab_1m",
        "macd_30s_probe",
        "macd_30s_reclaim",
        "macd_30s_retest",
        "macd_1m",
        "tos",
        "runner",
    ]


def test_atr_and_orb_use_operator_names_without_changing_their_routes() -> None:
    registrations = configured_strategy_registrations(
        Settings(
            strategy_schwab_1m_v2_enabled=True,
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_account_name="live:orb",
            orb_enabled=True,
            orb_live_schwab_orders_enabled=True,
        )
    )
    by_code = {registration.code: registration for registration in registrations}

    assert by_code["schwab_1m_v2"].display_name == "ATR Bot"
    assert by_code["schwab_1m_v2"].account_name == "live:schwab_1m_v2"
    assert BOT_PAGE_META["schwab_1m_v2"]["title"] == "ATR Bot"
    assert BOT_PAGE_META["schwab_1m_v2"]["nav_title"] == "ATR Bot"

    # ORBLIVE1 (operator 10-07): one ORB bot, live; no paper registration, page or label.
    assert "orb" not in by_code
    assert "orb" not in BOT_PAGE_META
    assert by_code["orb_schwab"].display_name == "ORB Live"
    assert by_code["orb_schwab"].execution_mode == "live"
    assert BOT_PAGE_META["orb_schwab"]["title"] == "ORB Live"
    assert BOT_PAGE_META["orb_schwab"]["nav_title"] == "ORB Live"
    assert BOT_PAGE_META["orb_schwab"]["path"] == "/bot/orb"
