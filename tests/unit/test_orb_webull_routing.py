"""ORB's retired Webull settings cannot restore a broker route."""
from __future__ import annotations

from project_mai_tai.broker_adapters.webull import configured_webull_accounts
from project_mai_tai.settings import Settings


def test_orb_provider_is_none_when_override_unset() -> None:
    settings = Settings()
    assert settings.orb_broker_provider is None
    assert settings.provider_for_strategy("orb") == "none"
    assert settings.provider_for_account(settings.orb_broker_account_name) == "none"


def test_orb_cannot_route_to_webull_when_legacy_settings_are_configured() -> None:
    settings = Settings(
        orb_enabled=True,
        orb_broker_account_name="live:orb",
        orb_broker_provider="webull",
        webull_account_id="WB-ACCT-1",
    )
    assert settings.provider_for_account("live:orb") == "none"
    assert settings.provider_for_strategy("orb") == "none"
    accounts = configured_webull_accounts(settings)
    assert "live:orb" not in accounts


def test_orb_paper_account_not_mapped_to_webull_without_override() -> None:
    # Shadow today: paper:orb with no override must NOT map into the webull adapter.
    settings = Settings(orb_enabled=True, webull_account_id="WB-ACCT-1")
    assert "paper:orb" not in configured_webull_accounts(settings)
