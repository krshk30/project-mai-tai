"""Fresh bounded per-request reads, never the ordinary cached today/position readers."""

from decimal import Decimal, InvalidOperation

from project_mai_tai.webull_day_cancel_proof import DayCancelAbsence, FlatPositionRead


def _pages(leaf, request_type, account_id, endpoint, cursor_key, rows_key):
    from project_mai_tai.broker_adapters.cancel_terminal import _http_status, _webull_response

    cursor, seen = "", set()
    for _ in range(20):
        request = request_type()
        if (request.get_version(), request.get_method(), request.get_action_name()) != ("v2", "GET", endpoint):
            raise ValueError("endpoint_unknown")
        request.set_account_id(account_id)
        request.set_page_size(100 if rows_key == "orders" else 50)
        if cursor:
            getattr(request, "set_last_" + cursor_key)(cursor)
        response = _webull_response(leaf, request, endpoint=endpoint, owner=account_id)
        body = leaf._body(response)
        if (_http_status(response) != 200 or not isinstance(body, dict) or body.get("error_code")
                or body.get("account_id", account_id) != account_id):
            raise ValueError("page_unreadable")
        flags = [body[key] for key in ("has_next", "hasNext") if key in body]
        rows = body.get(rows_key)
        if (not flags or any(type(flag) is not bool or flag != flags[0] for flag in flags)
                or not isinstance(rows, list) or len(rows) > (100 if rows_key == "orders" else 50)):
            raise ValueError("pagination_unknown")
        for row in rows:
            if not isinstance(row, dict) or row.get("account_id", account_id) != account_id:
                raise ValueError("row_identity_unknown")
            identity = row.get(cursor_key)
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("row_identity_unknown")
            seen.add(identity)
            yield row
        if not flags[0]:
            return
        if not rows:
            raise ValueError("pagination_empty")
        cursor = rows[-1][cursor_key]
    raise ValueError("pagination_overflow")


def acquire_day_absence(leaf, bound):
    from webull.trade.request.get_account_positions_request import AccountPositionsRequest
    from webull.trade.request.get_today_orders_request import TodayOrdersListRequest

    from project_mai_tai.broker_adapters.cancel_terminal import _webull_book_row, now_ms
    from project_mai_tai.cancel_terminal_proof import CompleteWorkingBook

    scope = bound.scope
    started = now_ms()
    orders = tuple(_webull_book_row(row, scope.account_id) for row in _pages(
        leaf, TodayOrdersListRequest, scope.account_id, "/trade/orders/list-today",
        "client_order_id", "orders"))
    if any(row is None for row in orders):
        raise ValueError("today_order_unknown")
    today = CompleteWorkingBook(scope.account_name, scope.account_id, started, now_ms(),
                                True, "today", orders, "broker")
    started = now_ms()
    flat = True
    for raw in _pages(leaf, AccountPositionsRequest, scope.account_id, "/account/positions",
                      "instrument_id", "holdings"):
        try:
            quantity = Decimal(str(raw["quantity"]))
        except (KeyError, InvalidOperation, ValueError):
            raise ValueError("position_quantity_unknown") from None
        symbol = raw.get("symbol")
        if not quantity.is_finite() or not isinstance(symbol, str) or not symbol:
            raise ValueError("position_identity_unknown")
        if symbol.upper() == scope.symbol.upper() and quantity != 0:
            flat = False
    positions = FlatPositionRead(scope.account_name, scope.account_id, scope.symbol,
                                 started, now_ms(), True, flat)
    return DayCancelAbsence(bound, today, positions)


def decode_day_absence(raw):
    from project_mai_tai.cancel_terminal_proof import BookOrder, CancelScope, CompleteWorkingBook
    from project_mai_tai.webull_day_cancel_proof import BoundDayCancel

    bound = dict(raw["bound"])
    bound["scope"] = CancelScope(**bound["scope"])
    book = dict(raw["today"])
    book["orders"] = tuple(BookOrder(**row) for row in book["orders"])
    return DayCancelAbsence(BoundDayCancel(**bound), CompleteWorkingBook(**book),
                            FlatPositionRead(**raw["positions"]))
