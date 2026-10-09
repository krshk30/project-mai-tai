"""Off-loop Webull broker evidence; Schwab requires a separate local witness."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from time import time_ns

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    TERMINAL, BookOrder, CancelReceipt, CancelTerminalEvidence, CompleteWorkingBook,
)


def now_ms() -> int:
    return time_ns() // 1_000_000


def broker_binding(adapter, account_name: str) -> tuple[object, str]:
    adapter = getattr(adapter, "cancel_terminal_delegate", adapter)
    leaf = (adapter._adapter_for_account(account_name)
            if isinstance(adapter, RoutingBrokerAdapter) else adapter)
    account = getattr(leaf, "accounts_by_name", {}).get(account_name)
    if isinstance(leaf, WebullBrokerAdapter) and account:
        return leaf, account.account_id
    if isinstance(leaf, SchwabBrokerAdapter) and account:
        return leaf, account.account_hash
    raise ValueError("cancel_broker_account_unavailable")


def _zero(value: object) -> bool:
    try:
        return value is not None and not isinstance(value, bool) and Decimal(str(value)) == 0
    except (ValueError, InvalidOperation):
        return False


def _status(value: object) -> str:
    raw = str(value).lower()
    return {"submitted": "working", "partial_filled": "partially_filled",
            "partial_fill": "partially_filled", "failed": "rejected"}.get(raw, raw)


def _webull_response(leaf, request, *, endpoint: str, owner: str):
    client = leaf._get_client()
    # ApiClient defaults auto_retry=False. Refuse injected/unknown retry policies
    # so one permit cannot hide multiple target endpoint HTTP attempts.
    if getattr(client, "_auto_retry", None) is not False:
        raise ValueError("sdk_retry_policy_unknown")
    # Detail and open-book reads share a stricter ceiling in addition to the
    # existing endpoint ceilings; no other caller's permits are weakened.
    leaf._query_budget.claim("cancel-terminal-family", owner, strict=True)
    leaf._query_budget.claim(endpoint, owner, strict=True)
    return client.get_response(request)


def _http_status(response) -> int | None:
    for attr in ("status_code", "code", "status"):
        code = getattr(response, attr, None)
        if type(code) is int:
            return code
    return None


class _TargetNotFound(ValueError):
    """Only a structured venue ORDER_NOT_FOUND, never malformed/empty detail."""


class _TargetEmpty(ValueError):
    """Measured HTTP200/zero-byte shape; no target receipt is implied."""


def _webull_book_row(row: dict, account_id: str) -> BookOrder:
    # Official v2 TradeClient.getOpenedOrders returns Orders<ComboOrder> for US:
    # identity is on the parent and symbol/status/side are in its items.
    items = row.get("items") if "items" in row else [row]
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("open_book_legs_unknown")
    item = items[0]
    if item.get("account_id", account_id) != account_id:
        raise ValueError("open_book_leg_account_unknown")
    coid, broker_id = row.get("client_order_id", ""), row.get("order_id", "")
    symbol = item.get("symbol")
    status = _status(item.get("order_status", item.get("status", "")))
    side = item.get("side", "unknown")
    side = side.lower() if isinstance(side, str) else "unknown"
    if (not isinstance(coid, str) or not isinstance(broker_id, str) or not (coid or broker_id)
            or not isinstance(symbol, str) or not symbol
            or status not in {"working", "pending", "pending_cancel", "pending_replace",
                              "partially_filled", "accepted", "new"} | TERMINAL | {"filled"}):
        raise ValueError("open_book_row_unknown")
    if item is not row and any(k in row and row[k] != item.get(k)
                              for k in ("symbol", "side", "order_status", "status")):
        raise ValueError("open_book_parent_leg_conflict")
    if status in TERMINAL and not _zero(item.get("filled_qty")):
        raise ValueError("open_book_terminal_fills_unknown_or_present")
    return BookOrder(coid, symbol, status, side, broker_id)


def _webull_book(
    leaf: WebullBrokerAdapter, account_name: str, account_id: str,
) -> CompleteWorkingBook:
    # This SDK request is /trade/orders/list-open, never list-today or target cache.
    from webull.trade.request.get_open_orders_request import OpenOrdersListRequest

    started = now_ms()
    orders: list[BookOrder] = []
    seen: set[tuple[str, str]] = set()
    cursors: set[str] = set()
    cursor = ""
    for _ in range(20):
        request = OpenOrdersListRequest()
        if (request.get_action_name(), request.get_version(), request.get_method()) != (
            "/trade/orders/list-open", "v2", "GET",
        ):
            raise ValueError("open_book_sdk_contract_unmeasured")
        request.set_account_id(account_id)
        request.set_page_size(100)
        if cursor:
            request.set_last_client_order_id(cursor)
        response = _webull_response(leaf, request, endpoint="list-open", owner=account_id)
        body = leaf._body(response)
        if (_http_status(response) != 200 or not isinstance(body, dict)
                or body.get("error_code")):
            raise ValueError("open_book_unreadable")
        if body.get("account_id", account_id) != account_id:
            raise ValueError("open_book_account_mismatch")
        flags = [body[k] for k in ("has_next", "hasNext") if k in body]
        if not flags or any(type(flag) is not bool or flag != flags[0] for flag in flags):
            raise ValueError("open_book_pagination_unknown")
        rows = body.get("orders")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("open_book_rows_unknown")
        for row in rows:
            if not isinstance(row, dict) or row.get("account_id", account_id) != account_id:
                raise ValueError("open_book_row_account_unknown")
            order = _webull_book_row(row, account_id)
            identity = order.client_order_id, order.broker_order_id
            if any((order.client_order_id and order.client_order_id == coid)
                   or (order.broker_order_id and order.broker_order_id == broker_id)
                   for coid, broker_id in seen):
                raise ValueError("open_book_duplicate_order")
            seen.add(identity)
            orders.append(order)
        if not flags[0]:
            return CompleteWorkingBook(account_name, account_id, started, now_ms(),
                                       True, "all_working", tuple(orders), "broker")
        if not rows:
            raise ValueError("open_book_empty_next_page")
        cursor = rows[-1].get("client_order_id", "")
        if not cursor or cursor in cursors:
            raise ValueError("open_book_cursor_did_not_advance")
        cursors.add(cursor)
    raise ValueError("open_book_page_limit")


class CompleteBookCycle:
    """One physical account read shared by assessments in this adapter/process.

    Failed reads are shared too. Timestamps are never refreshed by cache access.
    A pending physical SDK read remains retained after an assessment timeout.
    """

    def __init__(self):
        self.reads: dict[str, tuple[int, asyncio.Task]] = {}

    async def acquire(self, leaf, name, account_id, after_ms):
        current = self.reads.get(account_id)
        if current is None or (current[1].done() and now_ms() - current[0] > 15_000):
            async def read():
                try:
                    return await asyncio.to_thread(_webull_book, leaf, name, account_id)
                except Exception:
                    return None
            current = (now_ms(), asyncio.create_task(read()))
            self.reads[account_id] = current
        try:
            book = await asyncio.wait_for(asyncio.shield(current[1]), timeout=15)
        except TimeoutError:
            return None
        if (not isinstance(book, CompleteWorkingBook) or book.complete is not True
                or book.source != "broker" or book.coverage != "all_working"
                or not after_ms <= book.started_at_ms <= book.finished_at_ms <= now_ms()
                or now_ms() - book.started_at_ms > 15_000):
            return None
        return replace(book, account_name=name)

    async def drain(self):
        await asyncio.gather(*(read for _start, read in self.reads.values()), return_exceptions=True)


async def acquire_complete_working_book(
    adapter, account_name: str, *, after_ms: int = 0, cycle: CompleteBookCycle | None = None,
) -> CompleteWorkingBook | None:
    """Webull only; no Schwab HTTP or relabelled local journal inventory."""
    try:
        leaf, account_id = broker_binding(adapter, account_name)
        if not isinstance(leaf, WebullBrokerAdapter):
            return None
        if cycle is None:
            cycle = getattr(leaf, "_cancel_terminal_book_cycle", None)
            if cycle is None:
                cycle = leaf._cancel_terminal_book_cycle = CompleteBookCycle()
        return await cycle.acquire(leaf, account_name, account_id, after_ms)
    except Exception:
        return None


async def acquire_request_working_books(
    adapter, account_names, *, after_ms: int = 0, cycle: CompleteBookCycle | None = None,
) -> dict[str, CompleteWorkingBook | None]:
    """Explicit per-request acquisition with one shared 15-second read bound."""
    books = dict.fromkeys(account_names)
    try:
        async with asyncio.timeout(15):
            for name in books:
                books[name] = await acquire_complete_working_book(adapter, name, after_ms=after_ms, cycle=cycle)
    except TimeoutError:
        pass
    return books


def _webull_target(leaf: WebullBrokerAdapter, receipt: CancelReceipt, broker_order_id: str):
    from webull.trade.request.get_order_detail_request import OrderDetailRequest

    scope = receipt.scope
    request = OrderDetailRequest()
    request.set_account_id(scope.account_id)
    request.set_client_order_id(scope.client_order_id)
    try:
        response = _webull_response(leaf, request, endpoint="detail",
                                  owner=f"{scope.account_id}:{scope.client_order_id}")
    except Exception as exc:
        if (getattr(exc, "error_code", None) == "ORDER_NOT_FOUND"
                and getattr(exc, "http_status", None) in {404, 417}):
            raise _TargetNotFound("broker_order_not_found") from exc
        raise
    body = leaf._body(response)
    if _http_status(response) == 200 and body is None and getattr(response, "content", None) == b"":
        raise _TargetEmpty("broker_target_empty")
    if (isinstance(body, dict) and body.get("error_code") == "ORDER_NOT_FOUND"
            and _http_status(response) in {200, 404, 417}
            and body.get("account_id", scope.account_id) == scope.account_id
            and body.get("client_order_id", scope.client_order_id) == scope.client_order_id):
        raise _TargetNotFound("broker_order_not_found")
    if _http_status(response) != 200 or not isinstance(body, dict) or body.get("error_code"):
        raise ValueError("target_unreadable")
    items = body.get("items")
    if (body.get("account_id", scope.account_id) != scope.account_id
            or body.get("client_order_id") != scope.client_order_id
            or not body.get("order_id")
            or (broker_order_id and body.get("order_id") != broker_order_id)
            or not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get("symbol") != scope.symbol):
        raise ValueError("target_identity_unknown")
    item = items[0]
    status = _status(item.get("order_status", item.get("status", "")))
    filled = "0" if _zero(item.get("filled_qty")) else "unknown_or_present"
    return status, filled


async def acquire_broker_cancel_evidence(
    adapter, receipt: CancelReceipt, *, broker_order_id: str = "", cycle: CompleteBookCycle | None = None,
) -> CancelTerminalEvidence:
    leaf, account_id = broker_binding(adapter, receipt.scope.account_name)
    if account_id != receipt.scope.account_id:
        raise ValueError("cancel_broker_binding_changed")
    evidence = CancelTerminalEvidence(receipt, None)
    status, filled = "", ""
    if isinstance(leaf, WebullBrokerAdapter):
        try:
            status, filled = await asyncio.to_thread(_webull_target, leaf, receipt, broker_order_id)
        except _TargetNotFound:
            pass
        except _TargetEmpty:
            if receipt.status != "rejected" or not (
                receipt.refusal_code == "cancel_target_not_found"
                or receipt.refusal_origin == "skipped_before_submit"
            ):
                return evidence
        except Exception:
            return evidence
        # A known working/fill read is a contradiction and cannot fall back to absence.
        if not status:
            try:
                book = await acquire_complete_working_book(adapter, receipt.scope.account_name,
                    after_ms=receipt.observed_at_ms, cycle=cycle)
                evidence = replace(evidence, book=book, source="broker" if book else "unknown")
            except Exception:
                if not status:
                    return evidence
    if status:
        return replace(evidence, source="broker", target_status=status,
                       target_observed_at_ms=now_ms(), target_client_order_id=receipt.scope.client_order_id,
                       target_symbol=receipt.scope.symbol, target_account_id=account_id,
                       target_filled_quantity=filled)
    return evidence
