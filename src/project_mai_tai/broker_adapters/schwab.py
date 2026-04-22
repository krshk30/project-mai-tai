from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderRequest,
)
from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchwabAccountConfig:
    account_hash: str


def configured_schwab_accounts(settings: Settings) -> dict[str, SchwabAccountConfig]:
    configured: dict[str, SchwabAccountConfig] = {}
    shared_hash = (settings.schwab_account_hash or "").strip()
    shared_tos_runner_hash = (settings.schwab_tos_runner_account_hash or shared_hash).strip()

    def add(account_name: str, account_hash: str | None) -> None:
        resolved = (account_hash or shared_hash).strip() if account_hash is not None else shared_hash
        if not resolved:
            return
        configured[account_name] = SchwabAccountConfig(account_hash=resolved)

    add(settings.strategy_macd_30s_account_name, settings.schwab_macd_30s_account_hash)
    add(settings.strategy_macd_1m_account_name, settings.schwab_macd_1m_account_hash)
    add(settings.strategy_tos_account_name, shared_tos_runner_hash)
    add(settings.strategy_runner_account_name, shared_tos_runner_hash)
    return configured


class SchwabBrokerAdapter:
    FILLED_STATUSES = {"FILLED"}
    PARTIAL_FILL_STATUSES = {"PARTIAL_FILL"}
    ACCEPTED_STATUSES = {
        "ACCEPTED",
        "AWAITING_CONDITION",
        "AWAITING_MANUAL_REVIEW",
        "AWAITING_PARENT_ORDER",
        "AWAITING_RELEASE_TIME",
        "NEW",
        "PENDING_ACKNOWLEDGEMENT",
        "PENDING_ACTIVATION",
        "PENDING_CANCEL",
        "PENDING_RECALL",
        "PENDING_REPLACE",
        "QUEUED",
        "WORKING",
    }
    CANCELLED_STATUSES = {"CANCELED", "CANCELLED", "EXPIRED", "REPLACED"}
    REJECTED_STATUSES = {"REJECTED"}

    def __init__(
        self,
        settings: Settings,
        *,
        accounts_by_name: dict[str, SchwabAccountConfig] | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.schwab_base_url.rstrip("/")
        self.token_url = settings.schwab_token_url
        self.accounts_by_name = accounts_by_name or configured_schwab_accounts(settings)
        self.request_timeout_seconds = settings.schwab_request_timeout_seconds
        self.fill_timeout_seconds = settings.schwab_order_fill_timeout_seconds
        self.poll_interval_seconds = settings.schwab_order_poll_interval_seconds
        self.refresh_margin_seconds = max(0, settings.schwab_token_refresh_margin_seconds)
        self.client_id = settings.schwab_client_id
        self.client_secret = settings.schwab_client_secret
        self._token_store_path = (
            Path(settings.schwab_token_store_path).expanduser()
            if settings.schwab_token_store_path
            else None
        )
        self._sleep = asyncio.sleep
        self._token_lock = asyncio.Lock()
        self._access_token = settings.schwab_access_token
        self._access_token_expires_at = self._parse_datetime(settings.schwab_access_token_expires_at)
        self._refresh_token = settings.schwab_refresh_token
        self._load_token_store()

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        account = self.accounts_by_name.get(request.broker_account_name)
        if account is None:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=f"missing Schwab account hash for {request.broker_account_name}",
                    metadata=dict(request.metadata),
                )
            ]

        try:
            if request.intent_type == "cancel":
                return await self._cancel_order(account, request)

            status_code, headers, response = await self._authorized_request_json(
                "POST",
                f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders",
                body=self._build_order_payload(request),
            )
        except RuntimeError as exc:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason=str(exc),
                    metadata=dict(request.metadata),
                )
            ]

        if status_code >= 400:
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

        broker_order_id = self._extract_order_id(response, headers)
        reports = [
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

        final_order = await self._wait_for_terminal_order(account, broker_order_id)
        if final_order is None:
            return reports

        final_event_type = self._map_order_status(final_order)
        if final_event_type == "accepted":
            return reports

        reports.append(
            self._execution_report_from_order(
                request=request,
                order=final_order,
                event_type=final_event_type,
                broker_order_id=broker_order_id,
            )
        )
        return reports

    async def list_account_positions(self, broker_account_name: str) -> list[BrokerPositionSnapshot]:
        account = self.accounts_by_name.get(broker_account_name)
        if account is None:
            logger.warning("missing Schwab account hash for broker account %s", broker_account_name)
            return []

        try:
            status_code, _headers, response = await self._authorized_request_json(
                "GET",
                f"/trader/v1/accounts/{quote(account.account_hash, safe='')}?fields=positions",
            )
        except RuntimeError:
            logger.exception("failed listing Schwab positions for %s", broker_account_name)
            return []

        if status_code >= 400 or not isinstance(response, dict):
            logger.warning(
                "failed listing Schwab positions for %s: %s",
                broker_account_name,
                self._extract_error_reason(response),
            )
            return []

        account_payload = response.get("securitiesAccount", response)
        if not isinstance(account_payload, dict):
            return []

        snapshots: list[BrokerPositionSnapshot] = []
        for raw in account_payload.get("positions", []):
            if not isinstance(raw, dict):
                continue
            instrument = raw.get("instrument", {})
            symbol = ""
            if isinstance(instrument, dict):
                symbol = str(instrument.get("symbol", "")).upper()
            if not symbol:
                continue

            long_quantity = self._decimal_or_zero(raw.get("longQuantity"))
            short_quantity = self._decimal_or_zero(raw.get("shortQuantity"))
            quantity = long_quantity - short_quantity
            if quantity == 0:
                continue

            average_price = (
                self._decimal_or_none(raw.get("averagePrice"))
                or self._decimal_or_none(raw.get("averageLongPrice"))
                or self._decimal_or_none(raw.get("averageShortPrice"))
                or Decimal("0")
            )
            market_value = (
                self._decimal_or_none(raw.get("marketValue"))
                or self._decimal_or_none(raw.get("longMarketValue"))
                or self._decimal_or_none(raw.get("shortMarketValue"))
            )
            as_of = (
                self._parse_datetime(raw.get("tradeDate"))
                or self._parse_datetime(raw.get("settlementDate"))
                or datetime.now(UTC)
            )
            snapshots.append(
                BrokerPositionSnapshot(
                    broker_account_name=broker_account_name,
                    symbol=symbol,
                    quantity=quantity,
                    average_price=average_price,
                    market_value=market_value,
                    as_of=as_of,
                )
            )
        return snapshots

    async def fetch_quotes(
        self,
        symbols: list[str] | tuple[str, ...] | set[str],
    ) -> dict[str, dict[str, float | None]]:
        normalized = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            return {}

        try:
            status_code, _headers, response = await self._authorized_request_json(
                "GET",
                (
                    "/marketdata/v1/quotes?"
                    f"symbols={quote(','.join(normalized), safe='')}&fields=quote"
                ),
            )
        except RuntimeError:
            logger.exception("failed fetching Schwab quotes for %s", ",".join(normalized))
            return {}

        if status_code >= 400:
            logger.warning(
                "failed fetching Schwab quotes for %s: %s",
                ",".join(normalized),
                self._extract_error_reason(response),
            )
            return {}

        if not isinstance(response, dict):
            return {}

        quotes: dict[str, dict[str, float | None]] = {}
        for symbol in normalized:
            payload = response.get(symbol)
            if not isinstance(payload, dict):
                continue
            quote_payload = payload.get("quote")
            if isinstance(quote_payload, dict):
                payload = quote_payload
            bid_price = self._float_or_none(
                payload.get("bidPrice") or payload.get("bid") or payload.get("bid_price")
            )
            ask_price = self._float_or_none(
                payload.get("askPrice") or payload.get("ask") or payload.get("ask_price")
            )
            last_price = self._float_or_none(
                payload.get("lastPrice") or payload.get("last") or payload.get("last_price")
            )
            if bid_price is None and ask_price is None and last_price is None:
                continue
            quotes[symbol] = {
                "bid_price": bid_price,
                "ask_price": ask_price,
                "last_price": last_price,
            }
        return quotes

    async def fetch_order_update(self, request: OrderRequest) -> ExecutionReport | None:
        account = self.accounts_by_name.get(request.broker_account_name)
        if account is None:
            return None

        broker_order_id = str(request.metadata.get("broker_order_id", "")).strip()
        if not broker_order_id:
            return None

        order = await self._fetch_order(account, broker_order_id)
        if order is None:
            return None

        return self._execution_report_from_order(
            request=request,
            order=order,
            event_type=self._map_order_status(order),
            broker_order_id=broker_order_id,
        )

    async def _cancel_order(
        self,
        account: SchwabAccountConfig,
        request: OrderRequest,
    ) -> list[ExecutionReport]:
        broker_order_id = str(request.metadata.get("broker_order_id", "")).strip()
        if not broker_order_id:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason="missing broker_order_id for Schwab cancel intent",
                    metadata=dict(request.metadata),
                )
            ]

        try:
            status_code, _headers, response = await self._authorized_request_json(
                "DELETE",
                (
                    f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders/"
                    f"{quote(broker_order_id, safe='')}"
                ),
            )
        except RuntimeError as exc:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=str(exc),
                    metadata=dict(request.metadata),
                )
            ]

        if status_code >= 400:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="cancel",
                    quantity=request.quantity,
                    reason=self._extract_error_reason(response),
                    metadata=dict(request.metadata),
                )
            ]

        final_order = await self._fetch_order(account, broker_order_id)
        if final_order is None:
            return [
                ExecutionReport(
                    event_type="cancelled",
                    client_order_id=request.client_order_id,
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
                order=final_order,
                event_type=self._map_order_status(final_order),
                broker_order_id=broker_order_id,
            )
        ]

    async def _wait_for_terminal_order(
        self,
        account: SchwabAccountConfig,
        broker_order_id: str | None,
    ) -> dict[str, object] | None:
        if not broker_order_id:
            return None

        deadline = asyncio.get_running_loop().time() + max(1.0, self.fill_timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            await self._sleep(self.poll_interval_seconds)
            order = await self._fetch_order(account, broker_order_id)
            if order is None:
                continue
            if self._map_order_status(order) != "accepted":
                return order

        return None

    async def _fetch_order(
        self,
        account: SchwabAccountConfig,
        broker_order_id: str,
    ) -> dict[str, object] | None:
        try:
            status_code, _headers, order = await self._authorized_request_json(
                "GET",
                (
                    f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders/"
                    f"{quote(broker_order_id, safe='')}"
                ),
            )
        except RuntimeError:
            logger.exception("failed fetching Schwab order %s", broker_order_id)
            return None

        if status_code >= 400 or not isinstance(order, dict):
            return None
        return order

    async def _authorized_request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, str], object]:
        token = await self._get_access_token()
        status_code, headers, payload = await self._request_json(
            method,
            path,
            access_token=token,
            body=body,
        )
        if status_code == 401 and self._refresh_token and self.client_id and self.client_secret:
            token = await self._get_access_token(force_refresh=True)
            status_code, headers, payload = await self._request_json(
                method,
                path,
                access_token=token,
                body=body,
            )
        return status_code, headers, payload

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if not force_refresh and self._access_token and not self._access_token_needs_refresh():
                return self._access_token

            if not self.client_id or not self.client_secret or not self._refresh_token:
                if self._access_token and not force_refresh:
                    return self._access_token
                raise RuntimeError("missing Schwab OAuth credentials or refresh token")

            status_code, _headers, payload = await self._token_request_json(
                form_data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                }
            )
            if status_code >= 400 or not isinstance(payload, dict):
                raise RuntimeError(f"failed refreshing Schwab token: {self._extract_error_reason(payload)}")

            access_token = str(payload.get("access_token", "")).strip()
            if not access_token:
                raise RuntimeError("Schwab token refresh returned no access_token")

            self._access_token = access_token
            refreshed_token = str(payload.get("refresh_token", "")).strip()
            if refreshed_token:
                self._refresh_token = refreshed_token
            expires_in = int(payload.get("expires_in", 0) or 0)
            if expires_in > 0:
                self._access_token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
            else:
                self._access_token_expires_at = None
            self._save_token_store(payload)
            return access_token

    def _access_token_needs_refresh(self) -> bool:
        if not self._access_token:
            return True
        if self._access_token_expires_at is None:
            return False
        refresh_at = self._access_token_expires_at - timedelta(
            seconds=self.refresh_margin_seconds
        )
        return datetime.now(UTC) >= refresh_at

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, str], object]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        data: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}{path}"
        return await asyncio.to_thread(self._blocking_request_json, url, method, headers, data)

    async def _token_request_json(
        self,
        *,
        form_data: dict[str, str],
    ) -> tuple[int, dict[str, str], object]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("missing Schwab client_id or client_secret")
        basic_auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic_auth}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = urlencode(form_data).encode("utf-8")
        return await asyncio.to_thread(
            self._blocking_request_json,
            self.token_url,
            "POST",
            headers,
            data,
        )

    def _blocking_request_json(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, dict[str, str], object]:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return response.getcode(), dict(response.headers.items()), self._decode_json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            return exc.code, dict(exc.headers.items()), self._decode_json(raw)
        except Exception as exc:  # pragma: no cover - exercised via rejection fallback
            return 599, {}, {"message": str(exc)}

    def _build_order_payload(self, request: OrderRequest) -> dict[str, object]:
        order_type = str(request.metadata.get("order_type", request.order_type)).upper()
        session = str(request.metadata.get("session", "NORMAL")).upper()
        duration = self._map_duration(str(request.metadata.get("time_in_force", request.time_in_force)))
        payload: dict[str, object] = {
            "session": session,
            "duration": duration,
            "orderType": order_type,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "BUY" if request.side == "buy" else "SELL",
                    "quantity": float(request.quantity),
                    "instrument": {
                        "symbol": request.symbol,
                        "assetType": "EQUITY",
                    },
                }
            ],
        }
        limit_price = request.metadata.get("limit_price") or request.metadata.get("reference_price")
        stop_price = request.metadata.get("stop_price") or request.metadata.get("reference_price")
        if order_type == "LIMIT" and limit_price:
            payload["price"] = float(Decimal(str(limit_price)))
        if order_type == "STOP" and stop_price:
            payload["stopPrice"] = float(Decimal(str(stop_price)))
        return payload

    def _execution_report_from_order(
        self,
        *,
        request: OrderRequest,
        order: dict[str, object],
        event_type: str,
        broker_order_id: str | None,
    ) -> ExecutionReport:
        quantity = (
            self._decimal_or_none(order.get("quantity"))
            or self._decimal_or_none(order.get("requestedQuantity"))
            or request.quantity
        )
        filled_quantity = (
            self._decimal_or_none(order.get("filledQuantity"))
            or self._extract_filled_quantity(order)
            or Decimal("0")
        )
        fill_price = self._extract_fill_price(order)
        reported_at = (
            self._parse_datetime(order.get("closeTime"))
            or self._parse_datetime(order.get("cancelTime"))
            or self._parse_datetime(order.get("enteredTime"))
            or datetime.now(UTC)
        )
        return ExecutionReport(
            event_type=event_type,  # type: ignore[arg-type]
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id or self._extract_order_id(order, None),
            broker_fill_id=self._build_fill_id(
                broker_order_id or self._extract_order_id(order, None),
                filled_quantity,
                reported_at,
                event_type,
            ),
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            reason=self._extract_error_reason(order) or request.reason,
            metadata=dict(request.metadata),
            reported_at=reported_at,
        )

    def _build_fill_id(
        self,
        broker_order_id: str | None,
        filled_quantity: Decimal,
        reported_at: datetime,
        event_type: str,
    ) -> str | None:
        if event_type not in {"filled", "partially_filled"}:
            return None
        if not broker_order_id or filled_quantity <= 0:
            return None
        return f"{broker_order_id}:{filled_quantity}:{reported_at.isoformat()}"

    def _map_order_status(self, order: dict[str, object]) -> str:
        status = str(order.get("status", "")).upper()
        quantity = (
            self._decimal_or_none(order.get("quantity"))
            or self._decimal_or_none(order.get("requestedQuantity"))
            or Decimal("0")
        )
        filled_quantity = (
            self._decimal_or_none(order.get("filledQuantity"))
            or self._extract_filled_quantity(order)
            or Decimal("0")
        )
        if status in self.FILLED_STATUSES:
            return "filled"
        if status in self.REJECTED_STATUSES:
            return "rejected"
        if status in self.CANCELLED_STATUSES:
            return "cancelled"
        if status in self.PARTIAL_FILL_STATUSES:
            return "partially_filled"
        if quantity > 0 and Decimal("0") < filled_quantity < quantity:
            return "partially_filled"
        if status in self.ACCEPTED_STATUSES or not status:
            return "accepted"
        return "accepted"

    def _extract_filled_quantity(self, order: dict[str, object]) -> Decimal | None:
        total = Decimal("0")
        for activity in order.get("orderActivityCollection", []) or []:
            if not isinstance(activity, dict):
                continue
            for leg in activity.get("executionLegs", []) or []:
                if not isinstance(leg, dict):
                    continue
                total += self._decimal_or_zero(leg.get("quantity"))
        return total if total > 0 else None

    def _extract_fill_price(self, order: dict[str, object]) -> Decimal | None:
        total_value = Decimal("0")
        total_quantity = Decimal("0")
        for activity in order.get("orderActivityCollection", []) or []:
            if not isinstance(activity, dict):
                continue
            for leg in activity.get("executionLegs", []) or []:
                if not isinstance(leg, dict):
                    continue
                quantity = self._decimal_or_zero(leg.get("quantity"))
                price = self._decimal_or_none(leg.get("price"))
                if quantity > 0 and price is not None:
                    total_quantity += quantity
                    total_value += quantity * price
        if total_quantity > 0:
            return total_value / total_quantity
        return self._decimal_or_none(order.get("price"))

    def _extract_order_id(
        self,
        payload: object,
        headers: dict[str, str] | None,
    ) -> str | None:
        if headers:
            location = headers.get("location") or headers.get("Location")
            if location:
                order_id = location.rstrip("/").split("/")[-1]
                if order_id:
                    return order_id
        if isinstance(payload, dict):
            for key in ("orderId", "id"):
                value = payload.get(key)
                if value not in {None, ""}:
                    return str(value)
        return None

    def _map_duration(self, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"gtc", "good_till_cancel", "good_till_canceled"}:
            return "GOOD_TILL_CANCEL"
        return "DAY"

    def _load_token_store(self) -> None:
        if self._token_store_path is None or not self._token_store_path.exists():
            return
        try:
            payload = json.loads(self._token_store_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed reading Schwab token store %s", self._token_store_path)
            return
        if not isinstance(payload, dict):
            return
        self._access_token = str(payload.get("access_token", "")).strip() or self._access_token
        self._refresh_token = str(payload.get("refresh_token", "")).strip() or self._refresh_token
        expires_at = self._parse_datetime(payload.get("expires_at"))
        if expires_at is not None:
            self._access_token_expires_at = expires_at

    def _save_token_store(self, payload: dict[str, object]) -> None:
        if self._token_store_path is None:
            return
        document = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": (
                self._access_token_expires_at.isoformat()
                if self._access_token_expires_at is not None
                else None
            ),
            "token_type": payload.get("token_type"),
            "scope": payload.get("scope"),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._token_store_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_store_path.write_text(
                json.dumps(document, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("failed writing Schwab token store %s", self._token_store_path)

    def _extract_error_reason(self, payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "statusDescription", "description"):
                value = payload.get(key)
                if value:
                    return str(value)
        if payload is None:
            return "unknown Schwab error"
        return str(payload)

    def _parse_datetime(self, value: object) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            normalized = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

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

    def _float_or_none(self, value: object) -> float | None:
        decimal_value = self._decimal_or_none(value)
        if decimal_value is None:
            return None
        return float(decimal_value)

    def _decode_json(self, raw: str) -> object:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"message": raw}
