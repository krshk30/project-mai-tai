from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import urllib.error
from urllib.parse import quote
import urllib.request

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)
from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str


def configured_alpaca_credentials(settings: Settings) -> dict[str, AlpacaCredentials]:
    configured: dict[str, AlpacaCredentials] = {}

    def add(account_name: str, api_key: str | None, secret_key: str | None) -> None:
        if not api_key or not secret_key:
            return
        configured[account_name] = AlpacaCredentials(api_key=api_key, secret_key=secret_key)

    add(
        settings.strategy_macd_30s_account_name,
        settings.alpaca_macd_30s_api_key,
        settings.alpaca_macd_30s_secret_key,
    )
    add(
        settings.strategy_macd_1m_account_name,
        settings.alpaca_macd_1m_api_key,
        settings.alpaca_macd_1m_secret_key,
    )
    add(
        settings.strategy_tos_account_name,
        settings.alpaca_tos_runner_api_key,
        settings.alpaca_tos_runner_secret_key,
    )
    add(
        settings.strategy_runner_account_name,
        settings.alpaca_tos_runner_api_key,
        settings.alpaca_tos_runner_secret_key,
    )
    return configured


class AlpacaPaperBrokerAdapter:
    FILLED_STATUSES = {"filled"}
    PARTIAL_FILL_STATUSES = {"partially_filled"}
    ACCEPTED_STATUSES = {
        "accepted",
        "accepted_for_bidding",
        "accepted_for_tracking",
        "new",
        "pending_new",
        "pending_replace",
        "calculated",
    }
    CANCELLED_STATUSES = {"canceled", "cancelled", "done_for_day", "expired"}
    REJECTED_STATUSES = {"rejected", "suspended"}

    def __init__(
        self,
        settings: Settings,
        *,
        credentials_by_account: dict[str, AlpacaCredentials] | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.alpaca_paper_base_url.rstrip("/")
        self.credentials_by_account = credentials_by_account or configured_alpaca_credentials(settings)
        self.request_timeout_seconds = settings.alpaca_request_timeout_seconds
        self.fill_timeout_seconds = settings.alpaca_order_fill_timeout_seconds
        self.poll_interval_seconds = settings.alpaca_order_poll_interval_seconds
        self.cancel_unfilled_after_timeout = settings.alpaca_cancel_unfilled_after_timeout
        self._sleep = asyncio.sleep

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        credentials = self.credentials_by_account.get(request.broker_account_name)
        if credentials is None:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=f"missing Alpaca credentials for {request.broker_account_name}",
                    metadata=dict(request.metadata),
                )
            ]

        if request.intent_type == "cancel":
            return await self._cancel_order(credentials, request)

        status_code, response = await self._request_json(
            credentials,
            "POST",
            "/v2/orders",
            body=self._build_order_payload(request),
        )
        if status_code >= 400 or not isinstance(response, dict):
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=self._extract_error_reason(response),
                    metadata=dict(request.metadata),
                )
            ]

        initial_event_type = self._map_order_status(str(response.get("status", "")))
        if initial_event_type == "rejected":
            return [self._execution_report_from_order(request=request, order=response, event_type="rejected")]

        reports = [
            self._execution_report_from_order(
                request=request,
                order=response,
                event_type="accepted",
            )
        ]

        final_order = await self._wait_for_terminal_order(credentials, request, response)
        if final_order is None:
            return reports

        final_event_type = self._map_order_status(str(final_order.get("status", "")))
        if final_event_type == "accepted":
            return reports

        reports.append(
            self._execution_report_from_order(
                request=request,
                order=final_order,
                event_type=final_event_type,
            )
        )
        return reports

    async def _cancel_order(
        self,
        credentials: AlpacaCredentials,
        request: OrderRequest,
    ) -> list[ExecutionReport]:
        broker_order_id = str(request.metadata.get("broker_order_id", "")).strip()
        target_client_order_id = str(
            request.metadata.get("target_client_order_id") or request.client_order_id
        ).strip()

        if not broker_order_id and target_client_order_id:
            status_code, response = await self._request_json(
                credentials,
                "GET",
                f"/v2/orders:by_client_order_id?client_order_id={quote(target_client_order_id, safe='')}",
            )
            if status_code < 400 and isinstance(response, dict):
                broker_order_id = str(response.get("id", "")).strip()
            elif status_code >= 400:
                return [
                    ExecutionReport(
                        event_type="rejected",
                        client_order_id=target_client_order_id or request.client_order_id,
                        broker_order_id=None,
                        symbol=request.symbol,
                        side=request.side,
                        intent_type="cancel",
                        quantity=request.quantity,
                        reason=self._extract_error_reason(response),
                        metadata=dict(request.metadata),
                    )
                ]

        if not broker_order_id:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=target_client_order_id or request.client_order_id,
                    broker_order_id=None,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason="missing broker_order_id for cancel intent",
                    metadata=dict(request.metadata),
                )
            ]

        status_code, response = await self._request_json(
            credentials,
            "DELETE",
            f"/v2/orders/{quote(broker_order_id, safe='')}",
        )
        if status_code >= 400:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=target_client_order_id or request.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=self._extract_error_reason(response),
                    metadata=dict(request.metadata),
                )
            ]

        status_code, order = await self._request_json(
            credentials,
            "GET",
            f"/v2/orders/{quote(broker_order_id, safe='')}",
        )
        if status_code >= 400 or not isinstance(order, dict):
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=target_client_order_id or request.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                )
            ]

        return [
            self._execution_report_from_order(
                request=request,
                order=order,
                event_type=self._map_order_status(str(order.get("status", ""))),
            )
        ]

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        credentials = self.credentials_by_account.get(broker_account_name)
        if credentials is None:
            logger.warning("missing Alpaca credentials for broker account %s", broker_account_name)
            return []

        status_code, response = await self._request_json(credentials, "GET", "/v2/positions")
        if status_code >= 400 or not isinstance(response, list):
            logger.warning(
                "failed listing Alpaca positions for %s: %s",
                broker_account_name,
                self._extract_error_reason(response),
            )
            return []

        snapshots: list[BrokerPositionSnapshot] = []
        for raw in response:
            if not isinstance(raw, dict):
                continue
            quantity = self._decimal_or_zero(raw.get("qty"))
            if quantity <= 0:
                continue
            snapshots.append(
                BrokerPositionSnapshot(
                    broker_account_name=broker_account_name,
                    symbol=str(raw.get("symbol", "")).upper(),
                    quantity=quantity,
                    average_price=self._decimal_or_zero(raw.get("avg_entry_price")),
                    market_value=self._decimal_or_none(raw.get("market_value")),
                    as_of=self._parse_datetime(raw.get("updated_at")) or self._parse_datetime(raw.get("lastday_price")),
                )
            )
        return snapshots

    def _build_order_payload(self, request: OrderRequest) -> dict[str, object]:
        order_type = request.metadata.get("order_type", request.order_type)
        time_in_force = request.metadata.get("time_in_force", request.time_in_force)
        payload: dict[str, object] = {
            "symbol": request.symbol,
            "qty": self._decimal_to_string(request.quantity),
            "side": request.side,
            "type": order_type,
            "time_in_force": time_in_force,
            "client_order_id": request.client_order_id,
        }
        limit_price = request.metadata.get("limit_price")
        if order_type == "limit":
            payload["limit_price"] = limit_price or request.metadata.get("reference_price")
        if order_type == "stop":
            payload["stop_price"] = request.metadata.get("stop_price") or request.metadata.get("reference_price")
        if request.metadata.get("extended_hours", "").lower() == "true":
            payload["extended_hours"] = True
        return payload

    async def _wait_for_terminal_order(
        self,
        credentials: AlpacaCredentials,
        request: OrderRequest,
        initial_order: dict[str, object],
    ) -> dict[str, object] | None:
        order_id = str(initial_order.get("id", ""))
        if not order_id:
            return None

        current_status = self._map_order_status(str(initial_order.get("status", "")))
        if current_status in {"filled", "partially_filled", "rejected", "cancelled"}:
            return initial_order

        deadline = asyncio.get_running_loop().time() + max(1.0, self.fill_timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            await self._sleep(self.poll_interval_seconds)
            status_code, order = await self._request_json(credentials, "GET", f"/v2/orders/{order_id}")
            if status_code >= 400 or not isinstance(order, dict):
                continue
            mapped_status = self._map_order_status(str(order.get("status", "")))
            if mapped_status in {"filled", "partially_filled", "rejected", "cancelled"}:
                return order

        if not self.cancel_unfilled_after_timeout:
            return None

        await self._request_json(credentials, "DELETE", f"/v2/orders/{order_id}")
        status_code, order = await self._request_json(credentials, "GET", f"/v2/orders/{order_id}")
        if status_code >= 400 or not isinstance(order, dict):
            return {
                "id": order_id,
                "status": "canceled",
                "symbol": request.symbol,
                "side": request.side,
                "qty": self._decimal_to_string(request.quantity),
                "filled_qty": "0",
                "client_order_id": request.client_order_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        return order

    async def _request_json(
        self,
        credentials: AlpacaCredentials,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, object]:
        return await asyncio.to_thread(
            self._blocking_request_json,
            credentials,
            method,
            path,
            body,
        )

    def _blocking_request_json(
        self,
        credentials: AlpacaCredentials,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> tuple[int, object]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "APCA-API-KEY-ID": credentials.api_key,
            "APCA-API-SECRET-KEY": credentials.secret_key,
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return response.getcode(), self._decode_json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            return exc.code, self._decode_json(raw)
        except Exception as exc:  # pragma: no cover - exercised via rejection fallback
            return 599, {"message": str(exc)}

    def _execution_report_from_order(
        self,
        *,
        request: OrderRequest,
        order: dict[str, object],
        event_type: str,
    ) -> ExecutionReport:
        broker_order_id = str(order.get("id", "")) or None
        filled_quantity = self._decimal_or_zero(order.get("filled_qty"))
        reported_at = self._parse_datetime(order.get("updated_at")) or datetime.now(UTC)
        fill_price = self._decimal_or_none(order.get("filled_avg_price"))
        broker_fill_id = None
        if event_type in {"filled", "partially_filled"} and broker_order_id and filled_quantity > 0:
            broker_fill_id = f"{broker_order_id}:{filled_quantity}:{reported_at.isoformat()}"

        return ExecutionReport(
            event_type=event_type,  # type: ignore[arg-type]
            client_order_id=str(order.get("client_order_id", request.client_order_id)),
            broker_order_id=broker_order_id,
            broker_fill_id=broker_fill_id,
            symbol=str(order.get("symbol", request.symbol)).upper(),
            side=str(order.get("side", request.side)),  # type: ignore[arg-type]
            intent_type=request.intent_type,
            quantity=self._decimal_or_zero(order.get("qty")) or request.quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            reason=self._extract_error_reason(order) or request.reason,
            metadata=dict(request.metadata),
            reported_at=reported_at,
        )

    def _map_order_status(self, status: str) -> str:
        normalized = status.lower()
        if normalized in self.FILLED_STATUSES:
            return "filled"
        if normalized in self.PARTIAL_FILL_STATUSES:
            return "partially_filled"
        if normalized in self.CANCELLED_STATUSES:
            return "cancelled"
        if normalized in self.REJECTED_STATUSES:
            return "rejected"
        if normalized in self.ACCEPTED_STATUSES:
            return "accepted"
        return "accepted"

    def _extract_error_reason(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("message", "reason", "error"):
                value = payload.get(key)
                if value:
                    return str(value)
        if payload is None:
            return "unknown Alpaca error"
        return str(payload)

    def _parse_datetime(self, value: object) -> datetime | None:
        if value is None:
            return None
        try:
            normalized = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _decimal_to_string(self, value: Decimal) -> str:
        return format(value, "f")

    def _decimal_or_zero(self, value: object) -> Decimal:
        result = self._decimal_or_none(value)
        return result if result is not None else Decimal("0")

    def _decimal_or_none(self, value: object) -> Decimal | None:
        if value in {None, ""}:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None

    def _decode_json(self, raw: str) -> object:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"message": raw}
