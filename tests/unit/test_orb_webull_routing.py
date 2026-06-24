"""ORB -> Webull provider routing (wiring for the live:orb go-live)."""
from __future__ import annotations

from project_mai_tai.broker_adapters.webull import configured_webull_accounts
from project_mai_tai.settings import Settings


def test_orb_provider_defaults_to_resolved_when_override_unset() -> None:
    # Behaviour-identical to pre-wiring: no orb_broker_provider -> the global default.
    settings = Settings()
    assert settings.orb_broker_provider is None
    assert settings.provider_for_account(settings.orb_broker_account_name) == settings.resolved_broker_provider


def test_orb_routes_to_webull_when_configured() -> None:
    settings = Settings(
        orb_enabled=True,
        orb_broker_account_name="live:orb",
        orb_broker_provider="webull",
        webull_account_id="WB-ACCT-1",
    )
    assert settings.provider_for_account("live:orb") == "webull"
    assert settings.provider_for_strategy("orb") == "webull"
    # The webull adapter then maps the live:orb account -> the configured account id.
    accounts = configured_webull_accounts(settings)
    assert "live:orb" in accounts
    assert accounts["live:orb"].account_id == "WB-ACCT-1"


def test_orb_paper_account_not_mapped_to_webull_without_override() -> None:
    # Shadow today: paper:orb with no override must NOT map into the webull adapter.
    settings = Settings(orb_enabled=True, webull_account_id="WB-ACCT-1")
    assert "paper:orb" not in configured_webull_accounts(settings)
