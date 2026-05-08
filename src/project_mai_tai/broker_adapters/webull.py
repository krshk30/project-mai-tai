from __future__ import annotations

import logging
from dataclasses import dataclass

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)
from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebullAccountConfig:
    account_id: str


def configured_webull_accounts(settings: Settings) -> dict[str, WebullAccountConfig]:
    configured: dict[str, WebullAccountConfig] = {}
    account_id = (settings.webull_account_id or "").strip()
    if not account_id:
        return configured
    configured[settings.strategy_polygon_30s_account_name] = WebullAccountConfig(account_id=account_id)
    return configured


class WebullBrokerAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        accounts_by_name: dict[str, WebullAccountConfig] | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.webull_base_url.rstrip("/")
        self.region_id = settings.webull_region_id
        self.request_timeout_seconds = settings.webull_request_timeout_seconds
        self.app_key = settings.webull_app_key
        self.app_secret = settings.webull_app_secret
        self.accounts_by_name = accounts_by_name or configured_webull_accounts(settings)

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        reason = self._rejection_reason(request.broker_account_name)
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        if self._has_required_configuration(request.broker_account_name):
            logger.info(
                "Webull order-status polling is not implemented yet for %s",
                request.broker_account_name,
            )
        return None

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        if self._has_required_configuration(broker_account_name):
            logger.info(
                "Webull position sync is not implemented yet for %s",
                broker_account_name,
            )
        return []

    def _has_required_configuration(self, broker_account_name: str) -> bool:
        return (
            bool((self.app_key or "").strip())
            and bool((self.app_secret or "").strip())
            and broker_account_name in self.accounts_by_name
        )

    def _rejection_reason(self, broker_account_name: str) -> str:
        if not (self.app_key or "").strip() or not (self.app_secret or "").strip():
            return (
                "Webull order rejected: missing Webull App Key/App Secret; "
                "listening is active but broker auth is not configured yet"
            )
        if broker_account_name not in self.accounts_by_name:
            return f"Webull order rejected: missing Webull account id for {broker_account_name}"
        return (
            "Webull order rejected: adapter scaffolding is live but official order submission "
            "is not implemented yet"
        )
