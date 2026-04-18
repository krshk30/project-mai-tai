from __future__ import annotations

from collections.abc import Callable

from project_mai_tai.broker_adapters.protocols import (
    BrokerAdapter,
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)


class RoutingBrokerAdapter:
    def __init__(
        self,
        *,
        default_provider: str,
        provider_by_account: dict[str, str],
        factories_by_provider: dict[str, Callable[[], BrokerAdapter]],
    ) -> None:
        self.default_provider = str(default_provider)
        self.provider_by_account = {
            str(account_name): str(provider)
            for account_name, provider in provider_by_account.items()
            if str(account_name).strip() and str(provider).strip()
        }
        self.factories_by_provider = dict(factories_by_provider)
        self._adapters_by_provider: dict[str, BrokerAdapter] = {}

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        adapter = self._adapter_for_account(request.broker_account_name)
        return await adapter.submit_order(request)

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        adapter = self._adapter_for_account(request.broker_account_name)
        return await adapter.fetch_order_update(request)

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        adapter = self._adapter_for_account(broker_account_name)
        return await adapter.list_account_positions(broker_account_name)

    def _adapter_for_account(self, broker_account_name: str) -> BrokerAdapter:
        provider = self.provider_by_account.get(str(broker_account_name), self.default_provider)
        return self._adapter_for_provider(provider)

    def _adapter_for_provider(self, provider: str) -> BrokerAdapter:
        normalized_provider = str(provider)
        adapter = self._adapters_by_provider.get(normalized_provider)
        if adapter is not None:
            return adapter

        factory = self.factories_by_provider.get(normalized_provider)
        if factory is None:
            raise RuntimeError(f"Unsupported broker provider for routing adapter: {normalized_provider}")
        adapter = factory()
        self._adapters_by_provider[normalized_provider] = adapter
        return adapter
