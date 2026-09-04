"""The legacy ORB pricing flags are evidence only in broker-disconnected paper mode."""

from __future__ import annotations

from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.settings import Settings


def _service(**overrides: object) -> OrbService:
    return OrbService(
        settings=Settings(orb_running_high_enabled=True, **overrides),
        redis_client=MagicMock(),
    )


def test_legacy_pricing_policy_is_recorded_without_dispatch() -> None:
    service = _service()
    decision = service._build_paper_entry_decision(
        "FOO", 10.50, observed_at=service._session_open_utc()
    )
    metadata = decision.detail["metadata"]

    assert metadata["order_type"] == "limit"
    assert metadata["limit_price"] == "10.5000"
    assert metadata["reference_price"] == "10.5000"
    assert metadata["orb_intended_break_level"] == "10.5000"
    assert "price_source" not in metadata


def test_quote_priced_policy_is_recorded_without_a_broker_price() -> None:
    service = _service(orb_oms_quote_priced_entry_enabled=True)
    decision = service._build_paper_entry_decision(
        "FOO", 10.50, observed_at=service._session_open_utc()
    )
    metadata = decision.detail["metadata"]

    assert metadata["order_type"] == "limit"
    assert "limit_price" not in metadata
    assert "reference_price" not in metadata
    assert metadata["price_source"] == "ask"
    assert metadata["orb_intended_break_level"] == "10.5000"
    assert metadata["orb_gap_cap_pct"] == "1.5"
