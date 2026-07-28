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

    async def fetch_armed_native_oco_symbols(
        self, broker_account_name: str, symbols: list[str]
    ) -> set[str]:
        """Route to the account's adapter. Optional capability -- an adapter without it (Webull,
        Alpaca, simulated) means no native OCO to detect, so return empty (the caller then
        fails open and runs its software ladder)."""
        adapter = self._adapter_for_account(broker_account_name)
        fn = getattr(adapter, "fetch_armed_native_oco_symbols", None)
        if fn is None:
            return set()
        return await fn(broker_account_name, symbols)

    async def fetch_oco_resolved_by_fill_symbols(
        self, broker_account_name: str, symbols: list[str]
    ) -> set[str]:
        """Route to the account's adapter. Optional capability -- an adapter without it (Webull,
        Alpaca, simulated) means no native OCO fills to detect, so return empty (the caller then
        keeps the phantom row for the grace backstop + reject self-heal)."""
        adapter = self._adapter_for_account(broker_account_name)
        fn = getattr(adapter, "fetch_oco_resolved_by_fill_symbols", None)
        if fn is None:
            return set()
        return await fn(broker_account_name, symbols)

    async def fetch_oco_exit_fill(
        self,
        broker_account_name: str,
        symbol: str,
        base_client_order_id: str = "",
        *,
        resolved_within_seconds: float = 3600.0,
    ) -> dict[str, object] | None:
        """Route to the account's adapter. Optional capability -- an adapter without it (Alpaca,
        simulated) has no OCO child legs to read, so return None (the caller then closes the row
        without a recorded exit, exactly as before).

        ⛔ WITHOUT THIS FORWARDER THE WHOLE FEATURE IS A SILENT NO-OP. The OMS holds the ROUTER,
        not a leaf adapter, and its call site is `getattr(adapter, "fetch_oco_exit_fill", None)` --
        so a missing forwarder resolves to None and the capture never runs, with the flag ON and
        the code deployed. Found 2026-07-27 by dry-running the backfill against real closed trades
        instead of waiting for the next session: every row came back
        `AttributeError: 'RoutingBrokerAdapter' object has no attribute 'fetch_oco_exit_fill'`.
        """
        adapter = self._adapter_for_account(broker_account_name)
        fn = getattr(adapter, "fetch_oco_exit_fill", None)
        if fn is None:
            return None
        return await fn(
            broker_account_name,
            symbol,
            base_client_order_id,
            resolved_within_seconds=resolved_within_seconds,
        )

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
