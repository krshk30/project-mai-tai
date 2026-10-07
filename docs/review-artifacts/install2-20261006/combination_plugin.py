"""Exercise retained ALL-ON replays with the final Install2 process settings.

Use only with tests/unit/test_all_on_pm.py on the merged candidate. The
recorded clocks, transports, prices and completed-line controls are unchanged.
This is composition coverage, not a provider or historical-fill attestation.
"""

import pytest


INSTALL2 = {
    "strategy_schwab_1m_v2_keep_rest_after_buy_enabled": True,
    "strategy_schwab_1m_v2_removed_wait_clear_enabled": True,
    "oms_v2_webull_mirror_retained_hold_enabled": True,
    "webull_list_primary_reads_enabled": True,
    "strategy_schwab_1m_v2_retry_one_enabled": True,
    "strategy_schwab_1m_v2_retry_one_max_retries": 0,
}


def install_settings_patch(monkeypatch, settings_type):
    assert set(INSTALL2) <= set(settings_type.model_fields), "incomplete Install2 tree"
    original = settings_type.__init__

    def configured(self, **values):
        original(self, **{**values, **INSTALL2})
        assert all(getattr(self, key) == value for key, value in INSTALL2.items())

    monkeypatch.setattr(settings_type, "__init__", configured)


@pytest.fixture(autouse=True)
def exact_install2_settings(monkeypatch):
    from project_mai_tai.settings import Settings

    install_settings_patch(monkeypatch, Settings)
