from __future__ import annotations

from project_mai_tai.runtime_registry import configured_strategy_registrations
from project_mai_tai.settings import Settings


def test_configured_strategy_registrations_default_to_core_30s_only() -> None:
    registrations = configured_strategy_registrations(Settings())

    assert [registration.code for registration in registrations] == ["macd_30s"]


def test_configured_strategy_registrations_include_optional_runtimes_when_enabled() -> None:
    registrations = configured_strategy_registrations(
        Settings(
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
        "macd_30s_probe",
        "macd_30s_reclaim",
        "macd_30s_retest",
        "macd_1m",
        "tos",
        "runner",
    ]
