"""Strict native-exposure normalization, separate from broker request execution.

These functions never infer full native order coverage from locally known IDs,
today-only orders, or a missing continuation flag. Callers must supply a native
ALL-WORKING collection, including pre-session GTC/manual orders, or the explicit
14:51:24 Schwab reviewed-window policy. That policy accepts only older unfilled
manual GTC outside the window, never unread positions or partial window reads.
Unproven scope raises even when empty. Actual reader qualification remains a gate.
"""

from decimal import Decimal, InvalidOperation

from project_mai_tai.brokerdecides1 import MAX_SYMBOLS, NativeAccountBook, symbols
from project_mai_tai.webull_open_orders import open_order_page

MAX_ROWS = 2048
MAX_PAGES = 20
SCHWAB_TERMINAL = frozenset({"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"})
WEBULL_TERMINAL = frozenset({"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"})


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("native_quantity_UNKNOWN")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("native_quantity_UNKNOWN") from None
    if not number.is_finite():
        raise ValueError("native_quantity_UNKNOWN")
    return number


def _symbol(value):
    symbols((value,))
    return value


def _identity(row, expected, keys, *, required=False):
    found = [row[k] for k in keys if k in row]
    if required and not found or any(str(v) != expected for v in found):
        raise ValueError("native_account_identity_UNKNOWN")


def _scope(scope, complete):
    if scope != "all_native_working_including_previous_sessions" or complete is not True:
        raise ValueError("all_native_working_coverage_UNKNOWN")


def schwab_book(*, account, native_account, account_body, orders,
                wire_started_ms, wire_completed_ms, scope, complete, window_days=None,
                positions_request_account=None, positions_http_status=None):
    window_only = scope == "schwab_entered_60d_query_only" and complete is False
    reviewed = scope == "schwab_reviewed_window_20261022_145124" and complete is True
    if reviewed and (type(window_days) is not int or not 1 <= window_days <= 181):
        raise ValueError("Schwab_reviewed_window_UNKNOWN")
    if not window_only and not reviewed:
        _scope(scope, complete)
    if not isinstance(account_body, dict) or not isinstance(orders, list) or len(orders) >= MAX_ROWS:
        raise ValueError("Schwab_collection_UNKNOWN_or_at_cap")
    body = account_body.get("securitiesAccount")
    if not isinstance(body, dict):
        raise ValueError("Schwab_account_UNKNOWN")
    if any(key in value for value in (account_body, body)
           for key in ("error", "errors", "error_code", "errorCode")):
        raise ValueError("Schwab_account_error_UNKNOWN")
    _identity(body, native_account, ("accountNumber",), required=True)
    if "positions" not in body:
        # C1 2026-10-27 15:27: only this fresh, successful account request may
        # interpret Schwab's omitted flat positions field; NULL is not absence.
        if (type(positions_http_status) is not int or positions_http_status != 200
                or not isinstance(native_account, str) or not native_account
                or positions_request_account != native_account
                or body.get("accountNumber") != native_account
                or complete is not True):
            raise ValueError("Schwab_positions_completeness_UNKNOWN")
        positions = []
    else:
        positions = body["positions"]
    if not isinstance(positions, list) or len(positions) >= MAX_ROWS:
        raise ValueError("Schwab_positions_completeness_UNKNOWN")
    held, seen = set(), set()
    for row in positions:
        if not isinstance(row, dict) or not isinstance(row.get("instrument"), dict):
            raise ValueError("Schwab_holding_UNKNOWN")
        symbol = _symbol(row["instrument"].get("symbol"))
        if symbol in seen or not {"longQuantity", "shortQuantity"}.issubset(row):
            raise ValueError("Schwab_holding_duplicate_or_quantity_UNKNOWN")
        seen.add(symbol)
        long, short = _number(row["longQuantity"]), _number(row["shortQuantity"])
        if long < 0 or short < 0:
            raise ValueError("Schwab_negative_gross_quantity")
        if long or short:
            held.add(symbol)  # Gross exposure: equal long/short is NOT absence.
    working, order_ids = set(), set()
    stack = [(row, 0) for row in orders]
    while stack:
        row, depth = stack.pop()
        if not isinstance(row, dict) or depth > 8 or len(order_ids) >= MAX_ROWS:
            raise ValueError("Schwab_order_tree_UNKNOWN_or_overflow")
        _identity(row, native_account, ("accountNumber",))
        order_id = row.get("orderId")
        if isinstance(order_id, bool) or not isinstance(order_id, (int, str)) or not str(order_id):
            raise ValueError("Schwab_order_identity_UNKNOWN")
        if str(order_id) in order_ids:
            raise ValueError("Schwab_duplicate_order")
        order_ids.add(str(order_id))
        children = row.get("childOrderStrategies", [])
        if not isinstance(children, list) or len(children) > MAX_ROWS or len(stack) + len(children) > MAX_ROWS:
            raise ValueError("Schwab_children_UNKNOWN")
        stack.extend((child, depth + 1) for child in children)
        legs = row.get("orderLegCollection", [])
        if not isinstance(legs, list) or len(legs) > 32 or not legs and not children:
            raise ValueError("Schwab_order_symbols_UNKNOWN")
        status = row.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("Schwab_order_status_UNKNOWN")
        for leg in legs:
            if not isinstance(leg, dict) or not isinstance(leg.get("instrument"), dict):
                raise ValueError("Schwab_order_leg_UNKNOWN")
            symbol = _symbol(leg["instrument"].get("symbol"))
            if status.upper() not in SCHWAB_TERMINAL:
                working.add(symbol)  # Both BUY and SELL, including contingent siblings.
    return NativeAccountBook(account, "schwab", wire_started_ms, wire_completed_ms,
        frozenset(working), frozenset(held), not window_only,
        (f"Schwab_reviewed_{window_days}d_older_unfilled_GTC_accepted" if reviewed else
         "Schwab_older_GTC_universe_UNKNOWN" if window_only else "complete_native_all_working_and_positions"),
        scope_policy="schwab_reviewed_window_20261022_145124" if reviewed else "all_native_working_including_previous_sessions",
        window_days=window_days if reviewed else None)


def terminal_empty_open_orders(body, *, native_account, request_account):
    """Only the actual 16:11:31 terminal empty shape is proved so far.

    The endpoint does NOT echo account id. Bind its own fresh request context,
    never fabricate response identity or infer a non-empty item model.
    """
    if (not isinstance(native_account, str) or not native_account
            or request_account != native_account or not isinstance(body, dict)
            or not {"hasNext", "pageSize", "orders"}.issubset(body)
            or set(body) - {"hasNext", "pageSize", "orders", "has_next", "account_id", "accountId"}):
        raise ValueError("Webull_open_envelope_or_request_binding_UNKNOWN")
    _identity(body, native_account, ("account_id", "accountId"))
    if (body["hasNext"] is not False or "has_next" in body and body["has_next"] is not False
            or type(body["pageSize"]) is not int or body["pageSize"] != 0
            or not isinstance(body["orders"], list)):
        raise ValueError("Webull_open_terminal_empty_shape_UNKNOWN")
    if body["orders"]:
        raise ValueError("Webull_nonempty_item_model_UNKNOWN")
    return []


def _pages(pages, *, native_account, field, page_size, request_account=None):
    if not isinstance(pages, (list, tuple)) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError("Webull_pages_UNKNOWN")
    if field == "orders" and request_account is not None:
        result, cursors = [], set()
        for index, body in enumerate(pages):
            rows, more, cursor = open_order_page(body, native_account=native_account,
                                                 request_account=request_account)
            if more != (index < len(pages) - 1) or cursor is not None and cursor in cursors:
                raise ValueError("Webull_continuation_UNKNOWN")
            if cursor is not None:
                cursors.add(cursor)
            result.extend(rows)
            if len(result) >= MAX_ROWS:
                raise ValueError("Webull_native_rows_at_cap")
        return result
    if request_account is not None and (
            not isinstance(native_account, str) or not native_account
            or request_account != native_account):
        raise ValueError("Webull_positions_request_binding_UNKNOWN")
    result = []
    for index, body in enumerate(pages):
        if (not isinstance(body, dict) or body.get("error_code")
                or "error" in body or "errors" in body):
            raise ValueError("Webull_envelope_UNKNOWN")
        _identity(body, native_account, ("account_id", "accountId"), required=request_account is None)
        rows = body.get(field)
        flags = [body[k] for k in ("has_next", "hasNext") if k in body]
        if (not isinstance(rows, list) or len(rows) > page_size or not flags
                or any(type(v) is not bool or v != flags[0] for v in flags)
                or flags[0] != (index < len(pages) - 1)):
            raise ValueError("Webull_continuation_UNKNOWN")
        result.extend(rows)
        if len(result) >= MAX_ROWS:
            raise ValueError("Webull_native_rows_at_cap")
    return result


def webull_book(*, account, native_account, position_pages, order_pages,
                wire_started_ms, wire_completed_ms, scope, complete, order_request_account=None,
                positions_request_account=None, symbol_unknown_observer=None):
    query_only = scope == "webull_list_open_query_only" and complete is False
    if not query_only:
        _scope(scope, complete)
    held, seen = set(), set()
    for row in _pages(position_pages, native_account=native_account, field="holdings", page_size=50,
                      request_account=positions_request_account):
        if not isinstance(row, dict):
            raise ValueError("Webull_holding_UNKNOWN")
        _identity(row, native_account, ("account_id", "accountId"))
        symbol = _symbol(row.get("symbol"))
        if symbol in seen:
            raise ValueError("Webull_duplicate_holding")
        seen.add(symbol)
        quantity = None
        for key in ("quantity", "qty", "position", "shares"):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue
            quantity = value
            break
        if _number(quantity):
            held.add(symbol)
    working, seen = set(), set()
    rows = _pages(order_pages, native_account=native_account, field="orders", page_size=100,
                  request_account=order_request_account)
    if order_request_account is not None:
        unknown, identities, reasons = set(), {}, {}
        def mark_unknown(affected, reason):
            unknown.update(affected)
            for symbol in affected:
                reasons.setdefault(symbol, set()).add(reason)
        for row in rows:
            if row.unknown_reason:
                mark_unknown(row.symbols, row.unknown_reason)
            elif row.working:
                working.update(row.symbols)
            if row.order_id:
                if row.order_id in identities:
                    mark_unknown(identities[row.order_id] | row.symbols, "duplicate_order_identity")
                identities.setdefault(row.order_id, set()).update(row.symbols)
        # Keep schema3 readable by the already-running consumers. Unknown is
        # a conservative symbol hold in their existing native-working set.
        working.update(unknown)
        if len(working | held) > MAX_SYMBOLS:
            raise ValueError("Webull_symbol_overflow")
        book = NativeAccountBook(account, "webull", wire_started_ms, wire_completed_ms,
            frozenset(working), frozenset(held), not query_only,
            "Webull_symbol_scoped_UNKNOWN" if unknown else
            "Webull_retained_manual_GTC_scope_UNKNOWN" if query_only else
            "complete_native_all_working_and_positions")
        if book.complete and symbol_unknown_observer is not None:
            symbol_unknown_observer({s: tuple(sorted(v)) for s, v in reasons.items()})
        return book
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Webull_order_UNKNOWN")
        _identity(row, native_account, ("account_id", "accountId"))
        native_id = row.get("order_id")
        if not isinstance(native_id, str) or not native_id or native_id in seen:
            raise ValueError("Webull_native_order_identity_UNKNOWN")
        seen.add(native_id)
        status = row.get("status")
        items = row.get("items")
        if (not isinstance(status, str) or not status or not isinstance(items, list)
                or not items or len(items) > 32):
            raise ValueError("Webull_order_status_or_items_UNKNOWN")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Webull_order_item_UNKNOWN")
            symbol = _symbol(item.get("symbol"))
            if status.upper() not in WEBULL_TERMINAL:
                working.add(symbol)
    if len(working | held) > MAX_SYMBOLS:
        raise ValueError("Webull_symbol_overflow")
    return NativeAccountBook(account, "webull", wire_started_ms, wire_completed_ms,
        frozenset(working), frozenset(held), not query_only and not rows and order_request_account is not None,
        "Webull_nonempty_item_model_UNKNOWN" if rows else
        "Webull_retained_manual_GTC_scope_UNKNOWN" if query_only else
        "Webull_open_request_binding_UNKNOWN" if order_request_account is None else
        "complete_native_all_working_and_positions")
