"""Webull live broker adapter (US OpenAPI).

Built against the read-shapes confirmed by the on-box probe (2026-06-24): auth + account
reads + symbol->instrument_id resolution all verified against ``api.webull.com`` with the
``webull-openapi-python-sdk``. See ``docs/webull-orb-live-adapter-design.md`` and the
``project_mai_tai_webull_probe`` memory.

Design notes / safety:
- The SDK is imported LAZILY (never at module import) so this file is importable in the
  OMS / CI without ``webull-openapi-python-sdk`` installed. If the SDK or credentials are
  absent, every order is ``rejected`` (the old safe fallback) and reads return empty.
- The SDK client is synchronous (requests-based); every call is wrapped in
  ``asyncio.to_thread`` so the OMS event loop is never blocked.
- ORDER-RESPONSE field names (place / order-detail / holdings) are not yet confirmed by a
  real order (account was unfunded at build time). Extraction is therefore defensive
  (multiple candidate keys) and logs the raw body when it cannot parse a field. These are
  marked ``CONFIRM-AT-TEST`` and must be validated by a funded far-from-market test order
  before the live ``live:orb`` go-live.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)
from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)

# Webull OrderStatus -> our ExecutionReport.event_type. Values cover both the enum-name
# form ("PARTIAL_FILLED") and the human form ("PARTIAL FILLED") the SDK exposes.
_FILLED_STATUSES = {"FILLED"}
_PARTIAL_STATUSES = {"PARTIAL_FILLED", "PARTIAL FILLED", "PARTIALLY_FILLED"}
_CANCELLED_STATUSES = {"CANCELLED", "CANCELED"}
_REJECTED_STATUSES = {"FAILED", "REJECTED"}
_ACCEPTED_STATUSES = {"SUBMITTED", "PENDING", "WORKING", "ACCEPTED", "QUEUED"}


@dataclass(frozen=True)
class WebullAccountConfig:
    account_id: str


def configured_webull_accounts(settings: Settings) -> dict[str, WebullAccountConfig]:
    """Map every broker-account name routed to the ``webull`` provider -> the account id.

    The account id comes from ``MAI_TAI_WEBULL_ACCOUNT_ID``. Account names are discovered
    from the runtime registrations whose provider resolves to ``webull`` (so wiring
    ``live:orb`` -> webull in settings automatically maps it here). Returns empty when the
    account id is unset (keeps the safe reject-everything fallback).
    """
    account_id = (settings.webull_account_id or "").strip()
    if not account_id:
        return {}

    configured: dict[str, WebullAccountConfig] = {}
    try:
        from project_mai_tai.runtime_registry import configured_broker_account_registrations

        for registration in configured_broker_account_registrations(settings):
            if str(registration.provider).strip().lower() == "webull":
                configured[registration.name] = WebullAccountConfig(account_id=account_id)
    except Exception:  # pragma: no cover - registry should never break order routing
        logger.debug("configured_webull_accounts: registration scan failed", exc_info=True)

    return configured


class WebullBrokerAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        accounts_by_name: dict[str, WebullAccountConfig] | None = None,
        client: object | None = None,
    ) -> None:
        self.settings = settings
        self.region_id = (settings.webull_region_id or "us").strip() or "us"
        # SDK wants a bare host (no scheme / trailing slash); env may carry either form.
        self.host = self._normalize_host(settings.webull_base_url)
        self.app_key = (settings.webull_app_key or "").strip()
        self.app_secret = (settings.webull_app_secret or "").strip()
        self.accounts_by_name = (
            accounts_by_name if accounts_by_name is not None else configured_webull_accounts(settings)
        )
        self._client = client  # injectable for tests; otherwise lazily built
        self._client_lock = threading.Lock()
        self._instrument_cache: dict[str, str] = {}
        self._instrument_lock = threading.Lock()

    # ------------------------------------------------------------------ public API
    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        account = self.accounts_by_name.get(request.broker_account_name)
        if account is None:
            return [self._reject(request, self._missing_config_reason(request.broker_account_name))]
        if request.intent_type == "cancel":
            return await self._cancel_order(account, request)
        try:
            return await asyncio.to_thread(self._submit_blocking, account, request)
        except Exception as exc:  # noqa: BLE001 - any SDK/transport error -> reject, never crash OMS
            return [self._reject(request, self._exc_reason(exc))]

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        account = self.accounts_by_name.get(request.broker_account_name)
        if account is None:
            return None
        try:
            return await asyncio.to_thread(self._fetch_order_blocking, account, request)
        except Exception:  # noqa: BLE001
            logger.exception("Webull order-status fetch failed for %s", request.client_order_id)
            return None

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        account = self.accounts_by_name.get(broker_account_name)
        if account is None:
            return []
        try:
            return await asyncio.to_thread(self._positions_blocking, account, broker_account_name)
        except Exception:  # noqa: BLE001
            logger.exception("Webull position sync failed for %s", broker_account_name)
            return []

    # ------------------------------------------------------------------ blocking impls
    def _submit_blocking(
        self, account: WebullAccountConfig, request: OrderRequest
    ) -> list[ExecutionReport]:
        client = self._get_client()
        instrument_id = self._resolve_instrument_id(client, request.symbol)
        if not instrument_id:
            return [self._reject(request, f"Webull instrument id not found for {request.symbol}")]

        from webull.trade.request.place_order_request import PlaceOrderRequest

        po = PlaceOrderRequest()
        po.set_account_id(account.account_id)
        po.set_client_order_id(request.client_order_id)
        po.set_instrument_id(instrument_id)
        po.set_side("BUY" if request.side == "buy" else "SELL")
        po.set_order_type(self._order_type(request))
        po.set_qty(str(int(request.quantity)) if request.quantity == request.quantity.to_integral_value() else str(request.quantity))
        po.set_tif(self._tif(request))
        if hasattr(po, "set_category"):
            po.set_category("US_STOCK")
        limit_price = self._meta_price(request, "limit_price", "reference_price")
        if self._order_type(request) in {"LIMIT", "STOP_LIMIT"} and limit_price is not None:
            po.set_limit_price(str(limit_price))
        stop_price = self._meta_price(request, "stop_price")
        if self._order_type(request) in {"STOP", "STOP_LIMIT"} and stop_price is not None:
            po.set_stop_price(str(stop_price))
        # extended_hours_trading is REQUIRED by the API (null -> ILLEGAL_PARAMETER 417);
        # always set it, defaulting to RTH-only (False).
        if hasattr(po, "set_extended_hours_trading"):
            ext = str(request.metadata.get("extended_hours", request.metadata.get("session", ""))).strip().lower()
            po.set_extended_hours_trading(ext in {"true", "1", "yes", "am", "pm", "extended"})

        body = self._body(client.get_response(po))
        # Confirmed: the place response returns only {client_order_id}; the broker order_id
        # appears later in the order-detail (fetch_order_update). So a None here is normal.
        broker_order_id = self._first_str(body, "order_id", "orderId")
        # A resting limit (ORB reclaim) is acknowledged; the OMS polls fetch_order_update for
        # the fill — so we return "accepted" and do not block waiting on a terminal state.
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    def _fetch_order_blocking(
        self, account: WebullAccountConfig, request: OrderRequest
    ) -> ExecutionReport | None:
        client = self._get_client()
        try:
            from webull.trade.request.get_order_detail_request import OrderDetailRequest
        except ImportError:  # pragma: no cover - SDK layout fallback
            from webull.trade.request.v2.get_order_detail_request import OrderDetailRequest

        od = OrderDetailRequest()
        od.set_account_id(account.account_id)
        od.set_client_order_id(request.client_order_id)
        body = self._body(client.get_response(od))
        if not isinstance(body, dict):
            logger.warning("Webull order-detail: unexpected body=%r", body)
            return None

        # Confirmed shape: {order_id, client_order_id, ..., items:[{order_status, filled_qty, ...}]}.
        # order_id is top-level; status/fill live in items[0].
        items = body.get("items") if isinstance(body.get("items"), list) else []
        item = items[0] if items and isinstance(items[0], dict) else {}
        broker_order_id = self._first_str(body, "order_id", "orderId")
        event_type = self._map_status(item)
        filled_quantity = (
            self._decimal_or_none(item, "filled_qty", "filledQty", "filled_quantity", "filledQuantity")
            or Decimal("0")
        )
        # fill-price field unconfirmed (test order never filled) -> defensive candidates.
        fill_price = self._decimal_or_none(
            item, "avg_fill_price", "avgFillPrice", "filled_avg_price", "average_filled_price", "avg_price"
        )
        return ExecutionReport(
            event_type=event_type,  # type: ignore[arg-type]
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id,
            broker_fill_id=self._fill_id(broker_order_id, filled_quantity, event_type),
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            reason=str(item.get("failure_reason") or item.get("failureReason") or request.reason),
            metadata=dict(request.metadata),
        )

    def _positions_blocking(
        self, account: WebullAccountConfig, broker_account_name: str
    ) -> list[BrokerPositionSnapshot]:
        client = self._get_client()
        from webull.trade.request.get_account_positions_request import AccountPositionsRequest

        snapshots: list[BrokerPositionSnapshot] = []
        last_instrument_id: str | None = None
        for _ in range(20):  # hard page cap (safety)
            req = AccountPositionsRequest()
            req.set_account_id(account.account_id)
            if hasattr(req, "set_page_size"):
                req.set_page_size(50)
            if last_instrument_id and hasattr(req, "set_last_instrument_id"):
                req.set_last_instrument_id(last_instrument_id)
            body = self._body(client.get_response(req))
            if not isinstance(body, dict):
                break
            holdings = body.get("holdings") or body.get("positions") or []
            for raw in holdings:
                snapshot = self._position_snapshot(raw, broker_account_name)
                if snapshot is not None:
                    snapshots.append(snapshot)
            if not body.get("has_next") and not body.get("hasNext"):
                break
            if not holdings:
                break
            # CONFIRM-AT-TEST: the pagination cursor field name on a holding.
            last = holdings[-1]
            last_instrument_id = self._first_str(last, "instrument_id", "instrumentId") if isinstance(last, dict) else None
            if not last_instrument_id:
                break
        return snapshots

    async def _cancel_order(
        self, account: WebullAccountConfig, request: OrderRequest
    ) -> list[ExecutionReport]:
        try:
            return await asyncio.to_thread(self._cancel_blocking, account, request)
        except Exception as exc:  # noqa: BLE001
            return [self._reject(request, self._exc_reason(exc))]

    def _cancel_blocking(
        self, account: WebullAccountConfig, request: OrderRequest
    ) -> list[ExecutionReport]:
        client = self._get_client()
        from webull.trade.request.cancel_order_request import CancelOrderRequest

        co = CancelOrderRequest()
        co.set_account_id(account.account_id)
        co.set_client_order_id(request.client_order_id)
        self._body(client.get_response(co))
        return [
            ExecutionReport(
                event_type="cancelled",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason or "cancelled",
                metadata=dict(request.metadata),
            )
        ]

    # ------------------------------------------------------------------ client / instrument
    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            if not (self.app_key and self.app_secret):
                raise RuntimeError("Webull credentials are not configured (app key/secret)")
            from webull.core.client import ApiClient

            client = ApiClient(self.app_key, self.app_secret, self.region_id)
            if self.host and hasattr(client, "add_endpoint"):
                client.add_endpoint(self.region_id, self.host)
            self._client = client
            return client

    def _resolve_instrument_id(self, client: object, symbol: str) -> str | None:
        key = str(symbol).upper().strip()
        if not key:
            return None
        with self._instrument_lock:
            cached = self._instrument_cache.get(key)
        if cached:
            return cached
        from webull.data.quotes.instrument import Instrument

        body = self._body(Instrument(client).get_instrument(symbols=key, category="US_STOCK"))
        rows = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("symbol", "")).upper() != key and raw.get("symbol") is not None:
                continue
            instrument_id = self._first_str(raw, "instrument_id", "instrumentId")
            if instrument_id:
                with self._instrument_lock:
                    self._instrument_cache[key] = instrument_id
                return instrument_id
        logger.warning("Webull instrument lookup returned no id for %s: %r", key, body)
        return None

    # ------------------------------------------------------------------ helpers
    def _position_snapshot(
        self, raw: object, broker_account_name: str
    ) -> BrokerPositionSnapshot | None:
        if not isinstance(raw, dict):
            return None
        instrument = raw.get("instrument") if isinstance(raw.get("instrument"), dict) else {}
        symbol = (
            self._first_str(raw, "symbol", "ticker")
            or (self._first_str(instrument, "symbol", "ticker") if instrument else None)
        )
        if not symbol:
            logger.warning("Webull position without a symbol: %r", raw)
            return None
        quantity = self._decimal_or_none(raw, "quantity", "qty", "position", "shares") or Decimal("0")
        if quantity == 0:
            return None
        # Confirmed live shape (margin account holdings): unit_cost + market_value + last_price.
        average_price = (
            self._decimal_or_none(
                raw, "unit_cost", "unitCost", "cost_price", "costPrice", "average_cost", "avg_cost", "avg_price"
            )
            or Decimal("0")
        )
        market_value = self._decimal_or_none(raw, "market_value", "marketValue", "last_price")
        return BrokerPositionSnapshot(
            broker_account_name=broker_account_name,
            symbol=symbol.upper(),
            quantity=quantity,
            average_price=average_price,
            market_value=market_value,
            as_of=datetime.now(UTC),
        )

    def _map_status(self, order: dict[str, object]) -> str:
        status = str(order.get("status") or order.get("order_status") or order.get("orderStatus") or "").upper()
        if status in _FILLED_STATUSES:
            return "filled"
        if status in _PARTIAL_STATUSES:
            return "partially_filled"
        if status in _CANCELLED_STATUSES:
            return "cancelled"
        if status in _REJECTED_STATUSES:
            return "rejected"
        return "accepted"

    @staticmethod
    def _order_type(request: OrderRequest) -> str:
        raw = str(request.metadata.get("order_type", request.order_type)).strip().upper()
        return raw or "MARKET"

    @staticmethod
    def _tif(request: OrderRequest) -> str:
        raw = str(request.metadata.get("time_in_force", request.time_in_force)).strip().upper()
        return {"GTC": "GTC", "IOC": "IOC"}.get(raw, "DAY")

    @staticmethod
    def _meta_price(request: OrderRequest, *keys: str) -> Decimal | None:
        for key in keys:
            value = request.metadata.get(key)
            if value not in (None, ""):
                try:
                    return Decimal(str(value))
                except (InvalidOperation, ValueError):
                    continue
        return None

    @staticmethod
    def _normalize_host(base_url: str | None) -> str:
        """Reduce an env base_url to the bare host the SDK's add_endpoint expects."""
        host = (base_url or "").strip()
        if not host:
            return ""
        host = host.split("://", 1)[-1]  # drop scheme if present
        return host.split("/", 1)[0].strip()  # drop any path / trailing slash

    @staticmethod
    def _body(response: object) -> object:
        # Live SDK calls return a requests.Response (parse via .json(); no .body attr).
        # Some wrappers expose .body directly. Handle both.
        body = getattr(response, "body", None)
        if body is not None:
            return body
        if hasattr(response, "json"):
            try:
                return response.json()
            except Exception:  # noqa: BLE001
                return None
        return response

    @staticmethod
    def _first_str(body: object, *keys: str) -> str | None:
        if not isinstance(body, dict):
            return None
        for key in keys:
            value = body.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _decimal_or_none(body: object, *keys: str) -> Decimal | None:
        if not isinstance(body, dict):
            return None
        for key in keys:
            value = body.get(key)
            if value in (None, ""):
                continue
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue
        return None

    @staticmethod
    def _fill_id(broker_order_id: str | None, filled_quantity: Decimal, event_type: str) -> str | None:
        if event_type not in {"filled", "partially_filled"}:
            return None
        if not broker_order_id or filled_quantity <= 0:
            return None
        return f"{broker_order_id}:{filled_quantity}"

    def _reject(self, request: OrderRequest, reason: str) -> ExecutionReport:
        return ExecutionReport(
            event_type="rejected",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            reason=reason,
            metadata=dict(request.metadata),
        )

    def _missing_config_reason(self, broker_account_name: str) -> str:
        if not (self.app_key and self.app_secret):
            return (
                "Webull order rejected: missing Webull App Key/App Secret; "
                "broker auth is not configured"
            )
        return f"Webull order rejected: no Webull account id mapped for {broker_account_name}"

    @staticmethod
    def _exc_reason(exc: Exception) -> str:
        code = getattr(exc, "error_code", None)
        msg = getattr(exc, "error_msg", None)
        http = getattr(exc, "http_status", None)
        if code or msg or http:
            return f"Webull order rejected: {code or ''} {msg or ''} (http {http})".strip()
        return f"Webull order rejected: {exc!r}"
