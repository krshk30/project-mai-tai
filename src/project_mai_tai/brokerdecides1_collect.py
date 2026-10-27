"""Source-bound native inventory collection. No trading mutation or cache fallback.

Webull list-open request is pinned to delivered SDK 2.0.11 source; retained
shapes remain unproved. The 14:51:24 reviewed Schwab policy requests 181 days,
accepting only older unfilled manual GTC outside an explicitly accepted window.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import time
import json
import logging
from contextvars import ContextVar
from functools import wraps
from urllib.parse import quote, urlencode

from project_mai_tai.brokerdecides1_native import (
    MAX_PAGES, MAX_ROWS, schwab_book, terminal_empty_open_orders, webull_book,
)

SDK_OPEN_REQUEST_SHA = "51200dd8b295b49d022be94994b43c8a184ce4cefc76ee6b6280f694c015f398"
MAX_REQUESTS = 32
READ_BUDGET_SECONDS = 4.0
LOGGER = logging.getLogger(__name__)
BACKGROUND_ALLOWED = ContextVar("l12_background_allowed", default=None)
BACKGROUND_TASKS = ContextVar("l12_background_tasks", default=None)
BACKGROUND_FOREGROUND_ACTIVE = ContextVar("l12_foreground_active", default=None)
NATIVE_DIAGNOSTIC = ContextVar("l12_native_diagnostic", default=None)
SAFE_FAILURE_CODES = frozenset({
    "native_foreground_sync_has_priority_UNKNOWN", "native_read_inflight_UNKNOWN",
    "native_quantity_UNKNOWN", "native_account_identity_UNKNOWN",
    "all_native_working_coverage_UNKNOWN", "Schwab_reviewed_window_UNKNOWN",
    "Schwab_collection_UNKNOWN_or_at_cap", "Schwab_account_UNKNOWN",
    "Schwab_account_error_UNKNOWN", "Schwab_positions_completeness_UNKNOWN",
    "Schwab_holding_UNKNOWN", "Schwab_holding_duplicate_or_quantity_UNKNOWN",
    "Schwab_negative_gross_quantity", "Schwab_order_tree_UNKNOWN_or_overflow",
    "Schwab_order_identity_UNKNOWN", "Schwab_duplicate_order", "Schwab_children_UNKNOWN",
    "Schwab_order_symbols_UNKNOWN", "Schwab_order_status_UNKNOWN", "Schwab_order_leg_UNKNOWN",
    "Schwab_native_query_budget_UNKNOWN", "Schwab_native_query_UNKNOWN",
    "Schwab_native_time_cap_UNKNOWN", "Schwab_native_order_identity_UNKNOWN",
    "Schwab_native_order_changed_during_query", "Schwab_native_row_cap_UNKNOWN",
    "Schwab_native_query_refused_not_range_UNKNOWN", "Schwab_native_fallback_nonrange_failure_UNKNOWN",
    "Schwab_native_no_accepted_window_UNKNOWN", "Schwab_native_account_unconfigured",
    "Schwab_native_account_map_UNKNOWN", "Schwab_native_account_map_ambiguous",
    "Schwab_native_positions_UNKNOWN", "Schwab_native_total_query_budget_UNKNOWN",
    "Webull_native_account_unconfigured", "Webull_native_collection_UNKNOWN",
    "Webull_native_envelope_UNKNOWN", "Webull_native_identity_or_continuation_UNKNOWN",
    "Webull_positions_request_binding_UNKNOWN", "Webull_native_acquisition_budget_UNKNOWN",
    "Webull_native_http_UNKNOWN", "Webull_native_row_cap_UNKNOWN", "Webull_native_cursor_UNKNOWN",
    "Webull_native_page_cap_UNKNOWN", "Webull_open_envelope_or_request_binding_UNKNOWN",
    "Webull_open_terminal_empty_shape_UNKNOWN", "Webull_nonempty_item_model_UNKNOWN",
    "Webull_pages_UNKNOWN", "Webull_open_empty_page_count_UNKNOWN", "Webull_envelope_UNKNOWN",
    "Webull_continuation_UNKNOWN", "Webull_native_rows_at_cap", "Webull_holding_UNKNOWN",
    "Webull_duplicate_holding", "Webull_order_UNKNOWN", "Webull_native_order_identity_UNKNOWN",
    "Webull_order_status_or_items_UNKNOWN", "Webull_order_item_UNKNOWN", "Webull_symbol_overflow",
})


def _diagnostic_stage(stage, page=0, status=None):
    state = NATIVE_DIAGNOSTIC.get()
    if state is not None:
        state.update(stage=stage, page=page,
            status=status if type(status) is int and 100 <= status <= 599 else "UNKNOWN")


def _log_native_failure(exc):
    state = NATIVE_DIAGNOSTIC.get()
    if state is None or state["logged"]:
        return
    state["logged"] = True
    # Never stringify SDK exceptions: exact known codes only, not secret scrubbing.
    message = exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str else None
    kind = type(exc).__name__
    if kind == "QueryBudgetUnavailable" and message in {
            "list-open background budget unavailable", "positions background budget unavailable",
            "list-open query budget unavailable", "positions query budget unavailable"}:
        safe_message = message
    else:
        safe_message = message if type(exc) is ValueError and message in SAFE_FAILURE_CODES else "REDACTED"
        kind = kind if type(exc) in (ValueError, RuntimeError, TypeError, TimeoutError) else "Exception"
    foreground = "UNKNOWN"
    try:
        getter = state["foreground"]
        value = getter() if getter is not None else None
        if type(value) is bool:
            foreground = str(value).lower()
    except Exception:
        pass
    try:
        LOGGER.warning("[BROKERDECIDES1-NATIVE-FAIL] account=%s broker=%s type=%s msg=%s "
            "stage=%s page=%s http_status=%s foreground_sync_active=%s",
            state["account"], state["broker"], kind, safe_message[:200], state["stage"],
            state["page"], state["status"], foreground)
    except Exception:
        pass  # Observability must not replace the original native exception.


def _observe_native(broker):
    def decorate(read):
        @wraps(read)
        async def observed(adapter, account):
            logical = account if type(account) is str and account in {
                "live:orb", "live:schwab_1m_v2", "live:webull", "live:schwab"} else "UNKNOWN"
            # to_thread copies context, but shares this bounded per-invocation state.
            state = dict(account=logical, broker=broker, stage="initialization", page=0,
                status="UNKNOWN", logged=False, foreground=BACKGROUND_FOREGROUND_ACTIVE.get())
            token = NATIVE_DIAGNOSTIC.set(state)
            try:
                return await read(adapter, account)
            except Exception as exc:
                _log_native_failure(exc)
                raise
            finally:
                NATIVE_DIAGNOSTIC.reset(token)
        return observed
    return decorate


def require_background_priority():
    allowed = BACKGROUND_ALLOWED.get()
    if allowed is not None and allowed() is not True:
        raise ValueError("native_foreground_sync_has_priority_UNKNOWN")


def stamp():
    return int(datetime.now(UTC).timestamp() * 1000)


async def owned_read(adapter, account, operation):
    """One retained task/account. Outer timeout/cancellation never starts a copy.

    Native blocking delegates may finish after the caller times out. Keep their
    task until done, consume exceptions, and reject a new pass while it is alive.
    No cancellation of the shielded native invocation and no automatic retry.
    """
    tasks = adapter.__dict__.setdefault("_brokerdecides_native_tasks", {})
    old = tasks.get(account)
    if old is not None and not old.done():
        raise ValueError("native_read_inflight_UNKNOWN")
    if old is not None and not old.cancelled():
        old.exception()
    async def observed_operation():
        try:
            return await operation()
        except Exception as exc:
            _log_native_failure(exc)
            raise

    task = tasks[account] = asyncio.create_task(observed_operation(), name="l12-native-inventory")
    retained = BACKGROUND_TASKS.get()
    if retained is not None:
        retained.add(task)
        task.add_done_callback(retained.discard)
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(task)


def _continuation(body, field, expected, page_size, *, request_account=None):
    if (not isinstance(body, dict) or body.get("error_code")
            or "error" in body or "errors" in body
            or not isinstance(body.get(field), list) or len(body[field]) > page_size):
        raise ValueError("Webull_native_envelope_UNKNOWN")
    account_ids = [body[k] for k in ("account_id", "accountId") if k in body]
    flags = [body[k] for k in ("has_next", "hasNext") if k in body]
    request_bound = (field == "holdings" and isinstance(expected, str) and bool(expected)
        and request_account == expected)
    if ((not account_ids and not request_bound) or any(str(v) != expected for v in account_ids)
            or not flags or any(type(v) is not bool or v != flags[0] for v in flags)):
        raise ValueError("Webull_native_identity_or_continuation_UNKNOWN")
    return body[field], flags[0]


def webull_pages(fetch, *, account_id, field, page_size, cursor_keys,
                 clock=time.monotonic, order_request_account=None, positions_request_account=None):
    if positions_request_account is not None and positions_request_account != account_id:
        raise ValueError("Webull_positions_request_binding_UNKNOWN")
    started, pages, cursors, cursor, total = clock(), [], set(), "", 0
    for _ in range(MAX_PAGES):
        if clock() - started > READ_BUDGET_SECONDS:
            raise ValueError("Webull_native_acquisition_budget_UNKNOWN")
        status, body = fetch(cursor)
        if type(status) is not int or status != 200:
            raise ValueError("Webull_native_http_UNKNOWN")
        if field == "orders" and order_request_account is not None:
            terminal_empty_open_orders(body, native_account=account_id, request_account=order_request_account)
            return [body]  # Nonempty/continuation remain UNKNOWN pending actual model proof.
        rows, more = _continuation(body, field, account_id, page_size,
                                  request_account=positions_request_account)
        total += len(rows)
        if total >= MAX_ROWS:
            raise ValueError("Webull_native_row_cap_UNKNOWN")
        pages.append(body)
        if not more:
            return pages
        last = rows[-1] if rows else None
        values = [last[k] for k in cursor_keys if isinstance(last, dict) and k in last]
        if (not values or any(not isinstance(v, str) or not v or v != values[0] for v in values)
                or values[0] in cursors or values[0] == cursor):
            raise ValueError("Webull_native_cursor_UNKNOWN")
        cursor = values[0]
        cursors.add(cursor)
    raise ValueError("Webull_native_page_cap_UNKNOWN")


def _webull_blocking(adapter, account):
    from webull.trade.request.get_open_orders_request import OpenOrdersListRequest
    from webull.trade.request.get_account_positions_request import AccountPositionsRequest

    configured = adapter.accounts_by_name.get(account)
    if configured is None:
        raise ValueError("Webull_native_account_unconfigured")
    client, started = adapter._get_client(), stamp()

    def fetch(request_type, cursor_key, page_size, endpoint):
        page_number = 0
        def page(cursor):
            nonlocal page_number
            page_number += 1
            stage = "orders" if endpoint == "list-open" else "positions"
            _diagnostic_stage(stage, page_number)
            require_background_priority()
            adapter._query_budget.claim_background(endpoint, configured.account_id)
            request = request_type()
            request.set_account_id(configured.account_id)
            request.set_page_size(page_size)
            request.set_connect_timeout(2)
            request.set_read_timeout(2)
            if cursor:
                getattr(request, cursor_key)(cursor)
            response = client.get_response(request)
            # Do NOT use the legacy adapter helper's default-200 fallback.
            status = getattr(response, "status_code", None)
            _diagnostic_stage(stage, page_number, status)
            return status, adapter._body(response)
        return page

    orders = webull_pages(fetch(OpenOrdersListRequest, "set_last_client_order_id", 100, "list-open"),
        account_id=configured.account_id, field="orders", page_size=100,
        cursor_keys=("client_order_id", "clientOrderId"), order_request_account=configured.account_id)
    positions = webull_pages(fetch(AccountPositionsRequest, "set_last_instrument_id", 50, "positions"),
        account_id=configured.account_id, field="holdings", page_size=50,
        cursor_keys=("instrument_id", "instrumentId"), positions_request_account=configured.account_id)
    _diagnostic_stage("normalization")
    return webull_book(account=account, native_account=configured.account_id,
        position_pages=positions, order_pages=orders, wire_started_ms=started,
        wire_completed_ms=stamp(), scope="all_native_working_including_previous_sessions", complete=True,
        order_request_account=configured.account_id, positions_request_account=configured.account_id)


@_observe_native("webull")
async def read_webull(adapter, account):
    async def invoke():
        require_background_priority()
        try:
            return await asyncio.to_thread(_webull_blocking, adapter, account)
        except Exception as exc:
            _log_native_failure(exc)
            raise ValueError("Webull_native_collection_UNKNOWN") from None
    return await owned_read(adapter, account, invoke)


async def schwab_windows(fetch, start, end, *, max_results=500, clock=time.monotonic):
    pending, rows, requests, started = [(start, end)], {}, 0, clock()
    while pending:
        if requests >= MAX_REQUESTS or clock() - started > READ_BUDGET_SECONDS:
            raise ValueError("Schwab_native_query_budget_UNKNOWN")
        lower, upper = pending.pop()
        requests += 1
        status, body = await fetch(lower, upper, max_results)
        if type(status) is not int or status != 200 or not isinstance(body, list):
            raise ValueError("Schwab_native_query_UNKNOWN")
        if len(body) >= max_results:
            if upper - lower <= timedelta(milliseconds=2):
                raise ValueError("Schwab_native_time_cap_UNKNOWN")
            middle = lower + (upper - lower) / 2
            # Inclusive overlap at middle; exact duplicate identity is deduped.
            pending.extend(((lower, middle), (middle, upper)))
            continue
        for row in body:
            identity = row.get("orderId") if isinstance(row, dict) else None
            if isinstance(identity, bool) or not isinstance(identity, (str, int)):
                raise ValueError("Schwab_native_order_identity_UNKNOWN")
            identity = str(identity)
            if identity in rows and rows[identity] != row:
                raise ValueError("Schwab_native_order_changed_during_query")
            rows[identity] = row
            if len(rows) >= MAX_ROWS:
                raise ValueError("Schwab_native_row_cap_UNKNOWN")
    return list(rows.values())


def explicit_range_refusal(status, body):
    if type(status) is not int or status not in {400, 422} or not isinstance(body, (dict, list)):
        return False
    text = json.dumps(body, default=str).lower()
    if len(text) > 8192 or any(word in text for word in ("token", "permission", "unauthorized")):
        return False
    return (any(word in text for word in ("fromenteredtime", "toenteredtime"))
        and any(word in text for word in ("range", "days", "between"))
        and any(word in text for word in ("maximum", "max ", "exceed", "within", "too large", "no more")))


async def accepted_schwab_window(fetch, now):
    """181 first; integer binary search ONLY after an explicit API range refusal.

    This finds the largest accepted whole-day window within 1..181 under a
    monotone maximum-range restriction. Generic HTTP/auth/transport errors stop.
    API semantics not matching this explicit contract remain UNKNOWN.
    """
    status, body = await fetch(now - timedelta(days=181), now, 500)
    if type(status) is int and status == 200 and isinstance(body, list):
        return 181, body
    if not explicit_range_refusal(status, body):
        raise ValueError("Schwab_native_query_refused_not_range_UNKNOWN")
    lower, upper, best, best_body = 1, 180, None, None
    while lower <= upper:
        days = (lower + upper) // 2
        status, body = await fetch(now - timedelta(days=days), now, 500)
        if type(status) is int and status == 200 and isinstance(body, list):
            best, best_body, lower = days, body, days + 1
        elif explicit_range_refusal(status, body):
            upper = days - 1
        else:
            raise ValueError("Schwab_native_fallback_nonrange_failure_UNKNOWN")
    if best is None:
        raise ValueError("Schwab_native_no_accepted_window_UNKNOWN")
    return best, best_body


@_observe_native("schwab")
async def read_schwab(adapter, account):
    async def invoke():
        configured = adapter.accounts_by_name.get(account)
        if configured is None:
            raise ValueError("Schwab_native_account_unconfigured")
        started, now = stamp(), datetime.now(UTC)
        _diagnostic_stage("account_mapping", 1)
        require_background_priority()
        status, _, mapping = await adapter._authorized_request_json("GET", "/trader/v1/accounts/accountNumbers")
        _diagnostic_stage("account_mapping", 1, status)
        if status != 200 or not isinstance(mapping, list) or len(mapping) > 64:
            raise ValueError("Schwab_native_account_map_UNKNOWN")
        matches = [row.get("accountNumber") for row in mapping if isinstance(row, dict)
                   and row.get("hashValue") == configured.account_hash]
        if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
            raise ValueError("Schwab_native_account_map_ambiguous")
        base = f"/trader/v1/accounts/{quote(configured.account_hash, safe='')}"
        _diagnostic_stage("positions", 1)
        require_background_priority()
        status, _, body = await adapter._authorized_request_json("GET", base + "?fields=positions")
        _diagnostic_stage("positions", 1, status)
        if status != 200:
            raise ValueError("Schwab_native_positions_UNKNOWN")
        positions_status = status

        requests, query_started = 0, time.monotonic()

        async def fetch(lower, upper, cap):
            nonlocal requests
            _diagnostic_stage("orders", requests + 1)
            require_background_priority()
            if requests >= MAX_REQUESTS or time.monotonic() - query_started > READ_BUDGET_SECONDS:
                raise ValueError("Schwab_native_total_query_budget_UNKNOWN")
            requests += 1
            query = urlencode({"fromEnteredTime": lower.isoformat(timespec="milliseconds"),
                "toEnteredTime": upper.isoformat(timespec="milliseconds"), "maxResults": cap})
            code, _, rows = await adapter._authorized_request_json("GET", base + "/orders?" + query)
            _diagnostic_stage("orders", requests, code)
            return code, rows  # Unfiltered: every status, including all nonterminal AWAITING_*.

        days, first_body = await accepted_schwab_window(fetch, now)
        first_used = False

        async def page(lower, upper, cap):
            nonlocal first_used
            if not first_used and lower == now - timedelta(days=days) and upper == now:
                first_used = True
                return 200, first_body
            return await fetch(lower, upper, cap)

        orders = await schwab_windows(page, now - timedelta(days=days), now)
        logged = adapter.__dict__.setdefault("_brokerdecides_scope_logged", {})
        if account not in logged:
            LOGGER.info("[L12-SCOPE] schwab_window_days=%d account=%s "
                "older_unfilled_manual_gtc=reviewer_accepted_residual policy=20261022_145124", days, account)
        elif logged[account] != days:
            LOGGER.warning("[L12-SCOPE-CHANGED] schwab_window_days=%d account=%s", days, account)
        logged[account] = days
        _diagnostic_stage("normalization")
        return schwab_book(account=account, native_account=matches[0], account_body=body, orders=orders,
            wire_started_ms=started, wire_completed_ms=stamp(),
            scope="schwab_reviewed_window_20261022_145124", complete=True, window_days=days,
            positions_request_account=matches[0], positions_http_status=positions_status)
    return await owned_read(adapter, account, invoke)
